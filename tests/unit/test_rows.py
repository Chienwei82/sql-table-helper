"""Tests for row models: Row, RowKey, FetchSpec, RowPage, filters, sorting."""

import pytest

from sql_table_swiss_knife.domain import (
    FetchSpec,
    FilterOp,
    Row,
    RowFilter,
    RowPage,
    SortKey,
    make_row_key,
)


def test_row_is_defensive_copy() -> None:
    source = {"A": 1}
    row = Row(source)
    source["A"] = 999
    assert row["A"] == 1
    assert "A" in row
    assert row.get("Missing") is None
    assert row.get("Missing", 5) == 5


def test_make_row_key() -> None:
    key = make_row_key(("Code", "Name"), {"Code": "DE", "Name": "Germany", "X": 1})
    assert key == (("Code", "DE"), ("Name", "Germany"))


def test_make_row_key_requires_columns_and_presence() -> None:
    with pytest.raises(ValueError, match="at least one column"):
        make_row_key((), {"A": 1})
    with pytest.raises(KeyError, match="missing"):
        make_row_key(("Nope",), {"A": 1})


def test_filter_op_needs_value() -> None:
    assert FilterOp.EQ.needs_value
    assert FilterOp.LIKE.needs_value
    assert not FilterOp.IS_NULL.needs_value
    assert not FilterOp.IS_NOT_NULL.needs_value


def test_row_filter_validation() -> None:
    with pytest.raises(ValueError, match="requires a value"):
        RowFilter("A", FilterOp.EQ, None)
    assert RowFilter("A", FilterOp.IS_NULL).value is None
    with pytest.raises(ValueError, match="filter column"):
        RowFilter("", FilterOp.EQ, 1)


def test_fetch_spec_validation() -> None:
    assert FetchSpec().limit == 1000  # S-8 default
    with pytest.raises(ValueError, match="limit"):
        FetchSpec(limit=0)
    with pytest.raises(ValueError, match="offset"):
        FetchSpec(offset=-1)


def test_sort_key_validation() -> None:
    assert not SortKey("Code").descending
    with pytest.raises(ValueError, match="sort column"):
        SortKey("")


def test_row_page_validation_and_helpers() -> None:
    page = RowPage(rows=(Row({"A": 1}),), offset=0, limit=10, has_more=False)
    assert page.count == 1
    assert not page.is_empty
    with pytest.raises(ValueError, match="limit"):
        RowPage(rows=(Row({"A": 1}),), offset=0, limit=0, has_more=False)
    with pytest.raises(ValueError, match="but limit"):
        RowPage(rows=(Row({"A": 1}), Row({"A": 2})), offset=0, limit=1, has_more=True)
    with pytest.raises(ValueError, match="total_row_count"):
        RowPage(rows=(), offset=0, limit=5, has_more=False, total_row_count=-1)
