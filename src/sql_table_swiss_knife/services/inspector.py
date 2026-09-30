"""Inspector model: what the schema panel shows, derived from table metadata (FR-6).

Everything here is a **pure function over the metadata model** (``domain.catalog``): no
I/O, no driver types, no Textual. The TUI widget renders what these functions return,
which makes the whole schema-inspector behaviour testable without a database (NFR-5)
and impossible to drift from the metadata it describes.

Three products:

* :func:`build_warnings` — the safety banner (severity-ordered): triggers, missing primary
  key, temporal tables, server-managed columns, collations, CHECK constraints, incoming
  foreign keys. The wording is deliberately explicit about what the app cannot guarantee.
* :func:`column_badges` / :func:`header_label` — the glyph row of a column (colour is
  never the only signal, FR-3.7).
* :func:`column_detail` / :func:`table_summary_rows` — the fact lists of the two
  inspector sections (FR-6.1, FR-6.2).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

from ..domain.catalog import (
    Column,
    ForeignKey,
    IncomingForeignKey,
    Table,
    Trigger,
    UniqueConstraint,
)

__all__ = [
    "LARGE_TABLE_ROWS",
    "SEVERITIES",
    "ColumnBadge",
    "ColumnDetail",
    "InspectorService",
    "Severity",
    "Warning",
    "WarningCode",
    "build_warnings",
    "checks_for",
    "column_badges",
    "column_detail",
    "fks_for",
    "format_cell_value",
    "format_data_type",
    "header_label",
    "incoming_referencing_tables",
    "pk_ordinal",
    "read_only_reason",
    "table_summary_rows",
    "type_hint",
    "uniques_for",
]

#: Row count above which the table gets an advisory banner (S-8, OQ-7).
LARGE_TABLE_ROWS = 50_000


class Severity(IntEnum):
    """How loudly a :class:`Warning` is shown — drives colour *and* banner order.

    Ordered so the worst thing about a table is at the top: ``ERROR`` is a
    behaviour-changing fact (an ``INSTEAD OF`` trigger, an unusable row identity),
    ``WARNING`` can make a statement fail or write to other tables, ``INFO`` is context.
    """

    INFO = 0
    WARNING = 1
    ERROR = 2


#: Worst first — the banner renders in this order.
SEVERITIES: tuple[Severity, ...] = (Severity.ERROR, Severity.WARNING, Severity.INFO)


class WarningCode(IntEnum):
    """Stable identifier per warning kind — tests assert on these, not on wording."""

    INSTEAD_OF_TRIGGER = 0
    TRIGGER = 1
    TRIGGER_DISABLED = 2
    NO_PRIMARY_KEY = 3
    SYSTEM_VERSIONED = 4
    HISTORY_TABLE = 5
    COMPUTED_COLUMNS = 6
    ROWVERSION_COLUMNS = 7
    COLLATED_COLUMN = 8
    CHECK_CONSTRAINT = 9
    INCOMING_FOREIGN_KEYS = 10
    VIEW = 11
    LARGE_TABLE = 12


@dataclass(frozen=True, slots=True)
class Warning:
    """One entry of the warnings banner.

    ``notes`` carries the per-item detail (one line per trigger/FK) so the banner stays a
    compact block while still naming every object it talks about.
    """

    code: WarningCode
    severity: Severity
    title: str
    message: str
    notes: tuple[str, ...] = ()
    #: Disabled triggers are listed but dimmed — they cannot fire.
    dimmed: bool = False

    @property
    def glyph(self) -> str:
        """Leading marker: the severity is legible without colour (FR-3.7)."""
        return {Severity.ERROR: "⛔", Severity.WARNING: "⚠", Severity.INFO: "•"}[self.severity]


@dataclass(frozen=True, slots=True)
class ColumnBadge:
    """One glyph of a column's badge row (same idea as the table-picker badges)."""

    glyph: str
    label: str
    #: CSS colour name — a *semantic role*, never a literal colour.
    role: str


# -- column facts ------------------------------------------------------------


