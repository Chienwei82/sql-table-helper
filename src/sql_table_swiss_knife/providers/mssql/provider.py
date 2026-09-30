"""pyodbc-backed Microsoft SQL Server provider (M2: connection + metadata read path).

One connection per active session, always driven from a single dedicated worker thread
(DESIGN §5.1). Driver errors are translated at the boundary so nothing above this module
sees ``pyodbc.Error`` (DESIGN §5.2).
"""

from collections.abc import Sequence
from typing import Any

from ...domain.catalog import Database, Table, TableSummary
from ...domain.changes import PendingChange
from ...domain.connection import ConnectionProfile
from ...domain.identifiers import validate_identifier
from ...domain.rows import FetchSpec, Row, RowPage
from ..base import ActiveConnection, ExecuteResult, ProviderCapabilities
from ..dialect import SqlDialect
from ..errors import MetadataError
from . import metadata as md
from .connection import (
    SingleThreadRunner,
    build_connection_string,
    fetch_all,
    import_pyodbc,
)
from .dialect import TSqlDialect

__all__ = ["MssqlConnection", "MssqlProvider"]


class MssqlConnection:
    """Active connection handle: the driver connection plus its worker thread.

    Deliberately opaque to callers (DESIGN §5) — it satisfies the ``ActiveConnection``
    protocol via ``provider_name`` / ``server`` / ``database``.
    """

    def __init__(self, raw: Any, runner: SingleThreadRunner, profile: ConnectionProfile) -> None:
        self._raw = raw
        self.runner = runner
        self._profile = profile
        self.provider_name = "mssql"
        self.server = f"{profile.host},{profile.port}"
        self.database = profile.database or "master"
        self.closed = False

    @property
    def raw(self) -> Any:
        """The underlying driver connection (provider-internal use only)."""
        return self._raw

    @property
    def profile(self) -> ConnectionProfile:
        return self._profile

    def fetch(self, sql: str, params: Sequence[object] | None = None) -> list[dict[str, Any]]:
        """Run a query on the connection's worker thread and return plain dicts."""
        if self.closed:
            raise MetadataError("connection is closed")
        cursor = self._raw.cursor()
        try:
            return fetch_all(cursor, sql, params)
        finally:
            cursor.close()

    async def afetch(
        self, sql: str, params: Sequence[object] | None = None
    ) -> list[dict[str, Any]]:
        """Async wrapper around :meth:`fetch` running on the dedicated worker thread."""
        rows: list[dict[str, Any]] = await self.runner.run(self.fetch, sql, params)
        return rows

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._raw.close()
        finally:
            self.closed = True


OBJECT_LOOKUP_SQL = """
SELECT o.type AS object_type,
       ISNULL(rp.row_count, 0) AS approximate_rows
  FROM sys.objects AS o
  JOIN sys.schemas AS s ON s.schema_id = o.schema_id
  LEFT JOIN (SELECT object_id, SUM(row_count) AS row_count
               FROM sys.dm_db_partition_stats
              WHERE index_id IN (0, 1)
              GROUP BY object_id) AS rp ON rp.object_id = o.object_id
 WHERE o.object_id = OBJECT_ID(?)
   AND o.is_ms_shipped = 0
"""

SERVER_INFO_SQL = """
SELECT CONVERT(nvarchar(128), SERVERPROPERTY('ServerName')) AS server_name,
       CONVERT(nvarchar(128), SERVERPROPERTY('ProductVersion')) AS server_version,
       CONVERT(nvarchar(128), SERVERPROPERTY('Edition')) AS edition,
       CONVERT(nvarchar(128), DB_NAME()) AS database_name
"""


