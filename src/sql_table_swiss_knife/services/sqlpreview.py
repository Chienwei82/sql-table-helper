"""SQL preview model: the three renderings, the panel's copy targets, dry-run (FR-5).

The SQL panel has exactly one source of truth — the ``SqlStatement`` objects the provider
would execute — and this module is the pure logic over them. It is deliberately *not* a
service with I/O: given a table and a set of changes it answers "what would run, and in
which rendering". That is what makes the panel's contents testable without a terminal and
guarantees the preview cannot drift from execution (FR-5.4).

Three renderings, one per mode (FR-5.2/5.3):

``PARAMETERIZED``
    exactly what is sent to the driver, with the parameter legend underneath. Use this to
    answer "what is the app actually doing".
``LITERAL``
    the same statements with values inlined and escaped (``N'…'``, ``0x…``, ISO dates).
    Use this to paste into another tool.
``SCRIPT``
    every statement wrapped in ``BEGIN TRANSACTION`` / ``TRY…CATCH`` / ``COMMIT``, with
    ``SET IDENTITY_INSERT`` where a script needs it. Use this to run the change set by hand.

The two safety properties this module exists to guarantee:

* **Dry-run** — the panel never executes anything at all; it only produces text.
  Previewing is therefore always safe, so the *generated* actions (SELECT, INSERT, MERGE,
  "insert script for all rows") are available even on a read-only table.
* **All-or-nothing** — the script mode is the only rendering that claims to leave the
  database unchanged on failure, so it is what "copy script" copies.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum

from ..domain.catalog import Table
from ..domain.changes import ChangeKind, PendingChange, row_label
from ..domain.rows import RowKey
from ..providers import (
    ApplyOptions,
    SqlDialect,
    SqlParam,
    SqlStatement,
    build_delete,
    build_merge,
    build_row_select,
    build_row_update,
    build_script,
    build_statements,
    build_table_insert,
)

__all__ = [
    "EMPTY_HINT",
    "PREVIEW_ONLY_NOTE",
    "RowAction",
    "SqlEntry",
    "SqlMode",
    "SqlPreview",
    "entries_for",
    "generate_for",
    "next_mode",
]


class SqlMode(Enum):
    """Which rendering the panel is showing (FR-5.2/5.3)."""

    PARAMETERIZED = "parameterized"
    LITERAL = "literal"
    SCRIPT = "script"

    @property
    def label(self) -> str:
        """Short label for the tab strip."""
        return {
            SqlMode.PARAMETERIZED: "Parameterized",
            SqlMode.LITERAL: "Literal",
            SqlMode.SCRIPT: "Script",
        }[self]

    @property
    def hint(self) -> str:
        """One-line explanation shown under the tabs, so the mode is never a guess."""
        return {
            SqlMode.PARAMETERIZED: "what is sent to the server",
            SqlMode.LITERAL: "values inlined — copy/paste ready",
            SqlMode.SCRIPT: "one transaction, rolled back on error",
        }[self]


def next_mode(mode: SqlMode) -> SqlMode:
    """The mode after ``mode`` when the toggle key is pressed.

    Cycles back to the first mode, so the key is a complete control with no dead end.
    """
    order = (SqlMode.PARAMETERIZED, SqlMode.LITERAL, SqlMode.SCRIPT)
    return order[(order.index(mode) + 1) % len(order)]


class RowAction(Enum):
    """The ad-hoc "generate SQL for…" actions (FR-5.6).

    These describe a *row* (or a whole filter) rather than a pending change, so they work
    on tables the app would never let you edit — a read-only view, a table with no key for
    UPDATE/DELETE. Only SELECT and the INSERT-script actions are unconditionally available.
    """

    SELECT = "select"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    MERGE = "merge"
    INSERT_SCRIPT = "insert_script"

    @property
    def label(self) -> str:
        return {
            RowAction.SELECT: "SELECT",
            RowAction.INSERT: "INSERT",
            RowAction.UPDATE: "UPDATE",
            RowAction.DELETE: "DELETE",
            RowAction.MERGE: "MERGE (upsert by key)",
            RowAction.INSERT_SCRIPT: "INSERT script for all rows in this table/filter",
        }[self]

    @property
    def needs_key(self) -> bool:
        """True for the actions that address exactly one row by its key."""
        return self in {RowAction.SELECT, RowAction.UPDATE, RowAction.DELETE, RowAction.MERGE}

    @property
    def covers_table(self) -> bool:
        """True for the actions that cover every loaded row, not just the focused one."""
        return self in {RowAction.INSERT_SCRIPT, RowAction.MERGE}

    @property
    def needs_values(self) -> bool:
        """True when the action needs the row's *values*, not just its key."""
        return self in {RowAction.INSERT, RowAction.MERGE, RowAction.INSERT_SCRIPT}