def pk_ordinal(table: Table, column: Column) -> int | None:
    """1-based position of ``column`` in the primary key, or ``None`` when absent."""
    pk = table.primary_key
    if pk is None or column.name not in pk.columns:
        return None
    return pk.columns.index(column.name) + 1


def fks_for(table: Table, column_name: str) -> tuple[ForeignKey, ...]:
    """Outgoing foreign keys that involve ``column_name``."""
    return tuple(fk for fk in table.foreign_keys if column_name in fk.columns)


def uniques_for(table: Table, column_name: str) -> tuple[UniqueConstraint, ...]:
    """Unique constraints that involve ``column_name``."""
    return tuple(uq for uq in table.unique_constraints if column_name in uq.columns)


#: Identifier-shaped tokens inside a CHECK definition (quoted or bare).
_IDENTIFIER = re.compile(r"\[[^\[\]]+\]|[A-Za-z_][A-Za-z0-9_$#@]*")


def _identifiers(definition: str) -> frozenset[str]:
    """Every identifier mentioned by a CHECK expression, lowercased and unquoted.

    ``([Population]>=(0))`` yields ``{population}`` — the literal ``0`` is not an identifier,
    and a bracket-quoted name loses its brackets, so ``[Name]`` matches the column ``Name``.
    """
    found: set[str] = set()
    for token in _IDENTIFIER.findall(definition):
        cleaned = token.strip("[]").lower()
        if cleaned:
            found.add(cleaned)
    return frozenset(found)


def _mentions(definition: str, column_name: str) -> bool:
    """True when a CHECK definition textually refers to ``column_name``.

    The app never evaluates a CHECK expression (that would be a second SQL engine); this
    identifier match only *attributes* the constraint to a column so the inspector can
    show it in the column detail too.
    """
    return column_name.lower() in _identifiers(definition)


def checks_for(table: Table, column_name: str) -> tuple[str, ...]:
    """Definitions of the CHECK constraints that mention ``column_name``."""
    return tuple(
        check.definition
        for check in table.check_constraints
        if _mentions(check.definition, column_name)
    )


def column_badges(table: Table, column: Column) -> tuple[ColumnBadge, ...]:
    """The badge row of one column, in the fixed order the inspector renders.

    Order is stable and testable: key/server-managed facts first, then value rules.
    """
    badges: list[ColumnBadge] = []
    ordinal = pk_ordinal(table, column)
    if ordinal is not None:
        label = "primary key" if ordinal == 1 else f"primary key #{ordinal}"
        badges.append(ColumnBadge("🔑", label, "pk"))
    for fk in fks_for(table, column.name):
        target = f"{fk.referenced_schema}.{fk.referenced_table}." + ".".join(fk.referenced_columns)
        badges.append(ColumnBadge("🔗", f"→ {target}", "fk"))
    if column.is_identity:
        badges.append(ColumnBadge("#", "identity", "identity"))
    if column.is_computed:
        badges.append(ColumnBadge("ƒ", "computed", "computed"))
    if column.is_rowversion:
        badges.append(ColumnBadge("⏱", "rowversion", "computed"))
    badges.append(
        ColumnBadge("∅", "nullable", "nullable")
        if column.nullable
        else ColumnBadge("✱", "required (NOT NULL)", "warning")
    )
    if column.default_definition is not None:
        badges.append(ColumnBadge("D", f"default {column.default_definition}", "pending"))
    for uq in uniques_for(table, column.name):
        badges.append(ColumnBadge("U", f"unique {uq.name}", "pk"))
    for check in checks_for(table, column.name):
        badges.append(ColumnBadge("✓", f"check {check}", "warning"))
    return tuple(badges)


def format_data_type(column: Column) -> str:
    """The *exact* declared type, e.g. ``nvarchar(50)`` or ``decimal(10,2)``.

    ``sys.columns.max_length`` counts bytes, so character types declaring a length in
    characters (``nvarchar``/``nchar``) are halved; ``decimal``/``numeric`` render
    ``(precision,scale)`` and ``float`` its precision in bits.
    """
    kind = column.data_type
    if kind in {"nvarchar", "nchar"} and column.max_length is not None:
        return f"{kind}({max(1, column.max_length // 2)})"
    if kind in {"char", "varchar", "binary", "varbinary"} and column.max_length is not None:
        return f"{kind}({column.max_length})"
    if kind in {"decimal", "numeric"} and column.precision is not None:
        return f"{kind}({column.precision},{column.scale or 0})"
    if kind == "float" and column.precision is not None:
        return f"{kind}({column.precision})"
    return kind


