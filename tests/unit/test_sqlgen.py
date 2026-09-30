"""Tests for sqlgen: INSERT/UPDATE/DELETE/SELECT builders and apply ordering."""

import pytest

from sql_table_swiss_knife.domain import (
    ChangeKind,
    FetchSpec,
    FilterOp,
    PendingChange,
    RowFilter,
    SortKey,
    Table,
    TableRef,
)
from sql_table_swiss_knife.providers import (
    build_delete,
    build_insert,
    build_select,
    build_statements,
    build_update,
    sort_for_apply,
)
from sql_table_swiss_knife.providers.mssql import TSqlDialect

TABLE = TableRef(schema="dbo", name="Country")


@pytest.fixture
def dialect() -> TSqlDialect:
    return TSqlDialect()


def test_build_insert_parametrized_and_literal(dialect: TSqlDialect, country_table: Table) -> None:
    statement = build_insert(dialect, country_table, {"Name": "O'Brien", "Population": 5})
    assert statement.kind is ChangeKind.INSERT
    assert statement.sql_parametrized == (
        "INSERT INTO [dbo].[Country] ([Name], [Population]) "
        "OUTPUT INSERTED.[Code] VALUES (@p0, @p1)"
    )
    assert statement.sql_literal == (
        "INSERT INTO [dbo].[Country] ([Name], [Population]) "
        "OUTPUT INSERTED.[Code] VALUES (N'O''Brien', 5)"
    )
    assert statement.sql_script == statement.sql_literal + ";"
    assert statement.param_values == ("O'Brien", 5)
    assert [p.name for p in statement.params] == ["@p0", "@p1"]
    assert statement.row_key is None


def test_build_insert_rejects_server_managed_columns(
    dialect: TSqlDialect, country_table: Table, region_table: Table
) -> None:
    with pytest.raises(ValueError, match="computed column"):
        build_insert(dialect, country_table, {"NameUpper": "X"})
    with pytest.raises(ValueError, match="rowversion column"):
        build_insert(dialect, country_table, {"RowVer": b""})
    with pytest.raises(ValueError, match="identity column"):
        build_insert(dialect, region_table, {"RegionId": 5, "Name": "X"})
    with pytest.raises(ValueError, match="has no column"):
        build_insert(dialect, country_table, {"Nope": 1})
    with pytest.raises(ValueError, match="at least one column"):
        build_insert(dialect, country_table, {})


def test_build_update_scopes_by_key_and_rowversion(
    dialect: TSqlDialect, country_table: Table
) -> None:
    change = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TABLE,
        key=(("Code", "DE"),),
        before={"Code": "DE", "Name": "Germany", "RowVer": b"\x01"},
        after={"Code": "DE", "Name": "Deutschland", "RowVer": b"\x01"},
    )
    statement = build_update(dialect, country_table, change)
    assert statement.sql_parametrized == (
        "UPDATE [dbo].[Country] SET [Name] = @p0 WHERE [Code] = @p1 AND [RowVer] = @p2"
    )
    assert statement.sql_literal == (
        "UPDATE [dbo].[Country] SET [Name] = N'Deutschland' "
        "WHERE [Code] = N'DE' AND [RowVer] = 0x01"
    )
    assert statement.param_values == ("Deutschland", "DE", b"\x01")
    assert statement.row_key == (("Code", "DE"),)


def test_build_update_rejects_noop_and_identity_edits(
    dialect: TSqlDialect, country_table: Table, region_table: Table
) -> None:
    noop = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TABLE,
        key=(("Code", "DE"),),
        before={"Code": "DE", "Name": "Germany"},
        after={"Code": "DE", "Name": "Germany"},
    )
    with pytest.raises(ValueError, match="no changed columns"):
        build_update(dialect, country_table, noop)
    identity_edit = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TableRef("dbo", "Region"),
        key=(("RegionId", 1),),
        before={"RegionId": 1, "Name": "A"},
        after={"RegionId": 99, "Name": "A"},
    )
    with pytest.raises(ValueError, match="identity column"):
        build_update(dialect, region_table, identity_edit)