@dataclass(frozen=True, slots=True)
class SqlEntry:
    """One generated statement plus everything the panel shows about it (FR-5.1).

    ``dialect`` is held rather than looked up because the legend renders values through it;
    that is what guarantees the legend and the literal statement agree.
    """

    index: int
    kind: ChangeKind
    table: str
    row_key: RowKey | None
    statement: SqlStatement
    dialect: SqlDialect | None = None

    @property
    def label(self) -> str:
        """``1 · UPDATE dbo.Country (Code='DE')`` — what the statement list shows."""
        row = "" if self.row_key is None else f" ({row_label(self.row_key)})"
        return f"{self.index}. {self.kind.value.upper()} {self.table}{row}"

    @property
    def params(self) -> tuple[SqlParam, ...]:
        return self.statement.params

    def text(self, mode: SqlMode) -> str:
        """This statement in ``mode``'s rendering.

        ``SCRIPT`` falls back to the literal form: a single statement cannot be wrapped in a
        transaction, and showing it wrapped would be a lie about what a per-statement copy
        does. The *combined* script is :meth:`SqlPreview.script`.
        """
        if mode is SqlMode.PARAMETERIZED:
            return self.statement.sql_parametrized
        return self.statement.sql_literal

    def parameter_legend(self) -> str:
        """``@p0 = N'Germany'  (Name)`` lines for the parameterized mode (FR-5.2a).

        Each value is rendered through the dialect's ``literal``, so the legend shows the
        value in exactly the form the literal statement uses — not Python's ``repr``, which
        would render a string with double quotes and a ``bytes`` as ``b'\\x01'``, neither of
        which is what SQL Server receives. Without a dialect (a bare entry in a test) the
        value falls back to ``repr`` rather than being dropped.
        """
        lines: list[str] = []
        for param in self.params:
            if self.dialect is not None:
                try:
                    rendered = self.dialect.literal(param.value)
                except TypeError, ValueError:
                    rendered = repr(param.value)  # a value SQL cannot hold: still show it
            else:
                rendered = repr(param.value)
            lines.append(f"{param.name} = {rendered}  ({param.column})")
        return "\n".join(lines)


def entries_for(
    dialect: SqlDialect,
    table: Table,
    changes: Sequence[PendingChange],
    *,
    identity_insert: bool = False,
) -> tuple[SqlEntry, ...]:
    """The pending changes as ordered panel entries (FR-5.1).

    The order is the staging order — exactly the order Apply executes them in. The panel
    shows the plan, not a sorted summary of it.
    """
    statements = build_statements(
        dialect,
        table,
        changes,
        options=ApplyOptions(identity_insert=identity_insert),
    )
    return tuple(
        SqlEntry(
            index=position,
            kind=statement.kind,
            table=statement.table,
            row_key=statement.row_key,
            statement=statement,
            dialect=dialect,
        )
        for position, statement in enumerate(statements, start=1)
    )


def generate_for(
    dialect: SqlDialect,
    table: Table,
    action: RowAction,
    *,
    key: RowKey | None = None,
    values: Mapping[str, object] | None = None,
    rows: Sequence[Mapping[str, object]] = (),
    identity_insert: bool = False,
) -> str:
    """Render one ad-hoc statement for a row or the current filter (FR-5.6).

    Returns the statement text; raises ``ValueError`` when the request cannot be satisfied
    (no key for an action that needs one, no rows for one that covers the table), so the UI
    can say *why* instead of showing an empty panel.
    """
    if action is RowAction.SELECT:
        if key is None:
            raise ValueError("SELECT needs a row key")
        return build_row_select(dialect, table, key)
    if action is RowAction.INSERT:
        if values is None:
            raise ValueError("INSERT needs the row's values")
        return build_table_insert(dialect, table, [values], identity_insert=identity_insert)
    if action is RowAction.INSERT_SCRIPT:
        if not rows:
            raise ValueError("no rows to insert — the table or filter is empty")
        return build_table_insert(dialect, table, rows, identity_insert=identity_insert)
    if action is RowAction.MERGE:
        if not table.identity_columns and not rows:
            raise ValueError(
                f"MERGE needs a key: {table.ref} has no primary key, so its rows cannot be matched"
            )
        source = list(rows) if rows else ([values] if values is not None else [])
        if not source:
            raise ValueError("MERGE needs at least one row's values")
        return build_merge(dialect, table, source, identity_insert=identity_insert)
    # UPDATE / DELETE address exactly one row by its key.
    if key is None:
        raise ValueError(f"{action.value.upper()} needs a row key")
    if action is RowAction.DELETE:
        # A generated DELETE has no "before" of its own. The row's values are passed only
        # because the builder requires them; with ``compare_original`` off and no rowversion
        # the key alone scopes the statement, so they never reach the SQL.
        return build_delete(
            dialect,
            table,
            PendingChange(ChangeKind.DELETE, table.ref, key=key, before=dict(values or {})),
        ).sql_literal
    if values is None:
        raise ValueError("UPDATE needs the row's values")
    return build_row_update(dialect, table, key, values)


