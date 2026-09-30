"""In-memory ``DatabaseProvider`` fake used for contract and service tests."""

import re
import time
from collections.abc import Sequence
from typing import Any

from sql_table_swiss_knife.domain import (
    ChangeKind,
    ConnectionProfile,
    Database,
    FetchSpec,
    FilterOp,
    PendingChange,
    Row,
    RowFilter,
    RowKey,
    RowPage,
    Table,
    TableSummary,
)
from sql_table_swiss_knife.providers import (
    ActiveConnection,
    ExecuteResult,
    MetadataError,
    ProviderCapabilities,
    SqlDialect,
    SqlStatement,
    StatementResult,
    build_statements,
    sort_for_apply,
)
from sql_table_swiss_knife.providers.mssql import TSqlDialect


class FakeConnection:
    """Connection handle satisfying the ActiveConnection protocol."""

    def __init__(self, *, provider_name: str, server: str, database: str) -> None:
        self.provider_name = provider_name
        self.server = server
        self.database = database
        self.closed = False


class FakeProvider:
    """DBMS-free provider over in-memory rows.

    Simulates transactional semantics: ``execute_changes`` applies all changes to a
    snapshot and either commits (all rows visible) or rolls back (snapshot restored).
    ``fail_on`` scripts a failure at a statement index for rollback tests.
    """

    name = "fake"

    def __init__(
        self,
        tables: Sequence[Table] = (),
        rows: dict[str, list[dict[str, object]]] | None = None,
        *,
        fail_on: int | None = None,
    ) -> None:
        self._dialect = TSqlDialect()
        self._tables: dict[str, Table] = {f"{table.schema}.{table.name}": table for table in tables}
        self._rows: dict[str, list[dict[str, object]]] = {
            key: [dict(row) for row in (rows or {}).get(key.split(".")[-1], [])]
            for key in self._tables
        }
        self._connected = False
        self._fail_on = fail_on
        self.transaction_log: list[str] = []
        self.executed: list[str] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_integrated_auth=False,
            supports_rowversion=True,
            supports_output_clause=True,
        )

    @property
    def dialect(self) -> SqlDialect:
        return self._dialect

    async def connect(
        self, profile: ConnectionProfile, password: str | None = None
    ) -> FakeConnection:
        del password
        self._connected = True
        return FakeConnection(
            provider_name=self.name,
            server=f"{profile.host}:{profile.port}",
            database=profile.database or "test",
        )

    async def disconnect(self, conn: ActiveConnection) -> None:
        self._require(conn)
        self._connected = False

    async def list_databases(self, conn: ActiveConnection) -> list[Database]:
        self._require(conn)
        return [Database("test", is_current=True)]

    async def list_tables(
        self, conn: ActiveConnection, schema: str | None = None
    ) -> list[TableSummary]:
        self._require(conn)
        return [
            table.summary
            for table in self._tables.values()
            if schema is None or table.schema == schema
        ]

    async def get_table_metadata(self, conn: ActiveConnection, schema: str, name: str) -> Table:
        self._require(conn)
        try:
            return self._state(schema, name)
        except KeyError as exc:
            raise MetadataError(str(exc.args[0])) from exc

    async def fetch_rows(self, conn: ActiveConnection, table: Table, spec: FetchSpec) -> RowPage:
        self._require(conn)
        rows = self._table_rows(table)
        for row_filter in spec.filters:
            rows = [row for row in rows if _matches(row.get(row_filter.column), row_filter)]
        # Stable multi-pass sort: last sort key first (like ORDER BY semantics).
        for sort_key in reversed(spec.sort):
            rows = sorted(
                rows,
                key=lambda row: _sort_value(row, sort_key.column),
                reverse=sort_key.descending,
            )
        total = len(rows)
        page = rows[spec.offset : spec.offset + spec.limit]
        return RowPage(
            rows=tuple(Row(dict(row)) for row in page),
            offset=spec.offset,
            limit=spec.limit,
            has_more=spec.offset + len(page) < total,
            total_row_count=total,
        )

    async def execute_changes(
        self, conn: ActiveConnection, table: Table, changes: Sequence[PendingChange]
    ) -> ExecuteResult:
        self._require(conn)
        started = time.perf_counter()
        state_rows = self._table_rows(table)
        snapshot = [dict(row) for row in state_rows]
        self.transaction_log.append("BEGIN")
        ordered = sort_for_apply(changes)
        statements = build_statements(self._dialect, table, ordered)
        results: list[StatementResult] = []
        for index, (change, statement) in enumerate(zip(ordered, statements, strict=True)):
            self.executed.append(statement.sql_parametrized)
            if self._fail_on is not None and index == self._fail_on:
                return self._rollback(
                    state_rows,
                    snapshot,
                    results,
                    change,
                    statement,
                    index,
                    started,
                    "simulated failure",
                )
            rowcount, error = self._apply_change(change, state_rows)
            if error is not None:
                return self._rollback(
                    state_rows, snapshot, results, change, statement, index, started, error
                )
            results.append(StatementResult(change, statement, rowcount, True, None))
        self.transaction_log.append("COMMIT")
        return ExecuteResult(
            committed=True,
            results=tuple(results),
            duration_ms=_elapsed_ms(started),
        )

    def quote_identifier(self, name: str) -> str:
        return self._dialect.quote_ident(name)

    # -- internals ---------------------------------------------------------

    def _require(self, conn: ActiveConnection) -> None:
        if conn.provider_name != self.name:
            raise MetadataError(f"connection belongs to {conn.provider_name!r}")

    def _state(self, schema: str, name: str) -> Table:
        try:
            return self._tables[f"{schema}.{name}"]
        except KeyError as exc:
            raise MetadataError(f"unknown table {schema}.{name}") from exc

    def _table_rows(self, table: Table) -> list[dict[str, object]]:
        try:
            return self._rows[f"{table.schema}.{table.name}"]
        except KeyError as exc:
            raise MetadataError(f"unknown table {table.ref}") from exc

    def _apply_change(
        self, change: PendingChange, rows: list[dict[str, object]]
    ) -> tuple[int, str | None]:
        match change.kind:
            case ChangeKind.INSERT:
                if change.after is None:
                    raise ValueError("INSERT change is incomplete")
                rows.append(dict(change.after))
                return 1, None
            case ChangeKind.UPDATE | ChangeKind.DELETE:
                if change.key is None:
                    raise ValueError("change is missing a row key")
                index = _find_row(rows, change.key)
                if index is None:
                    return 0, "optimistic concurrency: 0 rows affected"
                if change.kind is ChangeKind.UPDATE:
                    if change.after is None:
                        raise ValueError("UPDATE change is incomplete")
                    rows[index].update(change.after)
                else:
                    rows.pop(index)
                return 1, None

    def _rollback(
        self,
        state_rows: list[dict[str, object]],
        snapshot: list[dict[str, object]],
        results: list[StatementResult],
        change: PendingChange,
        statement: SqlStatement,
        index: int,
        started: float,
        error: str,
    ) -> ExecuteResult:
        """Roll back to the snapshot and report the failing statement (FR-7.6)."""
        state_rows[:] = snapshot
        results.append(StatementResult(change, statement, 0, False, error))
        self.transaction_log.append("ROLLBACK")
        return ExecuteResult(
            committed=False,
            results=tuple(results),
            duration_ms=_elapsed_ms(started),
            failed_index=index,
            error=error,
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _sort_value(row: dict[str, object], column: str) -> tuple[bool, Any]:
    """NULLs sort last (asc): (False, "") < (True, value); same-typed values compare."""
    value = row.get(column)
    if value is None:
        return (False, "")
    return (True, value)


def _find_row(rows: list[dict[str, object]], key: RowKey) -> int | None:
    for index, row in enumerate(rows):
        if all(row.get(column) == value for column, value in key):
            return index
    return None


def _matches(value: object | None, row_filter: RowFilter) -> bool:
    kind = row_filter.operator
    if kind is FilterOp.IS_NULL:
        return value is None
    if kind is FilterOp.IS_NOT_NULL:
        return value is not None
    if value is None:
        return False  # SQL three-valued logic: NULL never compares true
    expected = row_filter.value
    # DB scalars of one column are homogeneous; loosen typing for plain comparisons.
    left: Any = value
    right: Any = expected
    match kind:
        case FilterOp.EQ:
            return bool(left == right)
        case FilterOp.NE:
            return bool(left != right)
        case FilterOp.LT:
            return bool(left < right)
        case FilterOp.LE:
            return bool(left <= right)
        case FilterOp.GT:
            return bool(left > right)
        case FilterOp.GE:
            return bool(left >= right)
        case FilterOp.LIKE:
            return _like(value, expected)
    return False


def _like(value: object, pattern: object) -> bool:
    """SQL LIKE with % / _ wildcards, case-insensitive (default CI collation)."""
    if not isinstance(value, str) or not isinstance(pattern, str):
        return False
    regex = "".join(
        ".*" if char == "%" else "." if char == "_" else re.escape(char) for char in pattern
    )
    return re.fullmatch(regex, value, flags=re.IGNORECASE) is not None
