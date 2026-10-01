"""Tests for sqlgen: INSERT/UPDATE/DELETE/SELECT builders and apply ordering."""

from dataclasses import replace

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
    ApplyOptions,
    build_delete,
    build_insert,
    build_merge,
    build_script,
    build_select,
    build_statements,
    build_table_insert,
    build_update,
    is_concurrency_conflict,
    needs_identity_insert,
    sort_for_apply,
)
from sql_table_swiss_knife.providers.mssql import TSqlDialect

TABLE = TableRef(schema="dbo", name="Country")


@pytest.fixture
def dialect() -> TSqlDialect:
    return TSqlDialect()


@pytest.fixture
def composite_table() -> Table:
    """A table whose row identity is two columns, as ``dbo.RegionAlias`` is live.

    Keyset paging only engages for an ascending sort over *exactly* the identity
    columns, so the single-key fixtures could never reach the multi-column path.
    """
    from sql_table_swiss_knife.domain import Column, PrimaryKey, TableKind

    return Table(
        schema="dbo",
        name="RegionAlias",
        kind=TableKind.BASE_TABLE,
        columns=(
            Column("A", 1, "int", None, 10, 0, False, None, False, is_primary_key=True),
            Column("B", 2, "char", 2, None, None, False, None, False, is_primary_key=True),
        ),
        primary_key=PrimaryKey("PK_RegionAlias", ("A", "B")),
    )


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


# -- M5: identity inserts, compare-all concurrency, keyset paging ---------------


def test_an_identity_column_cannot_be_written_by_default(
    dialect: TSqlDialect, region_table: Table
) -> None:
    with pytest.raises(ValueError, match="identity column 'RegionId' cannot be written"):
        build_insert(dialect, region_table, {"RegionId": 5, "Name": "Hesse"})


def test_identity_insert_mode_writes_the_identity_value(
    dialect: TSqlDialect, region_table: Table
) -> None:
    statement = build_insert(
        dialect,
        region_table,
        {"RegionId": 5, "Name": "Hesse"},
        identity_insert=True,
    )
    assert statement.sql_parametrized.startswith("INSERT INTO [dbo].[Region] ([RegionId], [Name])")
    assert statement.param_values == (5, "Hesse")


def test_identity_insert_is_the_only_way_to_bypass_the_identity_guard(
    dialect: TSqlDialect, region_table: Table
) -> None:
    # Computed and rowversion columns stay unwritable even in IDENTITY_INSERT mode.
    with pytest.raises(ValueError, match="computed column 'NameUpper' cannot be written"):
        build_insert(dialect, region_table, {"NameUpper": "X"}, identity_insert=True)
    with pytest.raises(ValueError, match="rowversion column 'RowVer' cannot be written"):
        build_insert(dialect, region_table, {"RowVer": b"\x00"}, identity_insert=True)


def test_a_null_original_value_is_compared_with_is_null(
    dialect: TSqlDialect, region_table: Table
) -> None:
    change = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TableRef(schema="dbo", name="Region"),
        key=(("RegionId", 1),),
        before={"RegionId": 1, "Name": "Bavaria", "ParentRegionId": None},
        after={"RegionId": 1, "Name": "Bayern", "ParentRegionId": None},
    )
    statement = build_update(dialect, region_table, change, compare_original=True)
    # WHERE [col] = NULL never matches, so the NULL guard must use IS NULL.
    assert "AND [ParentRegionId] IS NULL" in statement.sql_parametrized
    # IS NULL binds nothing, so the NULL guard contributes no parameter either: a driver
    # rejects a value count that does not match the statement's markers.
    assert statement.param_values == ("Bayern", 1)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"RegionId": 1, "Name": "Bavaria", "ParentRegionId": None}, {"Name": "Bayern"}),
        ({"RegionId": 1, "Name": "Bavaria", "SortOrder": None}, {"Name": "Bayern"}),
    ],
)
def test_bound_parameter_count_matches_the_placeholder_count(
    dialect: TSqlDialect, region_table: Table, before: dict[str, object], after: dict[str, object]
) -> None:
    """A driver rejects a value count that does not match the statement's markers.

    ``IS NULL`` renders no placeholder, so allocating a parameter for it produced
    statements with more bound values than markers — the failure was invisible until
    Apply reached a real driver, because the tests bind against a fake cursor.
    """
    update = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TableRef(schema="dbo", name="Region"),
        key=(("RegionId", 1),),
        before=before,
        after=after,
    )
    delete = replace(update, kind=ChangeKind.DELETE, after=None)
    for statement in (
        build_update(dialect, region_table, update, compare_original=True),
        build_delete(dialect, region_table, delete, compare_original=True),
    ):
        markers = statement.sql_parametrized.count("@p")
        assert len(statement.params) == markers
        assert len(statement.param_values) == markers


