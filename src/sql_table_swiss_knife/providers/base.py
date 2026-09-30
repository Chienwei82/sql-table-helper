"""``DatabaseProvider`` protocol: the single seam every DBMS plugin implements (DESIGN §5)."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.catalog import Database, Table, TableSummary
from ..domain.changes import ChangeKind, PendingChange
from ..domain.connection import ConnectionProfile
from ..domain.rows import FetchSpec, RowKey, RowPage
from .dialect import ApplyOptions, SqlDialect, SqlStatement

__all__ = [
    "ActiveConnection",
    "DatabaseProvider",
    "ExecuteResult",
    "ProviderCapabilities",
    "RowConflict",
    "StatementResult",
]


@runtime_checkable
class ActiveConnection(Protocol):
    """Opaque connection handle owned by a provider (services never inspect internals)."""

    provider_name: str
    server: str
    database: str


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a provider/DBMS supports (drives UI affordances)."""

    supports_schemas: bool = True
    supports_integrated_auth: bool = False
    supports_rowversion: bool = False
    supports_output_clause: bool = False
    read_only_views: bool = True


@dataclass(frozen=True, slots=True)
class StatementResult:
    """Per-statement outcome of an ``execute_changes`` call."""

    change: PendingChange
    statement: SqlStatement
    rowcount: int
    success: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RowConflict:
    """A per-row concurrency conflict (FR-7.8).

    Raised when an UPDATE or DELETE matched 0 rows: the row was deleted, or somebody
    else changed it after the grid fetched it. Reported per row so the UI can flag the
    offending row and keep working instead of aborting the whole session.
    """

    change: PendingChange
    statement: SqlStatement
    reason: str

    @property
    def row_key(self) -> RowKey | None:
        return self.change.key

    @property
    def is_update(self) -> bool:
        return self.change.kind is ChangeKind.UPDATE


@dataclass(frozen=True, slots=True)
class ExecuteResult:
    """Result of executing staged changes in one transaction (FR-7.6).

    On failure nothing was applied: ``committed`` is False, ``failed_index`` points at
    the failing statement and ``error`` carries the sanitized message. ``conflicts``
    collects the per-row concurrency conflicts detected (0 rows affected).
    """

    committed: bool
    results: tuple[StatementResult, ...]
    duration_ms: int
    failed_index: int | None = None
    error: str | None = None
    conflicts: tuple[RowConflict, ...] = ()
    #: Identity values the server generated for the INSERTs, in statement order.
    inserted_keys: tuple[RowKey, ...] = ()

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be >= 0")
        if self.committed and self.failed_index is not None:
            raise ValueError("committed result cannot carry a failed_index")
        if not self.committed and self.failed_index is None:
            raise ValueError("failed (rolled back) result requires a failed_index")

    @property
    def statement_count(self) -> int:
        return len(self.results)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


@runtime_checkable
class DatabaseProvider(Protocol):
    """Async DBMS plugin interface (M1 contract — implemented by mssql in M2)."""

    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    @property
    def dialect(self) -> SqlDialect: ...

    async def connect(
        self, profile: ConnectionProfile, password: str | None = None
    ) -> ActiveConnection: ...

    async def disconnect(self, conn: ActiveConnection) -> None: ...

    async def list_databases(self, conn: ActiveConnection) -> list[Database]: ...

    async def list_tables(
        self, conn: ActiveConnection, schema: str | None = None
    ) -> list[TableSummary]: ...

    async def get_table_metadata(self, conn: ActiveConnection, schema: str, name: str) -> Table: ...

    async def fetch_rows(
        self, conn: ActiveConnection, table: Table, spec: FetchSpec
    ) -> RowPage: ...

    async def execute_changes(
        self,
        conn: ActiveConnection,
        table: Table,
        changes: Sequence[PendingChange],
        options: ApplyOptions | None = None,
    ) -> ExecuteResult: ...

    def quote_identifier(self, name: str) -> str: ...