@dataclass(frozen=True, slots=True)
class SqlPreview:
    """Everything the SQL panel renders, in every mode (FR-5).

    One immutable value holding the entries, the mode and the table they belong to. The
    panel calls :meth:`body` to get the text for the current mode and :meth:`script` for the
    combined transaction script, so "what the user copies" is decided here rather than in
    the widget — a widget bug cannot then produce a copy that differs from what is shown.
    """

    #: ``None`` before the table metadata has loaded; the panel then shows nothing at all.
    table: Table | None
    dialect: SqlDialect
    entries: tuple[SqlEntry, ...] = ()
    mode: SqlMode = SqlMode.PARAMETERIZED
    identity_insert: bool = False
    #: Free-form statement shown instead of the entries (the "generate SQL for…" actions).
    generated: str | None = None
    generated_title: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.entries and not self.generated

    def with_mode(self, mode: SqlMode) -> SqlPreview:
        """The same preview in another rendering (immutable, so the panel can hold both)."""
        return replace(self, mode=mode)

    def with_generated(self, sql: str, title: str) -> SqlPreview:
        """A preview of one ad-hoc statement, replacing the change list."""
        return replace(self, generated=sql or None, generated_title=title)

    def body(self) -> str:
        """The text the panel shows, in :attr:`mode`."""
        if self.generated is not None:
            return self.generated
        if self.is_empty:
            # Checked before the script mode: an empty change set has no script either, so
            # asking for one would replace an explanation with a blank box.
            return EMPTY_HINT
        if self.mode is SqlMode.SCRIPT:
            return self.script()
        return "\n\n".join(entry.text(self.mode) for entry in self.entries)

    def script(self) -> str:
        """The whole change set as one transaction script (FR-5.3).

        Falls back to the generated statement when the panel is showing one, so "copy
        script" always copies the thing the user is looking at.
        """
        if self.generated is not None:
            return self.generated
        return build_script(
            self.dialect,
            self.table,
            [entry.statement for entry in self.entries],
            identity_insert=self.identity_insert,
        )

    def parameters(self) -> str:
        """The parameter legend for the current mode (empty unless parameterized)."""
        if self.mode is not SqlMode.PARAMETERIZED or self.generated is not None:
            return ""
        return "\n\n".join(entry.parameter_legend() for entry in self.entries if entry.params)

    def entry(self, index: int) -> SqlEntry | None:
        """The entry at the 1-based ``index`` shown in the statement list."""
        if 1 <= index <= len(self.entries):
            return self.entries[index - 1]
        return None

    def summary(self) -> str:
        """``3 statements · 1 insert, 1 update, 1 delete`` for the panel header (FR-7.2)."""
        if self.generated is not None:
            return self.generated_title or "generated statement"
        if not self.entries:
            return "nothing staged"
        counts = dict.fromkeys(ChangeKind, 0)
        for entry in self.entries:
            counts[entry.kind] += 1
        parts = [
            f"{counts[kind]} {kind.value}{'s' if counts[kind] != 1 else ''}"
            for kind in (ChangeKind.INSERT, ChangeKind.UPDATE, ChangeKind.DELETE)
            if counts[kind]
        ]
        noun = "statement" if len(self.entries) == 1 else "statements"
        return f"{len(self.entries)} {noun}" + (f" · {', '.join(parts)}" if parts else "")

    def copy_all(self) -> str:
        """What "copy the whole script" puts on the clipboard.

        Always the **script** rendering, whatever mode is displayed: the clipboard target is
        the thing that can actually be run, and a fragment of it would be a trap.
        """
        return self.script()

    def copy_entry(self, index: int) -> str:
        """What "copy the selected statement" puts on the clipboard.

        Parameterized mode copies the parameterized text *and* the legend — a bare
        ``@p0`` with no values is not a statement anyone can use.
        """
        entry = self.entry(index)
        if entry is None:
            return ""
        text = entry.text(self.mode)
        if self.mode is SqlMode.PARAMETERIZED and entry.params:
            return f"{text}\n-- parameters\n{entry.parameter_legend()}"
        return text


#: Shown when nothing is staged: an explanation beats an empty panel the user must decode.
EMPTY_HINT = "no pending changes — stage an edit, or use “generate SQL for…”"

#: Shown in the panel's header: the panel is a preview, and saying so is more honest than
#: letting the user wonder whether looking at SQL might write something (S-1).
PREVIEW_ONLY_NOTE = "preview only — nothing is executed from here"
