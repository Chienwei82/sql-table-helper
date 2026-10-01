"""End-to-end tests of MssqlProvider against a fake pyodbc driver.

These exercise the real provider code path — connect, the ``sys.*`` SQL it issues, the
row mapping, listing and the ``inspect`` CLI render — without a server, by stubbing the
``pyodbc`` module with a cursor that answers each catalog query from fixture rows.
"""

import sys
from datetime import date, datetime
from datetime import time as dt_time
from typing import Any

import pytest

from sql_table_swiss_knife.domain import (
    AuthMode,
    ChangeKind,
    Column,
    ConnectionOptions,
    ConnectionProfile,
    FetchSpec,
    PendingChange,
    ReferentialAction,
    Table,
    TableKind,
    TableRef,
)
from sql_table_swiss_knife.providers import ApplyOptions, get_provider
from sql_table_swiss_knife.providers.errors import ConnectError
from sql_table_swiss_knife.providers.mssql.dialect import TSqlDialect
from sql_table_swiss_knife.providers.mssql.errors import map_pyodbc_error
from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider, bindable, to_positional

from .test_mssql_connection import _RealShapePyodbcError

# -- fake driver --------------------------------------------------------------

#: Rows returned per catalog query, keyed by a distinctive fragment of the SQL.
RESPONSES: dict[str, list[dict[str, Any]]] = {
    "FROM sys.databases": [
        {
            "database_name": "SwissKnifeSample",
            "current_database": "SwissKnifeSample",
            "database_id": 5,
        },
    ],
    # OBJECT_LOOKUP_SQL: one object, keyed on its WHERE clause
    "o.object_id = OBJECT_ID(?)": [
        {"object_type": "U", "approximate_rows": 4},
    ],
    # table_rows_sql: a whole schema, keyed on its WHERE clause
    "AND o.type IN ('U', 'V')": [
        {
            "schema_name": "dbo",
            "table_name": "Country",
            "object_type": "U",
            "has_primary_key": 1,
            "approximate_rows": 3,
            "has_triggers": 0,
        },
        {
            "schema_name": "dbo",
            "table_name": "Region",
            "object_type": "U",
            "has_primary_key": 1,
            "approximate_rows": 4,
            "has_triggers": 1,
        },
    ],
    "FROM sys.columns": [
        {
            "column_name": "RegionId",
            "column_id": 1,
            "data_type": "int",
            "max_length": 4,
            "precision": 10,
            "scale": 0,
            "is_nullable": 0,
            "is_identity": 1,
            "is_computed": 0,
            "is_rowversion": 0,
            "collation_name": None,
            "default_definition": None,
            "seed_value": "1",
            "increment_value": "1",
            "computed_definition": None,
            "computed_persisted": None,
        },
        {
            "column_name": "Name",
            "column_id": 2,
            "data_type": "nvarchar",
            "max_length": 200,
            "precision": 0,
            "scale": 0,
            "is_nullable": 0,
            "is_identity": 0,
            "is_computed": 0,
            "is_rowversion": 0,
            "collation_name": "SQL_Latin1_General_CP1_CI_AS",
            "default_definition": None,
            "seed_value": None,
            "increment_value": None,
            "computed_definition": None,
            "computed_persisted": None,
        },
        {
            "column_name": "NameUpper",
            "column_id": 3,
            "data_type": "nvarchar",
            "max_length": 200,
            "precision": 0,
            "scale": 0,
            "is_nullable": 1,
            "is_identity": 0,
            "is_computed": 1,
            "is_rowversion": 0,
            "collation_name": "SQL_Latin1_General_CP1_CI_AS",
            "default_definition": None,
            "seed_value": None,
            "increment_value": None,
            "computed_definition": "UPPER([Name])",
            "computed_persisted": 0,
        },
        {
            "column_name": "RowVer",
            "column_id": 4,
            "data_type": "timestamp",
            "max_length": 8,
            "precision": 0,
            "scale": 0,
            "is_nullable": 0,
            "is_identity": 0,
            "is_computed": 0,
            "is_rowversion": 1,
            "collation_name": None,
            "default_definition": None,
            "seed_value": None,
            "increment_value": None,
            "computed_definition": None,
            "computed_persisted": None,
        },
    ],
    "FROM sys.indexes": [
        {
            "index_name": "PK_Region",
            "is_primary_key": 1,
            "key_ordinal": 1,
            "column_name": "RegionId",
        },
    ],
    "fk.parent_object_id = OBJECT_ID": [
        {
            "constraint_name": "FK_Region_Country",
            "constraint_column_id": 1,
            "column_name": "CountryCode",
            "referenced_schema": "dbo",
            "referenced_table": "Country",
            "referenced_column": "Code",
            "on_delete": "CASCADE",
            "on_update": "NO_ACTION",
        },
    ],
    "fk.referenced_object_id = OBJECT_ID": [
        {
            "constraint_name": "FK_RegionAlias_Region",
            "referencing_schema": "dbo",
            "referencing_table": "RegionAlias",
            "constraint_column_id": 1,
            "column_name": "RegionId",
            "referenced_column": "RegionId",
            "on_delete": "CASCADE",
            "on_update": "NO_ACTION",
        },
    ],
    "FROM sys.check_constraints": [
        {"constraint_name": "CK_Region_Name", "definition": "([Name]<>(N''))"},
    ],
    "FROM sys.triggers": [
        {
            "trigger_name": "trg_Region_AfterUpdate",
            "is_disabled": 0,
            "is_instead_of_trigger": 0,
            "event_type": 2,
        },
    ],
    "FROM sys.tables AS t": [{"temporal_type": 0}],
    "SERVERPROPERTY": [
        {
            "server_name": "SQL01",
            "server_version": "16.0.4105.2",
            "edition": "Developer Edition",
            "database_name": "SwissKnifeSample",
        },
    ],
    "SELECT [RegionId]": [
        {"RegionId": 1, "Name": "Bavaria", "NameUpper": "BAVARIA", "RowVer": b"\x00\x00"},
    ],
}


