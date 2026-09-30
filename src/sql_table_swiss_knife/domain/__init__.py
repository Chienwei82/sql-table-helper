"""Pure domain models — no I/O, no driver types (DESIGN §4)."""

from .catalog import (
    CheckConstraint,
    Column,
    Database,
    ForeignKey,
    IncomingForeignKey,
    PrimaryKey,
    ReferentialAction,
    Table,
    TableKind,
    TableMetadata,
    TableSummary,
    Trigger,
    UniqueConstraint,
)
from .changes import ChangeKind, PendingChange
from .connection import AuthMode, ConnectionOptions, ConnectionProfile, ConnectionResult
from .identifiers import TableRef, validate_identifier
from .rows import (
    FetchSpec,
    FilterOp,
    Row,
    RowFilter,
    RowKey,
    RowPage,
    SortKey,
    make_row_key,
)

__all__ = [
    "AuthMode",
    "ChangeKind",
    "CheckConstraint",
    "Column",
    "ConnectionOptions",
    "ConnectionProfile",
    "ConnectionResult",
    "Database",
    "FetchSpec",
    "FilterOp",
    "ForeignKey",
    "IncomingForeignKey",
    "PendingChange",
    "PrimaryKey",
    "ReferentialAction",
    "Row",
    "RowFilter",
    "RowKey",
    "RowPage",
    "SortKey",
    "Table",
    "TableKind",
    "TableMetadata",
    "TableRef",
    "TableSummary",
    "Trigger",
    "UniqueConstraint",
    "make_row_key",
    "validate_identifier",
]
