"""pyodbc-backed Microsoft SQL Server provider (M2: connection + metadata read path).

One connection per active session, always driven from a single dedicated worker thread
(DESIGN §5.1). Driver errors are translated at the boundary so nothing above this module
sees ``pyodbc.Error`` (DESIGN §5.2).
"""

import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from typing import Any

from ...domain.catalog import Database, Table, TableSummary
from ...domain.changes import ChangeKind, PendingChange, row_label
from ...domain.connection import ConnectionProfile
from ...domain.identifiers import validate_identifier
from ...domain.rows import FetchSpec, Row, RowKey, RowPage, SortKey
from ..base import (
    ActiveConnection,
    ExecuteResult,
    ProviderCapabilities,
    RowConflict,
    StatementResult,
)
from ..dialect import ApplyOptions, SqlDialect
from ..errors import MetadataError, QueryError
from ..sqlgen import (
    build_select,
    build_statements,
    is_concurrency_conflict,
    needs_identity_insert,
    sort_for_apply,
)
from . import metadata as md
from .connection import (
    SingleThreadRunner,
    build_connection_string,
    fetch_all,
    import_pyodbc,
)
from .dialect import TSqlDialect
from .errors import sanitize_driver_message

__all__ = [
    "CONFLICT_REASON",
    "MssqlConnection",
    "MssqlProvider",
]

#: Wording for "the row I fetched is no longer what the database holds" (FR-7.8).
CONFLICT_REASON = "row changed by someone else (0 rows affected)"


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _describe(change: PendingChange) -> str:
    """Which row a failure belongs to, for the error message."""
    return f"{change.kind.value} of {row_label(change.key)}"


def _message(exc: Exception) -> str:
    """User-safe message for a driver exception raised mid-Apply."""
    if isinstance(exc, (QueryError, MetadataError)):
        return str(exc)
    return sanitize_driver_message(str(exc))


