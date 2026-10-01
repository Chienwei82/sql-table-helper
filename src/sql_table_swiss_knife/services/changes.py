"""Staging service: the bridge between the grid and the SQL that Apply will run (FR-7).

Fetching and staging live in different places on purpose:

* :class:`~sql_table_swiss_knife.services.data.DataService` owns the *read* path (paging,
  sort, filter) and never mutates anything;
* this module owns the **staging area** (:class:`~domain.changes.ChangeSet`) plus the
  Apply call. The database is only ever written by :meth:`ChangeService.apply`.

Four facts shape the design:

**Editing is staged, never immediate.** Every user action changes the in-memory
:class:`ChangeSet` and nothing else; the counts the status bar shows come straight from
it. The database is written by exactly one call — :meth:`ChangeService.apply` — inside a
transaction that rolls back completely on any failure (FR-7.6).

**Server-managed columns are never written.** Identity columns are omitted from an INSERT
unless the caller explicitly opted into ``IDENTITY_INSERT``; computed and rowversion
columns are never written at all. Staging refuses them up front (S-3) rather than letting
the statement builder fail later.

**Optimistic concurrency is per row.** A table with a rowversion column puts the old
rowversion in the WHERE clause; a table without one can compare the original value of
every column the edit did not touch. Either way, 0 rows affected means "somebody else got
there first", and that is reported per row instead of aborting the UI (FR-7.8).

**Errors are mapped, not dumped.** :func:`map_database_error` turns a driver message into
the row and column the database actually complained about, so the UI can point at the
offending cell.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ..domain.catalog import Column, Table
from ..domain.changes import (
    ChangeKind,
    ChangeSet,
    PendingChange,
    StagedRow,
    row_label,
)
from ..domain.rows import RowKey
from ..providers import ApplyOptions, ExecuteResult, RowConflict, SqlStatement, build_statements
from ..providers.dialect import ErrorFacts, SqlDialect
from ..providers.errors import QueryError
from .clipboard import PastePlan, RowOutcome
from .connection import ConnectionService
from .validation import Hint, HintLevel, validate_input

__all__ = [
    "IDENTITY_INSERT_WARNING",
    "ApplyError",
    "CellStatus",
    "ChangeService",
    "IdentityInsertWarning",
    "MappedError",
    "map_database_error",
]

#: Shown before an ``IDENTITY_INSERT`` Apply, where the user must confirm (S-3).
IDENTITY_INSERT_WARNING = (
    "IDENTITY_INSERT writes explicit identity values. This locks the table for other "
    "writers and can leave the identity counter out of step with the rows present. "
    "Continue?"
)


class IdentityInsertWarning(UserWarning):
    """The explicit, confirmed opt-in that allows writing identity values (S-3)."""


class CellStatus(Enum):
    """What a cell looks like in the grid given the staging area (FR-3.7).

    Lives here rather than in the grid widget so the *rules* are testable without
    Textual: the widget only maps these onto CSS classes and glyphs.
    """

    UNCHANGED = "unchanged"
    MODIFIED = "modified"
    NEW = "new"
    DELETED = "deleted"
    READ_ONLY = "readonly"


@dataclass(frozen=True, slots=True)
class MappedError:
    """A database error attributed to a row and, when possible, a column.

    The raw driver message stays available in ``message``; the extra fields are what
    let the UI highlight the offending cell instead of only showing a toast.
    """

    message: str
    row_key: RowKey | None = None
    column: str | None = None
    constraint: str | None = None
    kind: str = "error"

    def describe(self) -> str:
        """One line naming the row, the column and the constraint, when known."""
        parts: list[str] = []
        if self.row_key is not None:
            parts.append(f"row {row_label(self.row_key)}")
        if self.column is not None:
            parts.append(f"column {self.column}")
        if self.constraint is not None:
            parts.append(f"constraint {self.constraint}")
        where = " · ".join(parts)
        return f"{self.message} ({where})" if where else self.message


def map_database_error(
    message: str,
    table: Table,
    change: PendingChange | None = None,
    dialect: SqlDialect | None = None,
) -> MappedError:
    """Attribute a database error to a row/column of ``table``.

    The row comes from the failing :class:`PendingChange` (the Apply result knows which
    statement failed). Classifying the *message* is the dialect's job — which wording means
    "duplicate key" is vendor-specific — so this only asks, and supplies the fallback: the
    first column the statement would have written, which is the best guess the change
    itself allows and is labelled as such by the caller.

    Nothing here raises: a message that matches nothing is still a usable
    :class:`MappedError`, just without a column. A missing dialect degrades to the raw
    message rather than failing, so a caller without a connection still gets a result.
    """
    facts = dialect.inspect_error(message) if dialect is not None else ErrorFacts()
    column_name = facts.column or _first_written_column(change)
    return MappedError(
        message=facts.hint or message,
        row_key=change.key if change is not None else None,
        column=column_name,
        constraint=facts.constraint,
        kind=facts.kind,
    )


def _first_written_column(change: PendingChange | None) -> str | None:
    """The first column the failing statement writes, as the best-guess column."""
    if change is None:
        return None
    if change.kind is ChangeKind.DELETE:
        return None
    if change.kind is ChangeKind.INSERT:
        return next(iter(change.after or {}), None)
    try:
        changed = change.changed_columns
    except ValueError:
        return None  # a malformed change has no changed_columns
    return changed[0] if changed else None


class ApplyError(Exception):
    """Apply failed; nothing was committed and the staging area is untouched (FR-7.6).

    Carries the row and column the database complained about, so the UI can point at the
    offending cell instead of dumping a raw driver message.
    """

    def __init__(
        self, message: str, result: ExecuteResult, *, mapped: MappedError | None = None
    ) -> None:
        super().__init__(message)
        self.result = result
        self.mapped = mapped

    @property
    def message(self) -> str:
        return str(self)

    @property
    def conflicts(self) -> tuple[RowConflict, ...]:
        """Per-row concurrency conflicts detected during this Apply (FR-7.8)."""
        return self.result.conflicts

    @property
    def failed_change(self) -> PendingChange | None:
        """The staged change whose statement failed, when it can be identified."""
        index = self.result.failed_index
        if index is None or index >= len(self.result.results):
            return None
        return self.result.results[index].change


@dataclass(frozen=True, slots=True)
class StagedEdit:
    """The outcome of staging one cell, for the UI to report back to the user."""

    ok: bool
    hints: tuple[Hint, ...] = ()
    #: The row's staged change; ``None`` means the edit unstaged the row entirely.
    change: StagedRow | None = None

    @property
    def message(self) -> str:
        return self.hints[0].text if self.hints else ""


class ChangeService:
    """Staging area plus Apply for one table (FR-7).

    Owns a :class:`~domain.changes.ChangeSet` and the validation rules that decide what
    may be staged at all. The TUI never builds a :class:`PendingChange` itself: it calls
    :meth:`edit_cell` / :meth:`insert_row` / :meth:`delete_row` and renders what comes
    back, which is why the "can this be staged?" rules can be tested without a terminal.
    """

    def __init__(
        self,
        connection: ConnectionService,
        table: Table,
        *,
        identity_insert: bool = False,
        compare_original_values: bool = False,
    ) -> None:
        self._connection = connection
        self._table = table
        self._identity_insert = identity_insert
        self._compare_original = compare_original_values
        self.changes = ChangeSet(table.ref)

    # -- state --------------------------------------------------------------

    @property
    def table(self) -> Table:
        return self._table

    def rebind(self, table: Table) -> bool:
        """Point this service at refreshed metadata, keeping the staged changes.

        Reloading a table (a manual ``r``, or a reconnect after a dropped link) must not
        cost the user their work: the staged set is keyed by the table's *ref*, which a
        refresh does not change, so the buffer survives while the metadata, the
        validation rules and the identity columns are replaced with the fresh copy.

        Returns:
            True when the buffer was carried over, False when the refresh turned out to
            describe a *different* table — in which case the staged changes belonged to
            something else and are dropped rather than applied to the wrong rows.
        """
        if table.ref != self._table.ref:
            return False
        self._table = table
        return True

    @property
    def is_empty(self) -> bool:
        return self.changes.is_empty

    @property
    def summary(self) -> str:
        """``2 inserts, 1 update, 3 deletes`` for the status bar (FR-7.2)."""
        return self.changes.summary

    @property
    def counts(self) -> dict[ChangeKind, int]:
        return self.changes.counts

    @property
    def can_undo(self) -> bool:
        return self.changes.can_undo

    @property
    def can_redo(self) -> bool:
        return self.changes.can_redo

    @property
    def options(self) -> ApplyOptions:
        """The apply options this service was configured with."""
        return ApplyOptions(
            identity_insert=self._identity_insert,
            compare_original_values=self._compare_original,
        )

    def identity_columns(self) -> tuple[Column, ...]:
        """Identity columns, which an INSERT may only write in ``IDENTITY_INSERT`` mode."""
        return tuple(column for column in self._table.columns if column.is_identity)

    def requires_identity_insert(self) -> bool:
        """True when a staged INSERT carries an identity value that needs the opt-in."""
        if not self._identity_insert:
            return False
        identity_names = {column.name for column in self.identity_columns()}
        return any(set(change.after or {}) & identity_names for change in self.changes.inserts())

    def status_for(self, key: RowKey, column: str) -> CellStatus:
        """The cell's visual state given the staging area (FR-3.7).

        Read-only wins over every other state, so a server-managed column never *looks*
        editable just because the row is staged (S-3).
        """
        metadata = self._table.column_or_none(column)
        if metadata is None or metadata.is_server_managed or not self._table.updatable:
            return CellStatus.READ_ONLY
        staged = self.changes.find(key)
        if staged is None:
            return CellStatus.UNCHANGED
        change = staged.change
        if staged.is_new:
            return CellStatus.NEW
        match change.kind:
            case ChangeKind.DELETE:
                return CellStatus.DELETED
            case ChangeKind.UPDATE:
                after = change.after or {}
                before = change.before or {}
                return (
                    CellStatus.MODIFIED
                    if after.get(column) != before.get(column)
                    else CellStatus.UNCHANGED
                )
            case _:  # pragma: no cover - INSERT already handled by is_new
                return CellStatus.NEW

    # -- staging ------------------------------------------------------------

    def edit_cell(
        self,
        key: RowKey,
        column_name: str,
        text: str,
        original: Mapping[str, object],
    ) -> StagedEdit:
        """Validate ``text`` for ``column_name`` and stage it against ``key``.

        The typed text goes through :func:`~services.validation.validate_input` first, so
        a value that cannot be represented in the column's type (or would violate a
        NOT NULL rule) is refused *before* it is staged (FR-3.5). Nothing reaches the
        database either way; staging is still only in memory.
        """
        column = self._table.column_or_none(column_name)
        if column is None:
            return StagedEdit(ok=False, hints=self._readonly_hints(column_name))
        if column.is_identity and not self._identity_insert:
            # S-3 + the identity opt-in: the server assigns these, unless the user
            # explicitly confirmed IDENTITY_INSERT.
            return StagedEdit(ok=False, hints=self._readonly_hints(column_name))
        parsed = validate_input(self._table, column, text)
        if not parsed.ok:
            return StagedEdit(ok=False, hints=parsed.hints)
        change = self.changes.stage_cell(key, column.name, parsed.value, dict(original))
        return StagedEdit(ok=True, hints=parsed.hints, change=change)

    def stage_value(
        self,
        key: RowKey,
        column_name: str,
        value: object,
        original: Mapping[str, object],
    ) -> StagedEdit:
        """Stage an already-parsed value (a picker choice, a pasted value).

        Skips the text parser — the value came from somewhere that already knows its
        type — but applies the same read-only and identity rules.
        """
        column = self._table.column_or_none(column_name)
        if column is None or (column.is_identity and not self._identity_insert):
            return StagedEdit(ok=False, hints=self._readonly_hints(column_name))
        change = self.changes.stage_cell(key, column.name, value, dict(original))
        return StagedEdit(ok=True, change=change)

    def insert_row(self, values: Mapping[str, object] | None = None) -> RowKey:
        """Stage a new row and return its synthetic key (FR-7.4).

        Only the columns the user can legitimately supply are staged: server-managed
        columns are dropped here, so the INSERT never mentions them.
        """
        staged = self._insertable_values(values or {})
        return self.changes.stage_insert(staged)

    def fill_new_row(self, key: RowKey, column_name: str, value: object | None) -> StagedEdit:
        """Stage one cell of a brand new row.

        Separate from :meth:`edit_cell` because a new row has no *original* values: the
        caller passes the new value directly (already parsed, or a picker's choice) and
        there is nothing to revert to.
        """
        staged = self.changes.find(key)
        if staged is None or not staged.is_new:
            return StagedEdit(ok=False, hints=self._readonly_hints(column_name))
        column = self._table.column_or_none(column_name)
        if column is None or (column.is_identity and not self._identity_insert):
            return StagedEdit(ok=False, hints=self._readonly_hints(column_name))
        change = self.changes.stage_cell(key, column.name, value, {})
        return StagedEdit(ok=True, change=change)

    def duplicate_row(self, key: RowKey, original: Mapping[str, object]) -> RowKey:
        """Stage a copy of an existing row (FR-7.4).

        Key and server-managed columns are dropped, so the copy needs its own key typed in
        (or an identity value, when ``IDENTITY_INSERT`` is confirmed) before it can be
        applied — copying a primary key would violate it on the way in.
        """
        identity = set(self._table.identity_columns)
        copied = {
            name: value
            for name, value in self._insertable_values(original).items()
            if name not in identity
        }
        return self.changes.stage_duplicate(copied)

    def delete_row(self, key: RowKey, original: Mapping[str, object]) -> None:
        """Mark a row for deletion (FR-7.4)."""
        self.changes.stage_delete(key, dict(original))

    def find(self, key: RowKey) -> StagedRow | None:
        """The staged row for ``key``, or ``None`` (alias of :meth:`pending_for`)."""
        return self.changes.find(key)

    def inserts(self) -> tuple[PendingChange, ...]:
        """The staged INSERTs, in staging order."""
        return self.changes.inserts()

    def updates(self) -> tuple[PendingChange, ...]:
        """The staged UPDATEs, in staging order."""
        return self.changes.updates()

    def deletes(self) -> tuple[PendingChange, ...]:
        """The staged DELETEs, in staging order."""
        return self.changes.deletes()

    def revert(self, key: RowKey) -> bool:
        """Drop every staged change for one row (``ctrl+u``)."""
        return self.changes.revert(key)

    def staged_rows(self) -> tuple[StagedRow, ...]:
        """Every staged row, in staging order (what the grid overlay walks)."""
        return tuple(self.changes)

    def revert_all(self) -> None:
        """Discard every staged change (``ctrl+shift+z``)."""
        self.changes.clear()

    def undo(self) -> bool:
        """Undo the last staging action."""
        return self.changes.undo()

    def redo(self) -> bool:
        """Redo the last undone staging action."""
        return self.changes.redo()

    def stage_paste(self, plan: PastePlan, rows: Sequence[Mapping[str, object]]) -> StagedEdit:
        """Stage every cell of a reviewed :class:`~services.clipboard.PastePlan` (FR-4.6).

        This is the *only* way a paste reaches the staging area, which is what keeps the
        promise that nothing is written except through Apply, and that nothing is staged
        before the user has seen the preview: the plan arrives already converted and
        validated, and a plan with any error is refused outright rather than staged
        half-parsed.

        Args:
            plan: The confirmed plan from :func:`~services.clipboard.plan_paste`.
            rows: The fetched/original values of the target rows, in grid order, used as
                the ``before`` image of an UPDATE.

        Returns:
            A :class:`StagedEdit`; ``ok`` is False when the plan was refused. On success
            ``hints`` carries one informational line per kind of change, so the UI can say
            what happened without counting the change set itself.
        """
        if plan.errors:
            first = plan.errors[0]
            return StagedEdit(ok=False, hints=(Hint(HintLevel.ERROR, first),))
        notes: list[Hint] = []
        # One batch for the whole paste: the cells collapse into the same rows either way,
        # but per-cell snapshotting made the cost quadratic in the paste size and left the
        # paste as one undo step per cell.
        with self.changes.batch():
            for row_plan in plan.rows:
                if row_plan.outcome is RowOutcome.INSERT:
                    values = {cell.column: cell.value for cell in row_plan.values}
                    self.insert_row(values)
                    continue
                if row_plan.key is None or row_plan.target_row is None:
                    # A FILL/CELL plan onto a row with no identity cannot be staged safely
                    # (S-4): there is no WHERE clause that could name the row.
                    name = row_plan.values[0].column if row_plan.values else "cell"
                    notes.append(
                        Hint(
                            HintLevel.WARNING,
                            f"row {row_plan.index + 1} ({name}) has no usable key — skipped",
                        )
                    )
                    continue
                key = row_plan.key
                index = row_plan.target_row
                original = dict(rows[index]) if 0 <= index < len(rows) else {}
                staged = self.changes.find(key)
                is_new = staged is not None and staged.is_new
                for cell in row_plan.values:
                    if is_new:
                        self.fill_new_row(key, cell.column, cell.value)
                    else:
                        self.stage_value(key, cell.column, cell.value, original)
        updates, inserts = len(plan.updates), len(plan.inserts)
        if plan.rows:
            notes.append(
                Hint(
                    HintLevel.INFO,
                    f"pasted {plan.cell_count} cell(s): {updates} row(s) updated, "
                    f"{inserts} row(s) inserted",
                )
            )
        return StagedEdit(ok=True, hints=tuple(notes))

    def _insertable_values(self, values: Mapping[str, object]) -> dict[str, object]:
        """Drop server-managed columns (and unknown names) from a value map."""
        return {
            name: value
            for name, value in values.items()
            if (column := self._table.column_or_none(name)) is not None
            and (not column.is_server_managed or (column.is_identity and self._identity_insert))
        }

    def _readonly_hints(self, column_name: str) -> tuple[Hint, ...]:
        """The hint shown when a cell may not be staged (S-3)."""
        return (Hint(HintLevel.ERROR, f"{column_name} is read-only and cannot be staged"),)

    # -- preview ------------------------------------------------------------

    def statements(self) -> list[SqlStatement]:
        """The SQL that Apply would run, in apply order — the preview (FR-5.1).

        Built from the *same* builder the provider executes with, so the preview can
        never disagree with what runs (FR-5.4).
        """
        provider = self._connection.provider()
        return build_statements(
            provider.dialect,
            self._table,
            self.changes.changes,
            options=self.options,
        )

    def preview_script(self) -> str:
        """The full transaction script, copy-ready for the SQL preview panel."""
        provider = self._connection.provider()
        dialect = provider.dialect
        lines = [dialect.begin_transaction()]
        for statement in build_statements(
            dialect, self._table, self.changes.changes, options=self.options
        ):
            lines.append(statement.sql_script)
        lines.append(dialect.commit_transaction())
        return ";\n".join(lines) + ";"

    # -- apply --------------------------------------------------------------

    async def apply(self) -> ExecuteResult:
        """Run every staged change in one transaction, then clear the staging area.

        This is the only method in the app that writes to the database. On success the
        staging area is cleared and the caller should re-fetch the rows (identity values
        are generated by the server, so the grid cannot know them in advance). On failure
        the staging area is deliberately **kept** — the user's work is not thrown away
        because the database said no — and an :class:`ApplyError` is raised carrying the
        row/column the error was mapped to (FR-7.6, FR-7.8).

        Raises:
            ApplyError: the transaction rolled back; nothing was applied.
        """
        if self.changes.is_empty:
            raise ValueError("nothing staged: there is nothing to apply")
        if not self._table.updatable:
            raise ApplyError(
                f"{self._table.ref} rows are read-only (no usable key) — nothing was applied",
                ExecuteResult(committed=False, results=(), duration_ms=0, failed_index=0),
            )
        provider = self._connection.provider()
        try:
            result = await provider.execute_changes(
                self._connection.connection(),
                self._table,
                self.changes.changes,
                self.options,
            )
        except QueryError as exc:
            # A connection-level or statement-build error: nothing ran, keep staging.
            mapped = map_database_error(str(exc), self._table, None, self._dialect())
            empty = ExecuteResult(committed=False, results=(), duration_ms=0, failed_index=0)
            raise ApplyError(mapped.describe(), empty, mapped=mapped) from exc
        if not result.committed:
            change = self._failed_change(result)
            mapped = map_database_error(
                result.error or "apply failed", self._table, change, self._dialect()
            )
            raise ApplyError(mapped.describe(), result, mapped=mapped)
        self.changes.clear()
        return result

    def _dialect(self) -> SqlDialect | None:
        """The connected provider's dialect, or ``None`` when there is no session."""
        try:
            return self._connection.provider().dialect
        except RuntimeError, LookupError:
            return None

    def _failed_change(self, result: ExecuteResult) -> PendingChange | None:
        """The staged change the provider reported as failing, when it named one."""
        if result.failed_index is None or result.failed_index >= len(result.results):
            return None
        return result.results[result.failed_index].change

    # -- diagnostics --------------------------------------------------------

    def pending_for(self, key: RowKey) -> StagedRow | None:
        """The staged change for ``key``, or ``None``."""
        return self.changes.find(key)

    def is_staged(self, key: RowKey) -> bool:
        return self.changes.find(key) is not None

    def display_values(self, key: RowKey, original: Mapping[str, object]) -> Mapping[str, object]:
        """What the grid should show for a row: staged values over the fetched ones."""
        return self.changes.values_for(key, original)

    def new_row_keys(self) -> tuple[RowKey, ...]:
        """Synthetic keys of the staged INSERT rows, in staging order."""
        return tuple(staged.key for staged in self.changes if staged.is_new)

    def conflict_message(self, conflict: RowConflict) -> str:
        """User-facing text for one concurrency conflict (FR-7.8)."""
        action = "update" if conflict.is_update else "delete"
        return (
            f"Cannot {action} {row_label(conflict.row_key)}: {conflict.reason}. "
            f"Somebody else changed it — refresh the table and stage the {action} again."
        )