def test_placeholder_indices_stay_contiguous_across_a_null_guard(
    dialect: TSqlDialect, region_table: Table
) -> None:
    """Indices are allocated left-to-right over the conditions that actually bind.

    The unchanged NULL column is a WHERE guard, so it renders no marker between two
    conditions that do — the numbering must not skip an index just because it skipped a
    value.
    """
    change = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TableRef(schema="dbo", name="Region"),
        key=(("RegionId", 1),),
        before={"RegionId": 1, "Name": "Bavaria", "ParentRegionId": None, "SortOrder": 3},
        after={"Name": "Bayern"},
    )
    statement = build_update(dialect, region_table, change, compare_original=True)
    assert statement.sql_parametrized == (
        "UPDATE [dbo].[Region] SET [Name] = @p0 "
        "WHERE [RegionId] = @p1 AND [ParentRegionId] IS NULL AND [SortOrder] = @p2"
    )
    assert statement.param_values == ("Bayern", 1, 3)


def test_compare_original_adds_every_unchanged_column_to_the_where_clause(
    dialect: TSqlDialect, region_table: Table
) -> None:
    change = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TableRef(schema="dbo", name="Region"),
        key=(("RegionId", 1),),
        before={"RegionId": 1, "Name": "Bavaria", "SortOrder": 3},
        after={"RegionId": 1, "Name": "Bayern", "SortOrder": 3},
    )
    without = build_update(dialect, region_table, change)
    assert without.sql_parametrized == (
        "UPDATE [dbo].[Region] SET [Name] = @p0 WHERE [RegionId] = @p1"
    )
    with_guard = build_update(dialect, region_table, change, compare_original=True)
    assert with_guard.sql_parametrized == (
        "UPDATE [dbo].[Region] SET [Name] = @p0 WHERE [RegionId] = @p1 AND [SortOrder] = @p2"
    )


def test_compare_original_never_re_checks_a_column_the_statement_writes(
    dialect: TSqlDialect, region_table: Table
) -> None:
    change = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TableRef(schema="dbo", name="Region"),
        key=(("RegionId", 1),),
        before={"RegionId": 1, "Name": "Bavaria", "SortOrder": 3},
        after={"RegionId": 1, "Name": "Bayern", "SortOrder": 9},
    )
    statement = build_update(dialect, region_table, change, compare_original=True)
    # [SortOrder] is being written, so re-checking its old value could never match.
    assert statement.sql_parametrized.count("[SortOrder]") == 1
    assert "[SortOrder] = @p" not in statement.sql_parametrized.split("WHERE")[1]


def test_a_rowversion_table_prefers_the_rowversion_guard_over_compare_all(
    dialect: TSqlDialect, region_table: Table
) -> None:
    change = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TableRef(schema="dbo", name="Region"),
        key=(("RegionId", 1),),
        before={"RegionId": 1, "Name": "Bavaria", "RowVer": b"\x00\x01", "SortOrder": 3},
        after={"RegionId": 1, "Name": "Bayern", "RowVer": b"\x00\x01", "SortOrder": 3},
    )
    statement = build_update(dialect, region_table, change, compare_original=True)
    assert statement.sql_parametrized == (
        "UPDATE [dbo].[Region] SET [Name] = @p0 WHERE [RegionId] = @p1 AND [RowVer] = @p2"
    )
    assert "[SortOrder]" not in statement.sql_parametrized


def test_compare_original_guards_a_delete_with_all_original_values(
    dialect: TSqlDialect, country_table: Table
) -> None:
    change = PendingChange(
        kind=ChangeKind.DELETE,
        table=TABLE,
        key=(("Code", "DE"),),
        before={"Code": "DE", "Name": "Germany"},
    )
    statement = build_delete(dialect, country_table, change, compare_original=True)
    assert statement.sql_parametrized == (
        "DELETE FROM [dbo].[Country] WHERE [Code] = @p0 AND [Name] = @p1"
    )


def test_a_rowversion_delete_uses_the_rowversion_not_every_column(
    dialect: TSqlDialect, country_table: Table
) -> None:
    change = PendingChange(
        kind=ChangeKind.DELETE,
        table=TABLE,
        key=(("Code", "DE"),),
        before={"Code": "DE", "Name": "Germany", "RowVer": b"\x00\x09"},
    )
    statement = build_delete(dialect, country_table, change, compare_original=True)
    assert statement.sql_parametrized == (
        "DELETE FROM [dbo].[Country] WHERE [Code] = @p0 AND [RowVer] = @p1"
    )


