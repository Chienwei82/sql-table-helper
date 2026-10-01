"""Live integration tests for the SQL Server provider (marked ``live``).

They run against the docker compose server in ``tests/live`` and are skipped when the
server is not reachable. Run them with::

    cd tests/live && docker compose up -d
    uv run pytest -m live

The assertions deliberately restate the sample catalog (tests/live/init/01_sample_catalog.sql)
so a regression in the ``sys.*`` queries shows up as a concrete diff.
"""

from typing import Any

import pytest

from sql_table_swiss_knife.domain import ReferentialAction, TableKind
from sql_table_swiss_knife.providers import MetadataError
from tests.live.conftest import LiveServer

pytestmark = pytest.mark.live


async def test_connect_and_test_connection(live_server: LiveServer, mssql_provider: Any) -> None:
    info = await mssql_provider.test_connection(live_server.profile(), live_server.password)
    assert info["database_name"] == live_server.database
    assert info["server_version"]
    # SERVERPROPERTY('Edition') returns e.g. "Developer Edition (64-bit)", not a product
    # name, so asserting "SQL Server" here could never pass against a real server.
    assert "Edition" in str(info["edition"])


async def test_list_databases_contains_sample(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    provider = MssqlProvider()
    databases = await provider.list_databases(live_connection)
    names = [database.name for database in databases]
    assert "SwissKnifeSample" in names
    current = [database.name for database in databases if database.is_current]
    assert current == ["SwissKnifeSample"]
    assert "master" not in names  # system databases are filtered out


async def test_list_tables_and_views(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    summaries = await MssqlProvider().list_tables(live_connection, "dbo")
    by_name = {f"{s.schema}.{s.name}": s for s in summaries}
    assert "dbo.Country" in by_name
    assert by_name["dbo.Country"].kind is TableKind.BASE_TABLE
    assert by_name["dbo.Country"].has_primary_key is True
    assert by_name["dbo.Country"].approximate_row_count == 3
    assert by_name["dbo.Country"].has_triggers is False

    assert by_name["dbo.Region"].has_triggers is True  # AFTER UPDATE trigger
    assert by_name["dbo.v_Country"].kind is TableKind.VIEW
    assert by_name["dbo.v_Country"].has_triggers is True  # INSTEAD OF UPDATE trigger
    assert by_name["dbo.v_Country"].has_primary_key is False

    assert "dbo.RegionAlias" in by_name  # composite PK


async def test_list_tables_without_schema_filter(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    summaries = await MssqlProvider().list_tables(live_connection)
    assert len(summaries) >= 5
    # No filter passed, so every visible schema comes back: dbo plus the ones the
    # fixtures add. Asserting "all dbo" only held while dbo was the sole schema.
    assert {summary.schema for summary in summaries} >= {"dbo"}
    assert any(summary.name == "Country" for summary in summaries)
    # Ordering comes from the server (ORDER BY schema, name under the database collation),
    # which is what the schema picker shows. It is deliberately not Python's sort: the
    # collation ignores case and accents, so "catálogos" sorts before "dbo" while Python's
    # code-point order puts it last. Assert only what is guaranteed: each schema's rows
    # form one contiguous block, which is what the picker relies on.
    schemas = [summary.schema for summary in summaries]
    blocks = [
        name for index, name in enumerate(schemas) if index == 0 or schemas[index - 1] != name
    ]
    assert len(blocks) == len(set(blocks)), f"schemas are not grouped: {schemas}"


async def test_metadata_columns_exact_types(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    table = await MssqlProvider().get_table_metadata(live_connection, "dbo", "Country")
    assert table.schema == "dbo"
    assert table.name == "Country"
    assert [column.name for column in table.columns] == [
        "Code",
        "Name",
        "Iso3",
        "Population",
        "IsActive",
        "CreatedUtc",
    ]

    code = table.column("Code")
    assert (code.data_type, code.max_length, code.nullable) == ("char", 2, False)
    name = table.column("Name")
    assert (name.data_type, name.max_length) == ("nvarchar", 100)
    population = table.column("Population")
    assert (population.data_type, population.default_definition) == ("int", "((0))")
    is_active = table.column("IsActive")
    assert (is_active.data_type, is_active.default_definition) == ("bit", "((1))")
    created = table.column("CreatedUtc")
    assert created.data_type == "datetime2"
    assert created.precision == 3
    assert created.collation is None or created.collation  # nvarchar has a collation


async def test_metadata_keys_and_constraints(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    table = await MssqlProvider().get_table_metadata(live_connection, "dbo", "Country")
    assert table.primary_key is not None
    assert table.primary_key.columns == ("Code",)
    assert table.column("Code").is_primary_key

    unique_columns = {u.name: u.columns for u in table.unique_constraints}
    assert unique_columns["UQ_Country_Name"] == ("Name",)

    check_names = {c.name for c in table.check_constraints}
    assert check_names == {"CK_Country_Code", "CK_Country_Population"}
    population_check = next(c for c in table.check_constraints if c.name == "CK_Country_Population")
    assert "Population" in population_check.definition

    # no primary key is impossible here; check the no-PK table separately
    assert table.updatable is True


async def test_metadata_foreign_keys_both_directions(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    provider = MssqlProvider()
    region = await provider.get_table_metadata(live_connection, "dbo", "Region")
    fks = {fk.name: fk for fk in region.foreign_keys}
    assert set(fks) == {"FK_Region_Country", "FK_Region_Parent"}
    assert fks["FK_Region_Country"].referenced_table == "Country"
    assert fks["FK_Region_Country"].referenced_columns == ("Code",)
    assert fks["FK_Region_Country"].on_delete is ReferentialAction.CASCADE
    assert fks["FK_Region_Parent"].referenced_table == "Region"  # self-referencing
    assert region.column("CountryCode").is_foreign_key
    assert region.column("ParentRegionId").is_foreign_key

    country = await provider.get_table_metadata(live_connection, "dbo", "Country")
    incoming = {fk.name: fk for fk in country.incoming_foreign_keys}
    assert "FK_Region_Country" in incoming
    assert incoming["FK_Region_Country"].ref == "dbo.Region"
    assert incoming["FK_Region_Country"].referenced_columns == ("Code",)
    assert incoming["FK_Region_Country"].on_delete is ReferentialAction.CASCADE


async def test_metadata_identity_computed_and_rowversion(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    region = await MssqlProvider().get_table_metadata(live_connection, "dbo", "Region")
    region_id = region.column("RegionId")
    assert region_id.is_identity is True
    assert (region_id.identity_seed, region_id.identity_increment) == (1, 1)
    assert region_id.is_server_managed

    computed = region.column("NameUpper")
    assert computed.is_computed is True
    assert "UPPER" in (computed.computed_definition or "").upper()
    assert computed.computed_persisted is False
    assert computed.is_server_managed

    rowversion = region.rowversion_column
    assert rowversion is not None
    assert rowversion.name == "RowVer"
    assert rowversion.is_rowversion is True
    assert rowversion.is_server_managed


async def test_metadata_triggers(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    region = await MssqlProvider().get_table_metadata(live_connection, "dbo", "Region")
    (trigger,) = region.triggers
    assert trigger.name == "trg_Region_AfterUpdate"
    assert trigger.events == ("UPDATE",)
    assert trigger.firing == "AFTER"
    assert trigger.enabled is True

    view = await MssqlProvider().get_table_metadata(live_connection, "dbo", "v_Country")
    (view_trigger,) = view.triggers
    assert view_trigger.name == "trg_v_Country_InsteadOfUpdate"
    assert view_trigger.firing == "INSTEAD OF"
    assert view_trigger.events == ("UPDATE",)
    assert view.kind is TableKind.VIEW
    assert view.updatable is False  # views are read-only in v1 (OQ-6)


async def test_metadata_composite_primary_key(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    alias = await MssqlProvider().get_table_metadata(live_connection, "dbo", "RegionAlias")
    assert alias.primary_key is not None
    assert alias.primary_key.columns == ("RegionId", "Lang")
    assert alias.identity_columns == ("RegionId", "Lang")


async def test_metadata_missing_object_raises(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    with pytest.raises(MetadataError, match="does not exist"):
        await MssqlProvider().get_table_metadata(live_connection, "dbo", "NoSuchTable")


async def test_fetch_rows_paging(live_connection: Any) -> None:
    from sql_table_swiss_knife.domain import FetchSpec
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    provider = MssqlProvider()
    country = await provider.get_table_metadata(live_connection, "dbo", "Country")

    first = await provider.fetch_rows(live_connection, country, FetchSpec(limit=2, offset=0))
    assert first.count == 2
    assert first.has_more is True
    assert {row["Code"] for row in first.rows} <= {"DE", "FR", "JP"}

    everything = await provider.fetch_rows(live_connection, country, FetchSpec(limit=100))
    assert everything.count == 3
    assert everything.has_more is False
    assert {row["Code"] for row in everything.rows} == {"DE", "FR", "JP"}

    second = await provider.fetch_rows(live_connection, country, FetchSpec(limit=2, offset=2))
    assert {row["Code"] for row in everything.rows} == {row["Code"] for row in second.rows} | {
        row["Code"] for row in first.rows
    }


async def test_rowvalues_include_server_managed_columns(live_connection: Any) -> None:
    from sql_table_swiss_knife.domain import FetchSpec
    from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider

    provider = MssqlProvider()
    region = await provider.get_table_metadata(live_connection, "dbo", "Region")
    page = await provider.fetch_rows(live_connection, region, FetchSpec(limit=10))
    row = page.rows[0]
    assert "NameUpper" in row  # computed column is readable
    assert row["RowVer"] is not None  # rowversion is returned as bytes
    assert row["SortOrder"] == 1


async def test_disconnect_is_idempotent(live_server: LiveServer, mssql_provider: Any) -> None:
    conn = await mssql_provider.connect(live_server.profile(), live_server.password)
    await mssql_provider.disconnect(conn)
    assert conn.closed is True
    with pytest.raises(MetadataError, match="closed"):
        conn.fetch("SELECT 1")