class FakeCursor:
    def __init__(self) -> None:
        self.description: list[tuple[str]] = []
        self._rows: list[dict[str, Any]] = []
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        #: Rowcount reported for DML statements, keyed by a fragment of the SQL.
        #: A value of 0 simulates a concurrency conflict (FR-7.8).
        self.rowcounts: dict[str, int] = {}
        #: Fragments of SQL that must raise, to exercise the rollback path. The value is
        #: the ``(sqlstate, vendor code, message)`` tuple a pyodbc error carries.
        self.fails: dict[str, tuple[str, int, str]] = {}
        self.rowcount = -1

    def execute(self, sql: str, params: object = None) -> None:
        bound = tuple(params) if isinstance(params, (list, tuple)) else ()
        self.executed.append((sql, bound))
        for fragment, failure in self.fails.items():
            if fragment in sql:
                sqlstate, vendor, message = failure
                raise FakePyodbc.Error(sqlstate, vendor, message)
        for fragment, rows in RESPONSES.items():
            if fragment in sql:
                self._rows = rows
                self.description = [(key,) for key in rows[0]] if rows else []
                self.rowcount = len(rows)
                return
        # unmatched SQL yields no rows, which the provider reports as "not found"
        self._rows = []
        self.description = []
        self.rowcount = next(
            (count for fragment, count in self.rowcounts.items() if fragment in sql), 1
        )

    def fetchall(self) -> list[tuple[object, ...]]:
        return [tuple(row.values()) for row in self._rows]

    def close(self) -> None:
        return None


class FakeRawConnection:
    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string
        self.closed = False
        self.cursors: list[FakeCursor] = []
        #: Configuration applied to every cursor this connection hands out.
        self.rowcounts: dict[str, int] = {}
        self.fails: dict[str, tuple[str, int, str]] = {}

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor()
        cursor.rowcounts = self.rowcounts
        cursor.fails = self.fails
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True

    def statements(self) -> list[str]:
        """Every SQL string this connection executed, in order."""
        return [sql for cursor in self.cursors for sql, _ in cursor.executed]


class FakePyodbc:
    """Minimal stand-in for the pyodbc module."""

    class Error(Exception):
        pass

    def __init__(self) -> None:
        self.connections: list[FakeRawConnection] = []
        self.kwargs: list[dict[str, Any]] = []

    def connect(self, connection_string: str, **kwargs: Any) -> FakeRawConnection:
        connection = FakeRawConnection(connection_string)
        self.connections.append(connection)
        self.kwargs.append(kwargs)
        return connection

    def Cursor(self) -> FakeCursor:
        return FakeCursor()