def type_hint(column: Column) -> str:
    """Compact type for the column list and the grid header."""
    return format_data_type(column)


def header_label(table: Table, column: Column) -> str:
    """Column header text: name, badges, read-only marker and the compact type.

    The header stays compact (the grid is dense) but never drops meaning: a read-only
    column carries ``🔒`` and a required one ``✱``, so both facts survive a narrow
    terminal and any colour-vision difference (FR-3.1, FR-3.7).
    """
    parts = [column.name]
    glyphs = "".join(badge.glyph for badge in column_badges(table, column))
    if glyphs:
        parts.append(glyphs)
    lock = " 🔒" if column.is_server_managed else ""
    return f"{' '.join(parts)}{lock}  {type_hint(column)}"


def read_only_reason(column: Column) -> str | None:
    """Why the cell cannot be edited, or ``None`` when it can (S-3)."""
    if column.is_identity:
        return "identity column — the server assigns values"
    if column.is_computed:
        return "computed column — the server calculates values"
    if column.is_rowversion:
        return "rowversion column — the server bumps it on every change"
    return None


def incoming_referencing_tables(table: Table) -> tuple[str, ...]:
    """Schema-qualified names of the tables holding a FK to this one, deduplicated."""
    refs: dict[str, None] = {}
    for fk in table.incoming_foreign_keys:
        refs.setdefault(fk.ref, None)
    return tuple(refs)