def test_build_statements_passes_the_options_through(
    dialect: TSqlDialect, region_table: Table
) -> None:
    change = PendingChange(
        kind=ChangeKind.INSERT,
        table=TableRef(schema="dbo", name="Region"),
        after={"RegionId": 5, "Name": "Hesse"},
    )
    with pytest.raises(ValueError, match="identity column"):
        build_statements(dialect, region_table, [change])
    statements = build_statements(
        dialect, region_table, [change], options=ApplyOptions(identity_insert=True)
    )
    assert "[RegionId]" in statements[0].sql_parametrized


def test_keyset_paging_asks_for_rows_after_the_given_key(
    dialect: TSqlDialect, country_table: Table
) -> None:
    select = build_select(
        dialect,
        country_table,
        FetchSpec(limit=10, sort=(SortKey("Code"),), after_key=(("Code", "DE"),)),
    )
    assert "WHERE ([Code] > @p0)" in select.sql
    assert "OFFSET 0 ROWS" in select.sql
    assert select.params[0].value == "DE"


def test_keyset_paging_expands_a_composite_key_into_t_sql(
    dialect: TSqlDialect, composite_table: Table
) -> None:
    """``(a, b) > (@p0, @p1)`` is a syntax error in T-SQL — it is PostgreSQL syntax.

    SQL Server answered it with "An expression of non-boolean type specified in a
    context where a condition is expected" (Msg 4145), so every page after the first
    failed on a composite-key table. The single-key case happened to be valid
    (``(x) > (@p0)`` is only parenthesization), which is why it went unnoticed.
    """
    select = build_select(
        dialect,
        composite_table,
        FetchSpec(
            limit=10,
            sort=(SortKey("A"), SortKey("B")),
            after_key=(("A", 1), ("B", 2)),
        ),
    )
    assert "([A], [B]) >" not in select.sql
    assert "[A] > @p0 OR [A] = @p1 AND [B] > @p2" in select.sql
    # Three markers, so three bound values — the earlier key is repeated as a value,
    # each with its own marker, because ODBC binds strictly positionally.
    assert select.sql.count("@p") == 3
    assert [param.value for param in select.params] == [1, 1, 2]
    assert [param.column for param in select.params] == ["A", "A", "B"]


def test_keyset_marker_order_matches_the_rendered_predicate(
    dialect: TSqlDialect, composite_table: Table
) -> None:
    """The SQL and the bound values must not be able to disagree about the marker count.

    This is the invariant that made the real driver reject the statement with "The SQL
    contains 3 parameter markers, but 2 parameters were supplied".
    """
    from sql_table_swiss_knife.providers.mssql.provider import to_positional

    select = build_select(
        dialect,
        composite_table,
        FetchSpec(
            limit=10,
            sort=(SortKey("A"), SortKey("B")),
            after_key=(("A", 1), ("B", 2)),
        ),
    )
    assert to_positional(select.sql).count("?") == len(select.param_values)


def test_keyset_paging_is_skipped_for_a_user_chosen_sort(
    dialect: TSqlDialect, country_table: Table
) -> None:
    # A descending sort reorders rows, so "after this key" is not the next page.
    select = build_select(
        dialect,
        country_table,
        FetchSpec(limit=10, sort=(SortKey("Name", descending=True),), after_key=(("Code", "DE"),)),
    )
    assert ">" not in select.sql
    assert select.params == ()


def test_filters_and_the_keyset_cursor_share_placeholder_order(
    dialect: TSqlDialect, country_table: Table
) -> None:
    select = build_select(
        dialect,
        country_table,
        FetchSpec(
            limit=10,
            sort=(SortKey("Code"),),
            filters=(RowFilter("Name", FilterOp.LIKE, "%er%"),),
            after_key=(("Code", "DE"),),
        ),
    )
    assert select.sql.index("[Name] LIKE @p0") < select.sql.index("[Code] > @p1")
    assert [param.name for param in select.params] == ["@p0", "@p1"]


# -- generated batches (MERGE / multi-row INSERT) ---------------------------


def test_merge_refuses_a_batch_where_a_row_is_missing_the_key(
    dialect: TSqlDialect, country_table: Table
) -> None:
    """All-or-nothing: a NULL-key source row never matches and would INSERT a NULL key.

    The row used to be skipped when the column list was built but still rendered, so the
    MERGE silently carried a NULL key into a NOT MATCHED INSERT.
    """
    with pytest.raises(ValueError, match="every key column"):
        build_merge(dialect, country_table, [{"Code": "DE", "Name": "Germany"}, {"Name": "Rome"}])
    assert "NULL" not in build_merge(dialect, country_table, [{"Code": "DE", "Name": "Germany"}])