def _inserted_key(table: Table, returned: dict[str, Any]) -> RowKey:
    """The identity of a row the server just inserted, from the OUTPUT clause."""
    columns = table.primary_key.columns if table.primary_key is not None else table.identity_columns
    if not columns or any(name not in returned for name in columns):
        raise QueryError(
            f"INSERT into {table.ref} did not return its key columns "
            f"({', '.join(columns) or 'none'}) — re-fetch the table to see the new rows"
        )
    return tuple((name, returned[name]) for name in columns)


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

    async def aexecute(
        self, sql: str, params: Sequence[object] | None = None
    ) -> tuple[int, list[dict[str, Any]]]:
        """Run a DML statement; returns ``(rowcount, returned rows)``.

        Separate from :meth:`afetch` because DML needs the driver's rowcount, and because
        an INSERT with an OUTPUT clause returns rows the caller must read before the
        cursor is closed. The rowcount is read on the worker thread that executed the
        statement, so it cannot be attributed to the wrong call.
        """
        if self.closed:
            raise MetadataError("connection is closed")
        result: tuple[int, list[dict[str, Any]]] = await self.runner.run(self._execute, sql, params)
        return result

    def _execute(
        self, sql: str, params: Sequence[object] | None
    ) -> tuple[int, list[dict[str, Any]]]:
        """Worker-thread body of :meth:`aexecute`."""
        cursor = self._raw.cursor()
        try:
            cursor.execute(sql, tuple(params) if params is not None else None)
            rowcount = cursor.rowcount if cursor.rowcount is not None else 0
            description = cursor.description or ()
            if not description:
                return rowcount, []
            columns = [str(item[0]) for item in description]
            return rowcount, [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        finally:
            cursor.close()

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
        """Fetch one page of rows, honouring the spec's sort, filters and keyset cursor.

        The whole statement is built by :func:`providers.sqlgen.build_select`, so the
        grid's paging can never diverge from the preview. ``limit + 1`` rows are read and
        the extra one dropped, which is how ``has_more`` is decided without a COUNT(*)
        (S-8: the full table is never loaded).

        Tables without any usable identity (no PK, no single-column UNIQUE) have no
        deterministic order — they are read-only anyway (OQ-5/S-4) — so a stable
        ``(SELECT NULL)`` order is used and ``has_more`` may over-report by one row.
        """
        handle = self._handle(conn)
        if table.identity_columns:
            # Over-fetch by one row: its existence is how ``has_more`` is decided
            # without a COUNT(*), which would scan the table on every page.
            over_fetched = replace(self._ordered(table, spec), limit=spec.limit + 1)
            select = build_select(self._dialect, table, over_fetched)
            rows = await handle.afetch(select.sql, select.params)
        else:
            qualified = self._dialect.quote_qualified(table.schema, table.name)
            column_sql = ", ".join(
                self._dialect.quote_ident(column.name) for column in table.columns
            )
            sql = (
                f"SELECT {column_sql} FROM {qualified} ORDER BY (SELECT NULL) "
                f"OFFSET {spec.offset} ROWS FETCH NEXT {spec.limit + 1} ROWS ONLY"
            )
            rows = await handle.afetch(sql)
        page = rows[: spec.limit]
        return RowPage(
            rows=tuple(Row(dict(row)) for row in page),
            offset=spec.offset,
            limit=spec.limit,
            has_more=len(rows) > spec.limit,
        )

    @staticmethod
    def _ordered(table: Table, spec: FetchSpec) -> FetchSpec:
        """Guarantee a deterministic order even when the caller sends none.

        ``OFFSET/FETCH`` without ``ORDER BY`` has no defined row order, so paging would
        skip and repeat rows. The identity columns are the natural default (and the same
        order the data service asks for); a caller-supplied sort always wins.
        """
        if spec.sort or not table.identity_columns:
            return spec
        return replace(spec, sort=tuple(SortKey(column=name) for name in table.identity_columns))

    async def execute_changes(
        self,
        conn: ActiveConnection,
        table: Table,
        changes: Sequence[PendingChange],
        options: ApplyOptions | None = None,
    ) -> ExecuteResult:
        """Apply staged changes inside one transaction, rolling back on any failure.

        The pipeline (DESIGN §7.2):

        1. order the changes DELETE → UPDATE → INSERT and build the statements once;
        2. ``BEGIN TRANSACTION``;
        3. run each statement with its parameters, collecting per-statement results;
        4. an UPDATE/DELETE affecting 0 rows is a **concurrency conflict**, not a
           statement error: it is recorded and the transaction is rolled back, so no
           partial work survives (FR-7.8);
        5. any driver error → ``ROLLBACK`` and an :class:`ExecuteResult` pointing at the
           failing statement with a sanitized message (FR-7.6);
        6. otherwise ``COMMIT`` and report the generated identity keys.

        ``IDENTITY_INSERT`` is toggled around the statements when the caller explicitly
        opted in, and always turned back off — leaving it on would block every other
        session's inserts on the table.
        """
        settings = options if options is not None else ApplyOptions()
        handle = self._handle(conn)
        if not table.updatable:
            raise QueryError(f"{table.ref} has no usable row identity — its rows are read-only")
        started = time.perf_counter()
        ordered = sort_for_apply(changes)
        statements = build_statements(self._dialect, table, ordered, options=settings)
        results: list[StatementResult] = []
        conflicts: list[RowConflict] = []
        inserted_keys: list[RowKey] = []
        identity_on = needs_identity_insert(
            table, statements, identity_insert=settings.identity_insert
        )
        await handle.afetch(self._dialect.begin_transaction())
        try:
            if identity_on:
                await handle.afetch(
                    self._dialect.identity_insert_sql(table.schema, table.name, enabled=True)
                )
            for index, (change, statement) in enumerate(zip(ordered, statements, strict=True)):
                rowcount, returned = await handle.aexecute(
                    statement.sql_parametrized, statement.param_values
                )
                if is_concurrency_conflict(change, rowcount):
                    conflicts.append(RowConflict(change, statement, CONFLICT_REASON))
                    results.append(StatementResult(change, statement, 0, False, CONFLICT_REASON))
                    return await self._rollback(
                        handle,
                        results,
                        conflicts,
                        index,
                        started,
                        f"{CONFLICT_REASON} ({_describe(change)}) — refresh the row and retry",
                    )
                if returned and statement.kind is ChangeKind.INSERT:
                    inserted_keys.append(_inserted_key(table, returned[0]))
                results.append(StatementResult(change, statement, rowcount, True, None))
            if identity_on:
                await handle.afetch(
                    self._dialect.identity_insert_sql(table.schema, table.name, enabled=False)
                )
        except Exception as exc:
            if identity_on:
                # Best effort: leaving IDENTITY_INSERT on would be worse than the error.
                with suppress(Exception):
                    await handle.afetch(
                        self._dialect.identity_insert_sql(table.schema, table.name, enabled=False)
                    )
            return await self._rollback(
                handle, results, conflicts, len(results), started, _message(exc)
            )
        await handle.afetch(self._dialect.commit_transaction())
        return ExecuteResult(
            committed=True,
            results=tuple(results),
            duration_ms=_elapsed_ms(started),
            inserted_keys=tuple(inserted_keys),
        )

    async def _rollback(
        self,
        handle: MssqlConnection,
        results: list[StatementResult],
        conflicts: list[RowConflict],
        failed_index: int,
        started: float,
        error: str,
    ) -> ExecuteResult:
        """Roll the transaction back and build the failure result (FR-7.6)."""
        with suppress(Exception):
            await handle.afetch(self._dialect.rollback_transaction())
        return ExecuteResult(
            committed=False,
            results=tuple(results),
            duration_ms=_elapsed_ms(started),
            failed_index=failed_index,
            error=error,
            conflicts=tuple(conflicts),
        )