def test_build_update_requires_matching_target(dialect: TSqlDialect, country_table: Table) -> None:
    change = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TableRef("dbo", "Other"),
        key=(("Code", "DE"),),
        before={"Code": "DE", "Name": "A"},
        after={"Code": "DE", "Name": "B"},
    )
    with pytest.raises(ValueError, match=r"targets dbo\.Other"):
        build_update(dialect, country_table, change)


def test_build_delete(dialect: TSqlDialect, country_table: Table) -> None:
    change = PendingChange(
        kind=ChangeKind.DELETE,
        table=TABLE,
        key=(("Code", "DE"),),
        before={"Code": "DE", "RowVer": b"\x01"},
    )
    statement = build_delete(dialect, country_table, change)
    assert statement.sql_parametrized == (
        "DELETE FROM [dbo].[Country] WHERE [Code] = @p0 AND [RowVer] = @p1"
    )
    assert statement.sql_literal == (
        "DELETE FROM [dbo].[Country] WHERE [Code] = N'DE' AND [RowVer] = 0x01"
    )


def test_build_select_paged_sorted_filtered(dialect: TSqlDialect, country_table: Table) -> None:
    spec = FetchSpec(
        limit=2,
        offset=4,
        sort=(SortKey("Name", descending=True),),
        filters=(
            RowFilter("Population", FilterOp.GE, 1000),
            RowFilter("NameUpper", FilterOp.IS_NULL),
        ),
    )
    query = build_select(dialect, country_table, spec)
    assert query.sql == (
        "SELECT [Code], [Name], [Population], [NameUpper], [RowVer] "
        "FROM [dbo].[Country] "
        "WHERE [Population] >= @p0 AND [NameUpper] IS NULL "
        "ORDER BY [Name] DESC OFFSET 4 ROWS FETCH NEXT 2 ROWS ONLY"
    )
    assert [(p.name, p.value) for p in query.params] == [("@p0", 1000)]


def test_build_select_without_sort_uses_stable_order(
    dialect: TSqlDialect, country_table: Table
) -> None:
    query = build_select(dialect, country_table, FetchSpec(limit=3))
    assert query.sql.endswith("ORDER BY (SELECT NULL) OFFSET 0 ROWS FETCH NEXT 3 ROWS ONLY")
    assert query.params == ()


def test_build_select_rejects_unknown_columns(dialect: TSqlDialect, country_table: Table) -> None:
    with pytest.raises(ValueError, match="has no column"):
        build_select(
            dialect,
            country_table,
            FetchSpec(filters=(RowFilter("Iso3", FilterOp.EQ, "DEU"),)),
        )
    with pytest.raises(ValueError, match="has no column"):
        build_select(dialect, country_table, FetchSpec(sort=(SortKey("Iso3"),)))


def test_sort_for_apply_orders_delete_update_insert(
    country_table: Table,
) -> None:
    insert = PendingChange(
        kind=ChangeKind.INSERT, table=TABLE, key=None, before=None, after={"Code": "X"}
    )
    delete = PendingChange(
        kind=ChangeKind.DELETE, table=TABLE, key=(("Code", "Y"),), before={"Code": "Y"}
    )
    update = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TABLE,
        key=(("Code", "Z"),),
        before={"Code": "Z", "Name": "a"},
        after={"Code": "Z", "Name": "b"},
    )
    ordered = sort_for_apply([insert, delete, update])
    assert [change.kind for change in ordered] == [
        ChangeKind.DELETE,
        ChangeKind.UPDATE,
        ChangeKind.INSERT,
    ]


def test_build_statements_dispatch(dialect: TSqlDialect, country_table: Table) -> None:
    changes = [
        PendingChange(
            kind=ChangeKind.INSERT,
            table=TABLE,
            key=None,
            before=None,
            after={"Code": "ES", "Name": "Spain"},
        ),
        PendingChange(
            kind=ChangeKind.DELETE,
            table=TABLE,
            key=(("Code", "DE"),),
            before={"Code": "DE"},
        ),
    ]
    statements = build_statements(dialect, country_table, changes)
    assert [s.kind for s in statements] == [ChangeKind.INSERT, ChangeKind.DELETE]
    assert statements[0].sql_parametrized.startswith("INSERT INTO [dbo].[Country]")
    assert statements[1].sql_parametrized.startswith("DELETE FROM [dbo].[Country]")