def test_merge_fills_a_column_another_row_lacks_with_null(
    dialect: TSqlDialect, country_table: Table
) -> None:
    """``build_table_insert`` already filled gaps with NULL; ``build_merge`` raised.

    The column list is unioned across the rows, so a row that simply lacks a column
    another row has is normal. A direct subscript raised a bare ``KeyError``, which
    escaped the "refuse with a reason" contract and crashed the SQL action.
    """
    sql = build_merge(
        dialect,
        country_table,
        [{"Code": "DE", "Name": "Germany"}, {"Code": "FR", "Population": 3}],
    )
    assert "(N'DE', N'Germany', NULL)" in sql
    assert "(N'FR', NULL, 3)" in sql


def test_table_insert_drops_rows_with_nothing_writable(
    dialect: TSqlDialect, region_table: Table
) -> None:
    """A row whose only column is server-managed becomes an all-NULL row otherwise.

    The docstring promised the drop; the implementation emitted the row anyway.
    """
    # RegionId is an identity column, so {"RegionId": n} has nothing writable in it.
    assert build_table_insert(dialect, region_table, [{"RegionId": 1}, {"RegionId": 2}]) == ""
    sql = build_table_insert(
        dialect, region_table, [{"RegionId": 1, "Name": "Bavaria"}, {"RegionId": 2}]
    )
    assert "(N'Bavaria')" in sql
    assert sql.count("VALUES") == 1
    assert "NULL" not in sql


def test_needs_identity_insert_is_false_for_a_table_with_no_identity_column(
    dialect: TSqlDialect, country_table: Table
) -> None:
    """Both the script and Apply must ask the same question, or one of them lies.

    ``Country`` has a natural ``Code`` key and no IDENTITY column, so ``SET
    IDENTITY_INSERT`` on it is a runtime error ("Table does not have the identity
    property"). The script already suppressed it; ``execute_changes`` did not, so the
    SQL panel showed a script that worked while Apply raised — breaking the promise
    that the SQL on screen is the SQL that runs.
    """
    statements = build_statements(
        dialect,
        country_table,
        [
            PendingChange(
                kind=ChangeKind.INSERT,
                table=TABLE,
                after={"Code": "DE", "Name": "Germany"},
            )
        ],
        options=ApplyOptions(identity_insert=True),
    )
    assert any(s.kind is ChangeKind.INSERT for s in statements)
    # The script already gets this right; the predicate below is what Apply must use.
    assert "IDENTITY_INSERT" not in build_script(
        dialect, country_table, statements, identity_insert=True
    )
    assert not needs_identity_insert(country_table, statements, identity_insert=True)


def test_needs_identity_insert_is_true_for_a_real_identity_insert(
    dialect: TSqlDialect, region_table: Table
) -> None:
    statements = build_statements(
        dialect,
        region_table,
        [
            PendingChange(
                kind=ChangeKind.INSERT, table=TABLE, after={"RegionId": 7, "Name": "Bavaria"}
            )
        ],
        options=ApplyOptions(identity_insert=True),
    )
    assert needs_identity_insert(region_table, statements, identity_insert=True)


def test_needs_identity_insert_is_false_when_not_opted_in(
    dialect: TSqlDialect, region_table: Table
) -> None:
    """Without the explicit opt-in there is nothing to bracket, even on an identity table."""
    statements = build_statements(
        dialect,
        region_table,
        [
            PendingChange(
                kind=ChangeKind.INSERT, table=TABLE, after={"RegionId": 7, "Name": "Bavaria"}
            )
        ],
        options=ApplyOptions(identity_insert=True),
    )
    assert not needs_identity_insert(region_table, statements, identity_insert=False)


def test_a_zero_row_update_or_delete_is_a_concurrency_conflict() -> None:
    """FR-7.8: the row I fetched is no longer what the database holds."""
    update = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TABLE,
        key=(("Code", "DE"),),
        before={"Name": "Germany"},
        after={"Name": "Deutschland"},
    )
    delete = PendingChange(
        kind=ChangeKind.DELETE, table=TABLE, key=(("Code", "DE"),), before={"Name": "Germany"}
    )
    assert is_concurrency_conflict(update, 0)
    assert is_concurrency_conflict(delete, 0)
    assert not is_concurrency_conflict(update, 1), "a matched row is not a conflict"


def test_a_zero_row_insert_is_not_a_concurrency_conflict() -> None:
    """An INSERT reporting 0 rows must not abort an otherwise healthy Apply.

    The check used to be inline in the driver loop, where the INSERT case was excluded by
    a compound condition. Stating it as its own function keeps that from drifting, and
    keeps the decision testable without a fake driver.
    """
    insert = PendingChange(kind=ChangeKind.INSERT, table=TABLE, after={"Name": "Germany"})
    assert not is_concurrency_conflict(insert, 0)