def format_cell_value(value: object) -> str:
    """Render a fetched cell value the way the grid shows it (FR-3.1)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        preview = raw[:8].hex()
        return f"0x{preview}{'…' if len(raw) > 8 else ''} ({len(raw)} bytes)"
    return str(value)


def table_summary_rows(table: Table) -> tuple[tuple[str, str], ...]:
    """The TABLE SUMMARY section as ``(label, value)`` rows (FR-6.1)."""
    pk = table.primary_key
    referencing = incoming_referencing_tables(table)
    plural = len(referencing) != 1
    rows: list[tuple[str, str]] = [
        ("name", f"{table.schema}.{table.name}"),
        ("kind", "view" if table.is_view else "table"),
        (
            "rows",
            f"≈{table.approximate_row_count:,}" if table.approximate_row_count is not None else "?",
        ),
        ("primary key", ", ".join(pk.columns) if pk is not None else "none ⚠"),
        (
            "referenced by",
            f"{len(referencing)} table{'s' if plural else ''}" if referencing else "no tables",
        ),
        ("editable", "yes" if table.updatable else "no — rows are read-only (S-4)"),
    ]
    if table.is_system_versioned:
        rows.append(("temporal", f"history in {table.history_schema}.{table.history_table}"))
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class ColumnDetail:
    """Everything FR-6.2 asks for about one column, ready to render."""

    column: Column
    rows: tuple[tuple[str, str], ...]

    @property
    def type_text(self) -> str:
        """The exact declared type, e.g. ``decimal(10,2)``."""
        return format_data_type(self.column)

    @property
    def is_read_only(self) -> bool:
        """True for identity, computed and rowversion columns (S-3)."""
        return self.column.is_server_managed


def column_detail(table: Table, column: Column) -> ColumnDetail:
    """Detail rows for the focused column: type, rules, defaults, keys, constraints."""
    rows: list[tuple[str, str]] = [
        ("ordinal", str(column.ordinal)),
        ("type", format_data_type(column)),
        ("nullable", "yes" if column.nullable else "no (required)"),
    ]
    reason = read_only_reason(column)
    if reason is not None:
        rows.append(("read-only", reason))
    rows.append(("default", column.default_definition or "none"))
    if column.is_identity:
        seed = "?" if column.identity_seed is None else str(column.identity_seed)
        increment = "?" if column.identity_increment is None else str(column.identity_increment)
        rows.append(("identity", f"seed {seed}, increment {increment}"))
    if column.is_computed:
        persisted = "persisted" if column.computed_persisted else "not persisted"
        rows.append(("computed", f"{column.computed_definition or '?'} ({persisted})"))
    if column.is_rowversion:
        rows.append(("rowversion", "timestamp — changes on every update"))
    pk = table.primary_key
    ordinal = pk_ordinal(table, column)
    if pk is not None and ordinal is not None:
        rows.append(("primary key", f"#{ordinal} of {len(pk.columns)}"))
    uniques = uniques_for(table, column.name)
    if uniques:
        rows.append(("unique", ", ".join(uq.name for uq in uniques)))
    checks = checks_for(table, column.name)
    if checks:
        rows.append(("check", "; ".join(checks)))
    for fk in fks_for(table, column.name):
        target = f"{fk.referenced_schema}.{fk.referenced_table}." + ".".join(fk.referenced_columns)
        local = "+".join(fk.columns)
        rows.append(
            (
                "foreign key",
                f"{fk.name}: {local} → {target} "
                f"(on delete {fk.on_delete.value}, on update {fk.on_update.value})",
            )
        )
    rows.append(("collation", column.collation or "database default"))
    return ColumnDetail(column=column, rows=tuple(rows))


# -- warnings ----------------------------------------------------------------


def build_warnings(table: Table) -> tuple[Warning, ...]:
    """Every safety-relevant fact about ``table``, most severe first.

    The banner exists so the user learns *before* editing that a statement may behave
    unexpectedly: an ``INSTEAD OF`` trigger replaces the statement's own effect, a table
    without a usable key cannot be updated or deleted safely, and a cascade on an
    incoming foreign key writes to other tables. Ordering is severity-first, then by
    :class:`WarningCode`, so the list is deterministic and testable.
    """
    warnings: list[Warning] = [
        *_trigger_warnings(table.triggers),
        *_identity_warnings(table),
        *_temporal_warnings(table),
        *_column_warnings(table),
        *_constraint_warnings(table),
        *_incoming_fk_warnings(table),
        *_size_warnings(table),
    ]
    warnings.sort(key=lambda warning: (-int(warning.severity), int(warning.code)))
    return tuple(warnings)


def _trigger_warnings(triggers: Sequence[Trigger]) -> list[Warning]:
    """One warning per trigger: ``INSTEAD OF`` is an error, ``AFTER``/``FOR`` a warning."""
    out: list[Warning] = []
    for trigger in triggers:
        events = ", ".join(trigger.events)
        if trigger.firing == "INSTEAD OF":
            out.append(
                Warning(
                    code=WarningCode.INSTEAD_OF_TRIGGER,
                    severity=Severity.ERROR,
                    title=f"INSTEAD OF trigger on {events}",
                    message=(
                        f"{trigger.name} replaces the {events} statement: your "
                        "INSERT/UPDATE/DELETE may not do what you expect."
                    ),
                )
            )
        elif not trigger.enabled:
            out.append(
                Warning(
                    code=WarningCode.TRIGGER_DISABLED,
                    severity=Severity.INFO,
                    title=f"disabled {trigger.firing} trigger on {events}",
                    message=f"{trigger.name} is disabled — it will not fire.",
                    dimmed=True,
                )
            )
        else:
            out.append(
                Warning(
                    code=WarningCode.TRIGGER,
                    severity=Severity.WARNING,
                    title=f"{trigger.firing} trigger on {events}",
                    message=(
                        f"{trigger.name} runs on {events}; it can modify data or reject "
                        "the statement."
                    ),
                )
            )
    return out


def _identity_warnings(table: Table) -> list[Warning]:
    """No primary key: an error when no UNIQUE key can stand in, a warning otherwise."""
    if table.primary_key is not None:
        return []
    if table.updatable:
        return [
            Warning(
                code=WarningCode.NO_PRIMARY_KEY,
                severity=Severity.WARNING,
                title="no primary key",
                message=(
                    "row identity comes from a UNIQUE constraint; updates and deletes "
                    "are only possible through it."
                ),
            )
        ]
    return [
        Warning(
            code=WarningCode.NO_PRIMARY_KEY,
            severity=Severity.ERROR,
            title="no primary key",
            message=(
                "row identity is ambiguous; updates/deletes are blocked until you "
                "confirm them (S-4)."
            ),
        )
    ]


def _temporal_warnings(table: Table) -> list[Warning]:
    """Temporal facts: the versioned table and its history counterpart."""
    if table.is_system_versioned:
        return [
            Warning(
                code=WarningCode.SYSTEM_VERSIONED,
                severity=Severity.WARNING,
                title="system-versioned (temporal) table",
                message=(
                    "every change is versioned: rows are copied to "
                    f"{table.history_schema}.{table.history_table} instead of being "
                    "overwritten."
                ),
            )
        ]
    if table.is_history_table:
        return [
            Warning(
                code=WarningCode.HISTORY_TABLE,
                severity=Severity.INFO,
                title="history table",
                message=(
                    "this is the history side of a temporal pair; edits change history records."
                ),
            )
        ]
    return []


def _column_warnings(table: Table) -> list[Warning]:
    """Computed/rowversion columns (informational) and explicit collations (risky)."""
    computed = [column.name for column in table.columns if column.is_computed]
    rowversions = [column.name for column in table.columns if column.is_rowversion]
    collated = [
        f"{column.name} ({column.collation})" for column in table.columns if column.collation
    ]
    out: list[Warning] = []
    if computed:
        out.append(
            Warning(
                code=WarningCode.COMPUTED_COLUMNS,
                severity=Severity.INFO,
                title="computed columns",
                message=(
                    "computed values are calculated by the server and are never edited here (S-3)."
                ),
                notes=tuple(computed),
            )
        )
    if rowversions:
        out.append(
            Warning(
                code=WarningCode.ROWVERSION_COLUMNS,
                severity=Severity.INFO,
                title="rowversion columns",
                message=(
                    "the server bumps these on every change; they are used for conflict detection."
                ),
                notes=tuple(rowversions),
            )
        )
    if collated:
        out.append(
            Warning(
                code=WarningCode.COLLATED_COLUMN,
                severity=Severity.WARNING,
                title="explicit collations",
                message=(
                    "these columns compare and sort by their own collation, which may "
                    "differ from the database default; values can be rejected on write."
                ),
                notes=tuple(collated),
            )
        )
    return out


def _constraint_warnings(table: Table) -> list[Warning]:
    count = len(table.check_constraints)
    if not count:
        return []
    return [
        Warning(
            code=WarningCode.CHECK_CONSTRAINT,
            severity=Severity.WARNING,
            title=f"{count} CHECK constraint{'s' if count != 1 else ''}",
            message=(
                "values that violate a CHECK are rejected by the database; the app "
                "shows the expression but does not evaluate it."
            ),
            notes=tuple(f"{check.name}: {check.definition}" for check in table.check_constraints),
        )
    ]


def _incoming_fk_warnings(table: Table) -> list[Warning]:
    """Deleting rows can fail or cascade into the tables that reference this one."""
    incoming = table.incoming_foreign_keys
    if not incoming:
        return []
    tables = incoming_referencing_tables(table)
    plural = len(tables) != 1
    cascading = [fk for fk in incoming if fk.on_delete.value == "CASCADE"]
    if cascading:
        message = (
            f"deleting rows may fail or cascade to {len(tables)} "
            f"table{'s' if plural else ''}; {len(cascading)} of {len(incoming)} incoming "
            "foreign keys cascade on delete."
        )
    else:
        message = (
            f"deleting rows may fail: {len(tables)} table{'s' if plural else ''} "
            f"reference{'s' if not plural else ''} this one."
        )
    return [
        Warning(
            code=WarningCode.INCOMING_FOREIGN_KEYS,
            severity=Severity.WARNING,
            title=f"referenced by {len(tables)} table{'s' if plural else ''}",
            message=message,
            notes=tuple(_incoming_note(fk) for fk in incoming),
        )
    ]


def _incoming_note(fk: IncomingForeignKey) -> str:
    """One line per incoming FK: local columns, referenced columns and both actions."""
    local = "+".join(fk.columns)
    referenced = "+".join(fk.referenced_columns)
    return (
        f"{fk.name}: {fk.ref}.{local} → .{referenced} "
        f"(on delete {fk.on_delete.value}, on update {fk.on_update.value})"
    )


def _size_warnings(table: Table) -> list[Warning]:
    """Advisory for a table the fetch limit will certainly truncate (S-8)."""
    count = table.approximate_row_count
    if count is None or count <= LARGE_TABLE_ROWS:
        return []
    return [
        Warning(
            code=WarningCode.LARGE_TABLE,
            severity=Severity.INFO,
            title="large table",
            message=f"≈{count:,} rows — the grid loads one page; fetch more reads the next.",
        )
    ]


class InspectorService:
    """Stateful wrapper around the pure inspector functions (DESIGN §2, §9.1).

    The TUI needs two things the pure functions do not provide: *which* table is on
    screen and *which* column is focused. This object owns exactly those two pieces of
    state and re-derives everything else on demand, so there is no cache to invalidate
    when metadata is reloaded — a new :class:`~sql_table_swiss_knife.domain.catalog.Table`
    simply replaces the old one.
    """

    def __init__(self, table: Table | None = None, focused_column: str | None = None) -> None:
        self._table = table
        self._focused = focused_column

    # -- state ---------------------------------------------------------------

    @property
    def table(self) -> Table | None:
        """The table currently being inspected, or ``None`` before metadata loads."""
        return self._table

    @property
    def focused_column_name(self) -> str | None:
        """Name of the focused column, or ``None`` when nothing is focused."""
        return self._focused

    def show(self, table: Table) -> None:
        """Point the inspector at ``table``, resetting the focus to its first column."""
        self._table = table
        self._focused = table.columns[0].name if table.columns else None

    def clear(self) -> None:
        """Forget the table (leaving the screen, or after a failed metadata read)."""
        self._table = None
        self._focused = None

    def focus(self, column_name: str | None) -> None:
        """Focus ``column_name``; unknown names are ignored rather than raising.

        The grid moves the cursor as the user scrolls, so a stale column name (a refresh
        that dropped a column) must degrade to "no detail" instead of crashing the screen.
        """
        if column_name is None or self._table is None:
            self._focused = column_name
            return
        if any(column.name == column_name for column in self._table.columns):
            self._focused = column_name

    @property
    def focused_column(self) -> Column | None:
        """The focused :class:`Column`, or ``None`` when no valid column is focused."""
        if self._table is None or self._focused is None:
            return None
        try:
            return self._table.column(self._focused)
        except KeyError:
            return None

    # -- derived view -------------------------------------------------------

    def summary_rows(self) -> tuple[tuple[str, str], ...]:
        """TABLE SUMMARY rows, empty when no table is loaded."""
        return table_summary_rows(self._table) if self._table is not None else ()

    def warnings(self) -> tuple[Warning, ...]:
        """The warnings banner content, empty when no table is loaded."""
        return build_warnings(self._table) if self._table is not None else ()

    def columns(self) -> tuple[tuple[Column, tuple[ColumnBadge, ...]], ...]:
        """Every column with its badges, in ordinal order — the COLUMN LIST section."""
        if self._table is None:
            return ()
        return tuple((column, column_badges(self._table, column)) for column in self._table.columns)

    def detail(self) -> ColumnDetail | None:
        """COLUMN DETAIL for the focused column, or ``None`` when none is focused."""
        column = self.focused_column
        if column is None or self._table is None:
            return None
        return column_detail(self._table, column)

    def headers(self) -> tuple[str, ...]:
        """Column header labels for the grid, in ordinal order."""
        if self._table is None:
            return ()
        return tuple(header_label(self._table, column) for column in self._table.columns)

    def column_names(self) -> tuple[str, ...]:
        """Column names in ordinal order — the grid's value keys."""
        if self._table is None:
            return ()
        return tuple(column.name for column in self._table.columns)
