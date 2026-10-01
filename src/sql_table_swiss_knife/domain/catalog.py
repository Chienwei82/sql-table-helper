"""Catalog domain models: databases, tables, columns, keys, constraints, triggers."""

from dataclasses import dataclass
from enum import Enum

from .identifiers import TableRef, validate_identifier

__all__ = [
    "CheckConstraint",
    "Column",
    "Database",
    "ForeignKey",
    "IncomingForeignKey",
    "PrimaryKey",
    "ReferentialAction",
    "Table",
    "TableKind",
    "TableMetadata",
    "TableSummary",
    "Trigger",
    "UniqueConstraint",
]


class TableKind(Enum):
    """Kind of relation exposed by the catalog."""

    BASE_TABLE = "table"
    VIEW = "view"


class ReferentialAction(Enum):
    """Foreign key ON DELETE / ON UPDATE actions."""

    NO_ACTION = "NO ACTION"
    CASCADE = "CASCADE"
    SET_NULL = "SET NULL"
    SET_DEFAULT = "SET DEFAULT"


@dataclass(frozen=True, slots=True)
class Database:
    """A database on the server."""

    name: str
    is_current: bool = False

    def __post_init__(self) -> None:
        validate_identifier(self.name, kind="database name")


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a table/view, with full type and server-managed metadata."""

    name: str
    ordinal: int
    data_type: str
    max_length: int | None
    precision: int | None
    scale: int | None
    nullable: bool
    default_definition: str | None
    is_identity: bool
    identity_seed: int | None = None
    identity_increment: int | None = None
    is_computed: bool = False
    computed_definition: str | None = None
    is_rowversion: bool = False
    is_primary_key: bool = False
    collation: str | None = None
    is_foreign_key: bool = False
    #: For computed columns: whether the value is physically stored (``PERSISTED``).
    computed_persisted: bool | None = None
    #: ``sys.columns.generated_always_type``: 0 = user-maintained, 1 = period start of a
    #: temporal table, 2 = period end. These columns are maintained by the engine and the
    #: server rejects any attempt to write them.
    generated_always_type: int = 0

    def __post_init__(self) -> None:
        validate_identifier(self.name, kind="column name")
        if not self.data_type:
            raise ValueError("column data_type must not be empty")
        if self.ordinal < 0:
            raise ValueError("column ordinal must be >= 0")
        if self.max_length is not None and self.max_length < 0:
            raise ValueError("column max_length must be >= 0 when set")
        if self.precision is not None and self.precision < 0:
            raise ValueError("column precision must be >= 0 when set")
        if self.scale is not None and self.scale < 0:
            raise ValueError("column scale must be >= 0 when set")

    @property
    def is_server_managed(self) -> bool:
        """True when the value is maintained by the server (never user-editable)."""
        return (
            self.is_identity
            or self.is_computed
            or self.is_rowversion
            # Temporal period columns: GENERATED ALWAYS, so the server owns them.
            or self.generated_always_type != 0
        )


@dataclass(frozen=True, slots=True)
class PrimaryKey:
    """Primary key constraint (ordered columns)."""

    name: str | None
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.columns:
            raise ValueError("primary key must have at least one column")
        for column in self.columns:
            validate_identifier(column, kind="primary key column")


@dataclass(frozen=True, slots=True)
class ForeignKey:
    """Foreign key constraint with referenced table/columns and actions."""

    name: str
    columns: tuple[str, ...]
    referenced_schema: str
    referenced_table: str
    referenced_columns: tuple[str, ...]
    on_delete: ReferentialAction
    on_update: ReferentialAction

    def __post_init__(self) -> None:
        validate_identifier(self.name, kind="foreign key name")
        if not self.columns:
            raise ValueError("foreign key must have at least one column")
        if len(self.columns) != len(self.referenced_columns):
            raise ValueError(
                "foreign key local and referenced column counts must match: "
                f"{len(self.columns)} != {len(self.referenced_columns)}"
            )
        for column in (*self.columns, *self.referenced_columns):
            validate_identifier(column, kind="foreign key column")
        validate_identifier(self.referenced_schema, kind="referenced schema")
        validate_identifier(self.referenced_table, kind="referenced table")


@dataclass(frozen=True, slots=True)
class IncomingForeignKey:
    """A foreign key on ANOTHER table that references this table (incoming edge).

    Symmetric to :class:`ForeignKey`: ``columns`` are the FK columns on the *referencing*
    table, ``referenced_columns`` are the matching columns on **this** table. The actions
    describe what happens to this table's rows when the referencing rows change — and,
    more importantly for safety, whether deleting this table's rows cascades/blocks.
    """

    name: str
    schema: str  # schema of the table holding the FK
    table: str  # table holding the FK (the referencing side)
    columns: tuple[str, ...]  # FK columns on the referencing table
    referenced_columns: tuple[str, ...]  # matching columns on this table
    on_delete: ReferentialAction
    on_update: ReferentialAction

    def __post_init__(self) -> None:
        validate_identifier(self.name, kind="foreign key name")
        validate_identifier(self.schema, kind="referencing schema")
        validate_identifier(self.table, kind="referencing table")
        if not self.columns:
            raise ValueError("foreign key must have at least one column")
        if len(self.columns) != len(self.referenced_columns):
            raise ValueError(
                "foreign key local and referenced column counts must match: "
                f"{len(self.columns)} != {len(self.referenced_columns)}"
            )
        for column in (*self.columns, *self.referenced_columns):
            validate_identifier(column, kind="foreign key column")

    @property
    def ref(self) -> str:
        """Schema-qualified name of the referencing table, e.g. ``dbo.Region``."""
        return f"{self.schema}.{self.table}"


@dataclass(frozen=True, slots=True)
class UniqueConstraint:
    """UNIQUE constraint (ordered columns)."""

    name: str
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_identifier(self.name, kind="unique constraint name")
        if not self.columns:
            raise ValueError("unique constraint must have at least one column")
        for column in self.columns:
            validate_identifier(column, kind="unique constraint column")


@dataclass(frozen=True, slots=True)
class CheckConstraint:
    """CHECK constraint — definition is display-only and never executed by the app."""

    name: str
    definition: str

    def __post_init__(self) -> None:
        validate_identifier(self.name, kind="check constraint name")
        if not self.definition:
            raise ValueError("check constraint definition must not be empty")


@dataclass(frozen=True, slots=True)
class Trigger:
    """DML trigger: events (INSERT/UPDATE/DELETE), firing mode, enabled state."""

    name: str
    events: tuple[str, ...]
    firing: str
    enabled: bool = True

    def __post_init__(self) -> None:
        validate_identifier(self.name, kind="trigger name")
        allowed_events = {"INSERT", "UPDATE", "DELETE"}
        normalized = tuple(event.strip().upper() for event in self.events)
        if not normalized:
            raise ValueError("trigger must have at least one event")
        for event in normalized:
            if event not in allowed_events:
                raise ValueError(
                    f"trigger event must be one of {sorted(allowed_events)}, got {event!r}"
                )
        firing = self.firing.strip().upper()
        if firing not in {"AFTER", "FOR", "INSTEAD OF"}:
            raise ValueError(
                f"trigger firing must be AFTER, FOR or INSTEAD OF, got {self.firing!r}"
            )
        object.__setattr__(self, "events", normalized)
        object.__setattr__(self, "firing", firing)


@dataclass(frozen=True, slots=True)
class TableSummary:
    """Lightweight table listing entry (table picker)."""

    schema: str
    name: str
    kind: TableKind
    has_primary_key: bool = False
    approximate_row_count: int | None = None  # cheap estimate; None if unknown/n/a
    has_triggers: bool = False
    #: Outgoing foreign keys — drives the 🔗 badge in the table picker.
    has_foreign_keys: bool = False

    def __post_init__(self) -> None:
        validate_identifier(self.schema, kind="schema name")
        validate_identifier(self.name, kind="table name")
        if self.approximate_row_count is not None and self.approximate_row_count < 0:
            raise ValueError("approximate_row_count must be >= 0 when set")

    @property
    def ref(self) -> TableRef:
        return TableRef(schema=self.schema, name=self.name)


@dataclass(frozen=True, slots=True)
class Table:
    """Full metadata for a table or view."""

    schema: str
    name: str
    kind: TableKind
    columns: tuple[Column, ...]
    primary_key: PrimaryKey | None = None
    foreign_keys: tuple[ForeignKey, ...] = ()
    unique_constraints: tuple[UniqueConstraint, ...] = ()
    check_constraints: tuple[CheckConstraint, ...] = ()
    triggers: tuple[Trigger, ...] = ()
    approximate_row_count: int | None = None
    #: FKs on OTHER tables that reference this table (incoming edges).
    incoming_foreign_keys: tuple[IncomingForeignKey, ...] = ()
    #: System-versioned (temporal) table: ``True`` when history tracking is enabled.
    is_system_versioned: bool = False
    #: ``True`` when this table is itself the history side of a temporal pair.
    is_history_table: bool = False
    history_schema: str | None = None
    history_table: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.schema, kind="schema name")
        validate_identifier(self.name, kind="table name")
        if not self.columns:
            raise ValueError("table must have at least one column")
        names = [column.name for column in self.columns]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate column names: {sorted(duplicates)}")
        if self.approximate_row_count is not None and self.approximate_row_count < 0:
            raise ValueError("approximate_row_count must be >= 0 when set")
        if self.is_system_versioned:
            if not self.history_table or not self.history_schema:
                raise ValueError("system-versioned table requires history_schema and history_table")
            validate_identifier(self.history_schema, kind="history schema")
            validate_identifier(self.history_table, kind="history table")
        elif self.history_table is not None or self.history_schema is not None:
            raise ValueError("history_schema/history_table require is_system_versioned=True")

    @property
    def ref(self) -> TableRef:
        return TableRef(schema=self.schema, name=self.name)

    @property
    def is_view(self) -> bool:
        return self.kind is TableKind.VIEW

    @property
    def rowversion_column(self) -> Column | None:
        for column in self.columns:
            if column.is_rowversion:
                return column
        return None

    @property
    def identity_columns(self) -> tuple[str, ...]:
        """Columns usable as stable row identity (OQ-5): PK, else a single-column
        non-nullable UNIQUE constraint. Empty tuple means rows are read-only (S-4)."""
        if self.primary_key is not None:
            return self.primary_key.columns
        for constraint in self.unique_constraints:
            if len(constraint.columns) != 1:
                continue
            column = self.column(constraint.columns[0])
            if not column.nullable and not column.is_computed:
                return constraint.columns
        return ()

    @property
    def updatable(self) -> bool:
        """True when rows can be safely UPDATEd/DELETEd (base table with a key)."""
        return self.kind is TableKind.BASE_TABLE and bool(self.identity_columns)

    def column(self, name: str) -> Column:
        """Return the column with the exact given name.

        Raises:
            KeyError: if no such column exists.
        """
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(f"{self.ref} has no column {name!r}")

    def column_or_none(self, name: str) -> Column | None:
        """The column with this name, or ``None``.

        Used where a name may legitimately be stale (a refresh dropped the column, a
        saved grid layout refers to a column that no longer exists) and degrading to "no
        column" beats crashing the screen.
        """
        for column in self.columns:
            if column.name == name:
                return column
        return None

    @property
    def summary(self) -> TableSummary:
        return TableSummary(
            schema=self.schema,
            name=self.name,
            kind=self.kind,
            has_primary_key=self.primary_key is not None,
            approximate_row_count=self.approximate_row_count,
            has_triggers=bool(self.triggers),
            has_foreign_keys=bool(self.foreign_keys),
        )


#: Alias kept for compatibility with DESIGN.md terminology.
TableMetadata = Table
