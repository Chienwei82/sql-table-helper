"""GridView: sorting, quick filtering and column visibility as pure state (FR-3.2/3.3).

The point of these tests is that *filtering and sorting are requests to the server*, not
Python list operations: the predicates below are what becomes SQL, so a row that was never
loaded can still be found by a filter (S-8).
"""

import pytest

from sql_table_swiss_knife.domain import (
    Column,
    FilterOp,
    PrimaryKey,
    RowFilter,
    SortKey,
    Table,
    TableKind,
)
from sql_table_swiss_knife.providers.mssql import TSqlDialect
from sql_table_swiss_knife.services.view import (
    FilterMode,
    GridView,
    QuickFilter,
    filter_for,
    toggle_sort,
    toggle_visible,
    visible_columns,
)

#: The filter text is escaped in the dialect's LIKE syntax, so these tests pin one.
DIALECT = TSqlDialect()

TABLE = Table(
    schema="dbo",
    name="Country",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Code", 1, "char", 2, None, None, False, None, False, is_primary_key=True),
        Column("Name", 2, "nvarchar", 100, None, None, False, None, False),
        Column("Population", 3, "int", None, 10, 0, False, None, False),
    ),
    primary_key=PrimaryKey("PK_Country", ("Code",)),
)


# -- quick filter --------------------------------------------------------------


def test_contains_becomes_a_like_predicate() -> None:
    predicates = filter_for((QuickFilter("Name", "er", FilterMode.CONTAINS),), DIALECT)

    assert predicates == (RowFilter("Name", FilterOp.LIKE, "%er%"),)


def test_equals_becomes_an_equality_predicate() -> None:
    predicates = filter_for((QuickFilter("Name", "Germany", FilterMode.EQUALS),), DIALECT)

    assert predicates == (RowFilter("Name", FilterOp.EQ, "Germany"),)


def test_a_blank_term_means_no_filter_at_all() -> None:
    assert filter_for((QuickFilter("Name", "   "),), DIALECT) == ()
    assert QuickFilter("Name", "").is_empty is True
    assert QuickFilter("Name", "x").is_empty is False


def test_typed_wildcards_are_escaped_so_they_match_literally() -> None:
    predicates = filter_for((QuickFilter("Name", "50%_off", FilterMode.CONTAINS),), DIALECT)

    # Only the metacharacters are escaped: % and _ become [%] and [_].
    assert predicates == (RowFilter("Name", FilterOp.LIKE, "%50[%][_]off%"),)


def test_several_filters_become_several_predicates() -> None:
    predicates = filter_for(
        (QuickFilter("Name", "er"), QuickFilter("Code", "DE", FilterMode.EQUALS)), DIALECT
    )

    assert len(predicates) == 2
    assert [p.operator for p in predicates] == [FilterOp.LIKE, FilterOp.EQ]


def test_a_quick_filter_needs_a_column() -> None:
    with pytest.raises(ValueError, match="needs a column"):
        QuickFilter("", "x")


def test_the_filter_label_explains_what_is_applied() -> None:
    assert QuickFilter("Name", "").describe() == "no filter"
    assert QuickFilter("Name", "er").describe() == "Name contains 'er'"
    assert QuickFilter("Code", "DE", FilterMode.EQUALS).describe() == "Code equals 'DE'"


# -- sorting -------------------------------------------------------------------


def test_sorting_cycles_ascending_descending_off() -> None:
    first = toggle_sort((), "Name")
    second = toggle_sort(first, "Name")
    third = toggle_sort(second, "Name")

    assert first == (SortKey("Name"),)
    assert second == (SortKey("Name", descending=True),)
    assert third == ()


def test_sorting_a_second_column_replaces_the_first() -> None:
    result = toggle_sort((SortKey("Name"),), "Population")

    assert result == (SortKey("Population"),)


def test_the_sort_label_uses_arrows() -> None:
    assert GridView().sort_label() == "unsorted"
    assert GridView(sort=(SortKey("Name"),)).sort_label() == "Name ↑"
    assert GridView(sort=(SortKey("Name", True),)).sort_label() == "Name ↓"


# -- column visibility ---------------------------------------------------------


def test_toggling_a_column_hides_and_shows_it() -> None:
    hidden = toggle_visible(frozenset(), "Population")
    assert hidden == frozenset({"Population"})
    assert toggle_visible(hidden, "Population") == frozenset()


def test_visible_columns_keeps_the_table_order() -> None:
    columns = visible_columns(TABLE, frozenset({"Name"}))

    assert [column.name for column in columns] == ["Code", "Population"]


def test_hiding_a_column_that_does_not_exist_is_harmless() -> None:
    assert visible_columns(TABLE, frozenset({"Gone"})) == TABLE.columns


# -- the view as a whole -------------------------------------------------------


def test_a_default_view_shows_everything() -> None:
    view = GridView()

    assert view.columns(TABLE) == TABLE.columns
    assert view.is_filtered is False
    assert view.filter_label() == "no filter"
    assert view.predicates(DIALECT) == ()


def test_the_default_order_is_the_table_identity() -> None:
    assert GridView().sort_for(TABLE) == (SortKey("Code"),)


def test_a_chosen_sort_overrides_the_default() -> None:
    view = GridView().with_sort("Name")

    assert view.sort_for(TABLE) == (SortKey("Name"),)


def test_setting_a_filter_for_a_column_replaces_the_previous_one() -> None:
    view = GridView().with_filter(QuickFilter("Name", "er")).with_filter(QuickFilter("Name", "fra"))

    assert len(view.filters) == 1
    assert view.filters[0].text == "fra"
    assert view.is_filtered is True


def test_a_blank_filter_clears_that_column() -> None:
    view = GridView().with_filter(QuickFilter("Name", "er")).with_filter(QuickFilter("Name", ""))

    assert view.filters == ()
    assert view.is_filtered is False


def test_clearing_a_filter_keeps_the_others() -> None:
    view = (
        GridView()
        .with_filter(QuickFilter("Name", "er"))
        .with_filter(QuickFilter("Code", "DE", FilterMode.EQUALS))
    )

    cleared = view.with_filter_cleared("Name")

    assert [quick.column for quick in cleared.filters] == ["Code"]


def test_the_filter_label_lists_every_active_filter() -> None:
    view = (
        GridView()
        .with_filter(QuickFilter("Name", "er"))
        .with_filter(QuickFilter("Code", "DE", FilterMode.EQUALS))
    )

    assert view.filter_label() == "Name contains 'er' · Code equals 'DE'"