class MssqlProvider:
    """``DatabaseProvider`` implementation for Microsoft SQL Server."""

    name = "mssql"

    def __init__(self) -> None:
        self._dialect = TSqlDialect()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_schemas=True,
            supports_integrated_auth=True,
            supports_rowversion=True,
            supports_output_clause=True,
            read_only_views=True,
        )

    @property
    def dialect(self) -> SqlDialect:
        return self._dialect

    def quote_identifier(self, name: str) -> str:
        return self._dialect.quote_ident(name)

    # -- connection lifecycle ---------------------------------------------

    async def connect(
        self, profile: ConnectionProfile, password: str | None = None
    ) -> MssqlConnection:
        """Open a connection.

        SQL authentication needs a password; integrated authentication must not have one
        (the connection-string builder enforces both directions).
        """
        pyodbc = import_pyodbc()
        connection_string = build_connection_string(profile, password)
        runner = SingleThreadRunner()
        try:
            raw = await runner.run(
                pyodbc.connect,
                connection_string,
                timeout=profile.options.connect_timeout_s,
            )
        except BaseException:
            await runner.close()
            raise
        return MssqlConnection(raw, runner, profile)

    async def test_connection(
        self, profile: ConnectionProfile, password: str | None = None
    ) -> dict[str, Any]:
        """Connect, read the server identification, disconnect (Home screen "Test")."""
        conn = await self.connect(profile, password)
        try:
            rows = await conn.afetch(SERVER_INFO_SQL)
            return dict(rows[0]) if rows else {}
        finally:
            await self.disconnect(conn)

    @staticmethod
    def _handle(conn: ActiveConnection) -> MssqlConnection:
        """Narrow an ``ActiveConnection`` to this provider's own handle.

        The protocol deliberately hides provider internals; the cast is the single
        place where the concrete type is recovered, and it fails loudly for a
        connection belonging to another provider.
        """
        if not isinstance(conn, MssqlConnection):
            raise MetadataError(
                f"connection belongs to {conn.provider_name!r}, not to the mssql provider"
            )
        return conn

    async def disconnect(self, conn: ActiveConnection) -> None:
        """Close the driver connection and shut down its worker thread."""
        handle = self._handle(conn)
        try:
            handle.close()
        finally:
            await handle.runner.close()

    # -- catalog -----------------------------------------------------------

    async def list_databases(self, conn: ActiveConnection) -> list[Database]:
        """List online, writable user databases; the connected one is flagged."""
        rows = await self._handle(conn).afetch(md.database_rows_sql())
        return md.databases_from_rows(rows)

    async def list_tables(
        self, conn: ActiveConnection, schema: str | None = None
    ) -> list[TableSummary]:
        """List tables and views (optionally in one schema) with counts and flags."""
        if schema is not None:
            validate_identifier(schema, kind="schema name")
        params = (schema,) if schema is not None else None
        rows = await self._handle(conn).afetch(md.table_rows_sql(schema), params)
        return md.table_summaries(rows)

    async def get_table_metadata(self, conn: ActiveConnection, schema: str, name: str) -> Table:
        """Full metadata for one table or view, read from the ``sys.*`` catalog views.

        Captures exact types (length/precision/scale), nullability, identity seed and
        increment, computed definitions (persisted or not), rowversion columns, defaults,
        PK and UNIQUE columns, outgoing FKs with referential actions, incoming FKs, CHECK
        definitions, triggers (firing mode, events, enabled state) and temporal pairing.
        """
        validate_identifier(schema, kind="schema name")
        validate_identifier(name, kind="table name")
        target = f"[{schema}].[{name}]"

        handle = self._handle(conn)
        object_rows = await handle.afetch(OBJECT_LOOKUP_SQL, (target,))
        if not object_rows:
            raise MetadataError(f"{schema}.{name} does not exist or is not visible to this login")
        object_type = md.object_kind(object_rows[0].get("object_type"))
        if object_type is None:
            raise MetadataError(
                f"{schema}.{name} is not a table or view "
                f"(sys.objects.type={object_rows[0].get('object_type')!r})"
            )
        approximate_row_count = md.row_count(object_rows[0].get("approximate_rows"))

        columns = await handle.afetch(md.COLUMNS_SQL, (target,))
        keys = await handle.afetch(md.KEYS_SQL, (target,))
        foreign = await handle.afetch(md.FOREIGN_KEYS_SQL, (target,))
        incoming = await handle.afetch(md.INCOMING_FOREIGN_KEYS_SQL, (target,))
        checks = await handle.afetch(md.CHECK_CONSTRAINTS_SQL, (target,))
        triggers = await handle.afetch(md.TRIGGERS_SQL, (target,))
        temporal = await handle.afetch(md.TEMPORAL_SQL, (target,))

        return md.build_table(
            schema=schema,
            name=name,
            kind=object_type,
            column_rows=columns,
            key_rows=keys,
            fk_rows=foreign,
            incoming_fk_rows=incoming,
            check_rows=checks,
            trigger_rows=triggers,
            temporal_rows=temporal,
            approximate_row_count=approximate_row_count,
        )

    # -- rows --------------------------------------------------------------

    async def fetch_rows(self, conn: ActiveConnection, table: Table, spec: FetchSpec) -> RowPage:
        """Fetch one page of rows ordered by the table's identity columns.

        Tables without any usable identity (no PK, no single-column UNIQUE) have no
        deterministic order — they are read-only anyway (OQ-5/S-4), so a stable
        ``(SELECT NULL)`` order is used and ``has_more`` may over-report by one row.
        """
        identity = table.identity_columns
        orders = [(column, False) for column in identity]
        columns = [column.name for column in table.columns]
        if orders:
            sql = self._dialect.select_rows_sql(
                table.schema, table.name, columns, [], orders, spec.limit + 1, spec.offset
            )
        else:
            qualified = self._dialect.quote_qualified(table.schema, table.name)
            column_sql = ", ".join(self._dialect.quote_ident(name) for name in columns)
            sql = (
                f"SELECT {column_sql} FROM {qualified} ORDER BY (SELECT NULL) "
                f"OFFSET {spec.offset} ROWS FETCH NEXT {spec.limit + 1} ROWS ONLY"
            )
        rows = await self._handle(conn).afetch(sql)
        page = rows[: spec.limit]
        return RowPage(
            rows=tuple(Row(dict(row)) for row in page),
            offset=spec.offset,
            limit=spec.limit,
            has_more=len(rows) > spec.limit,
        )

    async def execute_changes(
        self, conn: ActiveConnection, table: Table, changes: Sequence[PendingChange]
    ) -> ExecuteResult:
        """Not in M2: the transactional Apply pipeline lands in M6."""
        del conn, table, changes
        raise NotImplementedError("the apply pipeline lands in Milestone 6")