@pytest.fixture
def fake_pyodbc(monkeypatch: pytest.MonkeyPatch) -> FakePyodbc:
    module = FakePyodbc()
    monkeypatch.setitem(sys.modules, "pyodbc", module)
    return module


@pytest.fixture
def profile() -> ConnectionProfile:
    return ConnectionProfile(
        name="live",
        provider="mssql",
        host="localhost",
        port=1433,
        database="SwissKnifeSample",
        auth=AuthMode.SQL,
        username="sa",
        options=ConnectionOptions(trust_server_certificate=True),
    )


# -- connection ---------------------------------------------------------------


async def test_connect_uses_the_built_connection_string(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    assert conn.provider_name == "mssql"
    assert conn.server == "localhost,1433"
    assert conn.database == "SwissKnifeSample"
    assert conn.closed is False
    assert "PWD=pw" in fake_pyodbc.connections[0].connection_string
    assert "TrustServerCertificate=yes" in fake_pyodbc.connections[0].connection_string


async def test_connect_enables_autocommit(
    monkeypatch: pytest.MonkeyPatch, profile: ConnectionProfile
) -> None:
    """ODBC defaults autocommit to off, which leaves @@TRANCOUNT at 1 from the start.

    Verified against a real server: without this, reads hold open locks and a failed Apply
    leaks its transaction instead of unwinding, so the next statement sees stale locks.
    """
    module = FakePyodbc()
    monkeypatch.setattr(
        "sql_table_swiss_knife.providers.mssql.provider.import_pyodbc", lambda: module
    )

    await MssqlProvider().connect(profile, "pw")

    assert module.kwargs[0].get("autocommit") is True


async def test_connect_closes_runner_on_failure(
    monkeypatch: pytest.MonkeyPatch, profile: ConnectionProfile
) -> None:
    from sql_table_swiss_knife.providers import AuthError

    module = FakePyodbc()

    def boom(connection_string: str, **kwargs: Any) -> None:
        raise module.Error("28000", "18456", "Login failed")

    monkeypatch.setitem(sys.modules, "pyodbc", module)
    monkeypatch.setattr(module, "connect", boom)
    with pytest.raises(AuthError):
        await MssqlProvider().connect(profile, "wrong")


async def test_disconnect_closes_connection(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    await provider.disconnect(conn)
    assert conn.closed is True
    await provider.disconnect(conn)  # idempotent


async def test_test_connection_returns_server_info(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    info = await MssqlProvider().test_connection(profile, "pw")
    assert info["server_name"] == "SQL01"
    assert info["edition"] == "Developer Edition"
    assert info["database_name"] == "SwissKnifeSample"


# -- catalog ------------------------------------------------------------------


async def test_list_databases(fake_pyodbc: FakePyodbc, profile: ConnectionProfile) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    databases = await provider.list_databases(conn)
    assert [(d.name, d.is_current) for d in databases] == [("SwissKnifeSample", True)]


async def test_list_tables(fake_pyodbc: FakePyodbc, profile: ConnectionProfile) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    summaries = await provider.list_tables(conn, "dbo")
    assert [f"{s.schema}.{s.name}" for s in summaries] == ["dbo.Country", "dbo.Region"]
    assert summaries[0].has_primary_key is True
    assert summaries[0].approximate_row_count == 3
    assert summaries[1].has_triggers is True
    assert all(s.kind is TableKind.BASE_TABLE for s in summaries)


async def test_list_tables_rejects_bad_schema(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    with pytest.raises(ValueError, match="schema name"):
        await provider.list_tables(conn, "")  # empty identifiers are rejected early


async def test_get_table_metadata_end_to_end(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    table = await provider.get_table_metadata(conn, "dbo", "Region")

    assert (table.schema, table.name) == ("dbo", "Region")
    assert table.approximate_row_count == 4
    assert [c.name for c in table.columns] == ["RegionId", "Name", "NameUpper", "RowVer"]

    assert table.column("RegionId").is_identity
    assert (
        table.column("RegionId").identity_seed,
        table.column("RegionId").identity_increment,
    ) == (1, 1)
    assert table.column("Name").max_length == 100  # 200 bytes halved
    assert table.column("NameUpper").computed_persisted is False
    assert table.rowversion_column is not None

    assert table.primary_key is not None
    assert table.primary_key.columns == ("RegionId",)
    assert table.column("RegionId").is_primary_key

    (fk,) = table.foreign_keys
    assert fk.referenced_table == "Country"
    assert fk.on_delete is ReferentialAction.CASCADE

    (incoming,) = table.incoming_foreign_keys
    assert incoming.ref == "dbo.RegionAlias"

    assert [c.name for c in table.check_constraints] == ["CK_Region_Name"]
    assert [t.name for t in table.triggers] == ["trg_Region_AfterUpdate"]
    assert table.triggers[0].firing == "AFTER"
    assert table.is_system_versioned is False
    assert table.updatable is True


async def test_get_table_metadata_missing_object(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    """The object-lookup query returns no rows for an unknown name -> MetadataError."""
    from sql_table_swiss_knife.providers import MetadataError

    RESPONSES["o.object_id = OBJECT_ID(?)"] = []  # object not found
    try:
        provider = MssqlProvider()
        conn = await provider.connect(profile, "pw")
        with pytest.raises(MetadataError, match="does not exist"):
            await provider.get_table_metadata(conn, "dbo", "Ghost")
    finally:
        RESPONSES["o.object_id = OBJECT_ID(?)"] = [{"object_type": "U", "approximate_rows": 4}]


async def test_get_table_metadata_validates_identifiers(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    with pytest.raises(ValueError, match="table name"):
        await provider.get_table_metadata(conn, "dbo", "way-too-" + "x" * 200)


def _update_region() -> PendingChange:
    """An UPDATE of one Region row, scoped by its PK plus the rowversion guard."""
    return PendingChange(
        kind=ChangeKind.UPDATE,
        table=TableRef(schema="dbo", name="Region"),
        key=(("RegionId", 1),),
        before={"RegionId": 1, "Name": "Bavaria", "RowVer": b"\x00\x00"},
        after={"RegionId": 1, "Name": "Bayern", "RowVer": b"\x00\x00"},
    )


async def test_fetch_rows_orders_by_identity_and_pages(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    table = await provider.get_table_metadata(conn, "dbo", "Region")

    page = await provider.fetch_rows(conn, table, FetchSpec(limit=10, offset=0))
    assert page.count == 1
    assert page.has_more is False
    assert page.rows[0]["Name"] == "Bavaria"
    assert page.rows[0]["RowVer"] == b"\x00\x00"

    select_sql = fake_pyodbc.connections[0].cursors[-1].executed[-1][0]
    assert "ORDER BY [RegionId]" in select_sql
    assert "OFFSET 0 ROWS FETCH NEXT 11 ROWS ONLY" in select_sql


# -- provider protocol --------------------------------------------------------


def test_provider_is_registered_and_satisfies_the_protocol() -> None:
    from sql_table_swiss_knife.providers import DatabaseProvider

    provider = get_provider("mssql")
    assert isinstance(provider, MssqlProvider)
    assert isinstance(provider, DatabaseProvider)
    assert provider.quote_identifier("weird name") == "[weird name]]".replace("]]", "]")
    assert provider.dialect.name == "tsql"


async def test_execute_changes_runs_in_one_transaction_and_commits(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    table = await provider.get_table_metadata(conn, "dbo", "Region")
    before = len(fake_pyodbc.connections[0].statements())

    result = await provider.execute_changes(conn, table, [_update_region()])

    assert result.committed is True
    assert result.failed_index is None
    assert result.conflicts == ()
    assert result.statement_count == 1
    statements = fake_pyodbc.connections[0].statements()[before:]
    assert statements[0] == "BEGIN TRANSACTION"
    # Markers are positional: the dialect's @pN names are for the preview legend only.
    assert statements[1].startswith("UPDATE [dbo].[Region] SET [Name] = ?")
    assert statements[-1] == "COMMIT TRANSACTION"
    assert "ROLLBACK TRANSACTION" not in statements


async def test_execute_changes_rolls_everything_back_on_a_driver_error(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    table = await provider.get_table_metadata(conn, "dbo", "Region")
    raw = fake_pyodbc.connections[0]
    raw.fails = {
        "UPDATE [dbo].[Region]": (
            "23000",
            547,
            "The INSERT statement conflicted with the FOREIGN KEY constraint 'FK_Region_Country'.",
        )
    }
    before = len(raw.statements())

    result = await provider.execute_changes(conn, table, [_update_region()])

    assert result.committed is False
    assert result.failed_index == 0
    assert "FK_Region_Country" in (result.error or "")
    statements = raw.statements()[before:]
    assert statements[0] == "BEGIN TRANSACTION"
    assert statements[-1] == "ROLLBACK TRANSACTION"
    assert "COMMIT TRANSACTION" not in statements


async def test_execute_changes_reports_a_zero_rowcount_as_a_conflict(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    table = await provider.get_table_metadata(conn, "dbo", "Region")
    raw = fake_pyodbc.connections[0]
    raw.rowcounts = {"UPDATE [dbo].[Region]": 0}  # someone else changed the row first
    before = len(raw.statements())

    result = await provider.execute_changes(conn, table, [_update_region()])

    assert result.committed is False
    assert result.has_conflicts is True
    conflict = result.conflicts[0]
    assert conflict.row_key == (("RegionId", 1),)
    assert "0 rows affected" in conflict.reason
    assert "0 rows affected" in (result.error or "")
    assert raw.statements()[before:][-1] == "ROLLBACK TRANSACTION"


async def test_execute_changes_brackets_inserts_with_identity_insert(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    table = await provider.get_table_metadata(conn, "dbo", "Region")
    change = PendingChange(
        kind=ChangeKind.INSERT,
        table=table.ref,
        after={"RegionId": 99, "Name": "Hesse"},
    )
    before = len(fake_pyodbc.connections[0].statements())

    result = await provider.execute_changes(
        conn, table, [change], ApplyOptions(identity_insert=True)
    )

    assert result.committed is True
    statements = fake_pyodbc.connections[0].statements()[before:]
    assert statements[0] == "BEGIN TRANSACTION"
    assert statements[1] == "SET IDENTITY_INSERT [dbo].[Region] ON"
    assert "[RegionId]" in statements[2]  # the identity value is written explicitly
    assert statements[3] == "SET IDENTITY_INSERT [dbo].[Region] OFF"
    assert statements[-1] == "COMMIT TRANSACTION"


async def test_identity_insert_is_off_by_default_so_identity_values_are_rejected(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    table = await provider.get_table_metadata(conn, "dbo", "Region")
    change = PendingChange(
        kind=ChangeKind.INSERT, table=table.ref, after={"RegionId": 99, "Name": "Hesse"}
    )

    with pytest.raises(ValueError, match="identity column 'RegionId' cannot be written"):
        await provider.execute_changes(conn, table, [change])
    assert not any("IDENTITY_INSERT" in sql for sql in fake_pyodbc.connections[0].statements())


async def test_execute_changes_refuses_a_table_without_a_row_identity(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    from sql_table_swiss_knife.providers import QueryError

    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    keyless = Table(
        schema="dbo",
        name="Audit",
        kind=TableKind.BASE_TABLE,
        columns=(Column("Event", 1, "nvarchar", 50, None, None, True, None, False),),
    )

    with pytest.raises(QueryError, match="no usable row identity"):
        await provider.execute_changes(conn, keyless, [])


async def test_execute_changes_does_not_emit_identity_insert_on_a_natural_key_table(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    """``SET IDENTITY_INSERT`` on a table with no IDENTITY column is a runtime error.

    ``dbo.Country`` is keyed by a natural ``Code`` with no IDENTITY anywhere, so issuing
    ``SET IDENTITY_INSERT`` for it fails with "Table does not have the identity property".
    The SQL preview already suppressed the pair; ``execute_changes`` only checked "is
    there an INSERT", so Apply raised an error for a script the app had shown as valid —
    exactly the drift the SQL panel exists to prevent.
    """
    from sql_table_swiss_knife.domain import PrimaryKey

    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    natural = Table(
        schema="dbo",
        name="Country",
        kind=TableKind.BASE_TABLE,
        columns=(
            Column("Code", 1, "varchar", 10, None, None, False, None, False, is_primary_key=True),
            Column("Name", 2, "varchar", 50, None, None, False, None, False),
        ),
        primary_key=PrimaryKey("PK_Country", ("Code",)),
    )
    change = PendingChange(
        kind=ChangeKind.INSERT,
        table=natural.ref,
        after={"Code": "DE", "Name": "Germany"},
    )
    before = len(fake_pyodbc.connections[0].statements())

    result = await provider.execute_changes(
        conn, natural, [change], ApplyOptions(identity_insert=True)
    )

    assert result.committed is True
    emitted = fake_pyodbc.connections[0].statements()[before:]
    assert not any("IDENTITY_INSERT" in sql for sql in emitted), emitted
    assert emitted[0] == "BEGIN TRANSACTION"
    assert emitted[-1] == "COMMIT TRANSACTION"


async def test_identity_insert_is_turned_off_even_when_an_apply_rolls_back(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    """``IDENTITY_INSERT`` is a *session* setting: ROLLBACK does not undo it.

    The concurrency-conflict path returns from inside the statement loop, which used to
    jump over the ``OFF``. The connection then kept ``IDENTITY_INSERT`` ON, and SQL Server
    allows it for one session at a time — so every other session's insert into that table
    failed until the app disconnected. The module promises it is "always turned back off".
    """
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    table = await provider.get_table_metadata(conn, "dbo", "Region")
    raw = fake_pyodbc.connections[0]
    insert = PendingChange(
        kind=ChangeKind.INSERT,
        table=table.ref,
        after={"RegionId": 99, "Name": "Hesse"},
    )
    raw.rowcounts = {"UPDATE [dbo].[Region]": 0}  # somebody else changed the row first
    before = len(raw.statements())

    result = await provider.execute_changes(
        conn, table, [insert, _update_region()], ApplyOptions(identity_insert=True)
    )

    assert result.committed is False
    emitted = raw.statements()[before:]
    assert "SET IDENTITY_INSERT [dbo].[Region] ON" in emitted
    assert emitted[-1] == "SET IDENTITY_INSERT [dbo].[Region] OFF", emitted


async def test_identity_insert_is_turned_off_when_a_statement_raises(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    """The error path owes the same ``OFF``; the guarantee is not per-exit-path."""
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    table = await provider.get_table_metadata(conn, "dbo", "Region")
    raw = fake_pyodbc.connections[0]
    change = PendingChange(
        kind=ChangeKind.INSERT,
        table=table.ref,
        after={"RegionId": 99, "Name": "Hesse"},
    )
    raw.fails = {"INSERT INTO [dbo].[Region]": ("23000", 8152, "Arithmetic overflow error.")}
    before = len(raw.statements())

    result = await provider.execute_changes(
        conn, table, [change], ApplyOptions(identity_insert=True)
    )

    assert result.committed is False
    emitted = raw.statements()[before:]
    assert "SET IDENTITY_INSERT [dbo].[Region] OFF" in emitted


class TestToPositional:
    """The dialect names placeholders ``@pN``; the driver only understands ``?``.

    Sending the named form straight to the driver left the statement with no markers, so
    every filtered read and every write failed with "The SQL contains 0 parameter markers,
    but N parameters were supplied".
    """

    def test_named_placeholders_become_positional_markers(self) -> None:
        sql = "UPDATE [dbo].[T] SET [Name] = @p0 WHERE [Code] = @p1 AND [RowVer] = @p2"
        assert to_positional(sql) == (
            "UPDATE [dbo].[T] SET [Name] = ? WHERE [Code] = ? AND [RowVer] = ?"
        )

    def test_marker_count_matches_the_bound_values(self) -> None:
        sql = "INSERT INTO [dbo].[T] ([a],[b],[c]) VALUES (@p0, @p1, @p2)"
        assert to_positional(sql).count("?") == 3

    def test_order_is_preserved_so_values_stay_aligned(self) -> None:
        sql = "SELECT * FROM t WHERE a = @p0 OR b = @p1 OR c = @p2"
        assert to_positional(sql) == "SELECT * FROM t WHERE a = ? OR b = ? OR c = ?"

    def test_multi_digit_indices_are_replaced_whole(self) -> None:
        sql = "SELECT * FROM t WHERE a = @p1 AND b = @p10 AND c = @p2"
        assert to_positional(sql) == "SELECT * FROM t WHERE a = ? AND b = ? AND c = ?"

    def test_statement_without_placeholders_is_unchanged(self) -> None:
        assert to_positional("SELECT 1") == "SELECT 1"

    def test_a_column_named_like_a_placeholder_is_not_rewritten(self) -> None:
        """A bracketed identifier is a *name*, never a parameter position.

        SQL Server permits a column called ``@p0``, and the dialect renders it as
        ``[@p0]``. Rewriting that text produced ``SELECT [?] FROM ...``, which the server
        rejects with "Invalid column name '?'" (Msg 207) — so such a table was completely
        unreadable, and unwritable for the same reason.
        """
        sql = "SELECT [@p0], [@p1] FROM [dbo].[Weird] ORDER BY [@p0] OFFSET 0 ROWS"
        assert to_positional(sql) == sql

    def test_a_placeholder_shaped_string_literal_is_not_rewritten(self) -> None:
        """A quoted literal is a *value*: rewriting it would filter on a mangled string."""
        assert to_positional("SELECT * FROM t WHERE a = '@p0' AND b = @p1") == (
            "SELECT * FROM t WHERE a = '@p0' AND b = ?"
        )

    def test_doubled_delimiters_do_not_end_the_quoted_run(self) -> None:
        """``]]`` escapes an inner ``]`` and ``''`` an inner quote — both must not close."""
        assert to_positional("SELECT [a]]@p0] FROM t WHERE b = @p1") == (
            "SELECT [a]]@p0] FROM t WHERE b = ?"
        )
        assert to_positional("SELECT * FROM t WHERE a = 'it''s @p0' AND b = @p1") == (
            "SELECT * FROM t WHERE a = 'it''s @p0' AND b = ?"
        )

    def test_marker_count_still_matches_the_values_when_a_name_looks_like_one(self) -> None:
        """One real placeholder, one column called ``@p0`` — exactly one ``?``."""
        assert to_positional("SELECT [@p0] FROM t WHERE a = @p0").count("?") == 1

    def test_unterminated_quote_is_left_for_the_server_to_reject(self) -> None:
        """Never swallow the remainder of a malformed statement."""
        assert to_positional("SELECT * FROM t WHERE a = 'oops @p0") == (
            "SELECT * FROM t WHERE a = 'oops @p0"
        )


class TestBindable:
    """pyodbc sends ``time`` with a scale of 0, silently dropping the microseconds.

    ``13:45:56.123456`` was stored in a ``time(7)`` column as ``13:45:56``. Sending the
    same value as text preserves all the fractional digits the column can hold.
    """

    def test_time_with_microseconds_is_sent_as_text(self) -> None:
        value = dt_time(13, 45, 56, 123456)
        assert bindable(value) == "13:45:56.123456"

    def test_time_without_microseconds_is_left_as_a_time(self) -> None:
        value = dt_time(13, 45, 56)
        assert bindable(value) is value

    def test_other_types_pass_through_untouched(self) -> None:
        for value in (1, "text", None, date(2024, 2, 29), datetime(2024, 2, 29, 1, 2, 3)):
            assert bindable(value) is value


class TestTlsErrorMapping:
    """A TLS failure arrives as 08001, the same state as an unreachable host."""

    def test_certificate_failure_mentions_tls_not_the_firewall(self) -> None:
        message = (
            "[08001] [Microsoft][ODBC Driver 18 for SQL Server]SSL Provider: "
            "[error:0A000086:SSL routines::certificate verify failed:self-signed certificate] "
            "(-1) (SQLDriverConnect)"
        )
        mapped = map_pyodbc_error(_RealShapePyodbcError("08001", message))
        assert isinstance(mapped, ConnectError)
        assert "TLS" in str(mapped)
        assert "certificate" in str(mapped).lower()

    def test_a_genuinely_unreachable_host_keeps_the_reachability_wording(self) -> None:
        mapped = map_pyodbc_error(
            _RealShapePyodbcError("08001", "TCP Provider: No connection could be made")
        )
        assert isinstance(mapped, ConnectError)
        assert "cannot reach" in str(mapped)

    def test_a_timeout_is_still_reported_as_a_timeout(self) -> None:
        mapped = map_pyodbc_error(_RealShapePyodbcError("HYT00", "Timeout expired"))
        assert "timed out" in str(mapped)


class TestQuotedObjectTarget:
    """``OBJECT_ID`` parses brackets, so a name with ``]`` must be escaped for it.

    ``f"[{schema}].[{name}]"`` produced ``[Lookups].[Weird ]Name]``, which resolves to no
    object: the table exists but every metadata read claimed it did not.
    """

    def test_closing_bracket_in_a_name_is_doubled(self) -> None:
        dialect = TSqlDialect()
        assert dialect.quote_qualified("Lookups", "Weird ]Name") == "[Lookups].[Weird ]]Name]"

    def test_plain_names_are_unchanged(self) -> None:
        dialect = TSqlDialect()
        assert dialect.quote_qualified("dbo", "Region") == "[dbo].[Region]"

    def test_accented_names_are_preserved(self) -> None:
        dialect = TSqlDialect()
        assert dialect.quote_qualified("catálogos", "Moneda") == "[catálogos].[Moneda]"
