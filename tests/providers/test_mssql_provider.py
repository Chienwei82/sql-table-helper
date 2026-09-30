"""End-to-end tests of MssqlProvider against a fake pyodbc driver.

These exercise the real provider code path — connect, the ``sys.*`` SQL it issues, the
row mapping, listing and the ``inspect`` CLI render — without a server, by stubbing the
``pyodbc`` module with a cursor that answers each catalog query from fixture rows.
"""

import sys
from typing import Any

import pytest

from sql_table_swiss_knife.domain import (
    AuthMode,
    ConnectionOptions,
    ConnectionProfile,
    FetchSpec,
    ReferentialAction,
    TableKind,
)
from sql_table_swiss_knife.providers import get_provider
from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

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

    def execute(self, sql: str, params: object = None) -> None:
        bound = tuple(params) if isinstance(params, (list, tuple)) else ()
        self.executed.append((sql, bound))
        for fragment, rows in RESPONSES.items():
            if fragment in sql:
                self._rows = rows
                self.description = [(key,) for key in rows[0]] if rows else []
                return
        # unmatched SQL yields no rows, which the provider reports as "not found"
        self._rows = []
        self.description = []

    def fetchall(self) -> list[tuple[object, ...]]:
        return [tuple(row.values()) for row in self._rows]

    def close(self) -> None:
        return None


class FakeRawConnection:
    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string
        self.closed = False
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor()
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


class FakePyodbc:
    """Minimal stand-in for the pyodbc module."""

    class Error(Exception):
        pass

    def __init__(self) -> None:
        self.connections: list[FakeRawConnection] = []

    def connect(self, connection_string: str, **kwargs: Any) -> FakeRawConnection:
        connection = FakeRawConnection(connection_string)
        self.connections.append(connection)
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


async def test_execute_changes_is_not_implemented_yet(
    fake_pyodbc: FakePyodbc, profile: ConnectionProfile
) -> None:
    provider = MssqlProvider()
    conn = await provider.connect(profile, "pw")
    table = await provider.get_table_metadata(conn, "dbo", "Region")
    with pytest.raises(NotImplementedError, match="Milestone 6"):
        await provider.execute_changes(conn, table, [])
