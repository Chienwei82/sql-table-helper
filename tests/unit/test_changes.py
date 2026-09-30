"""Tests for staged changes: PendingChange validation and changed_columns."""

import pytest

from sql_table_swiss_knife.domain import ChangeKind, PendingChange, TableRef

TABLE = TableRef(schema="dbo", name="Country")
KEY = (("Code", "DE"),)


def test_insert_validation() -> None:
    change = PendingChange(
        kind=ChangeKind.INSERT,
        table=TABLE,
        key=None,
        before=None,
        after={"Code": "DE", "Name": "Germany"},
    )
    assert change.after == {"Code": "DE", "Name": "Germany"}
    with pytest.raises(ValueError, match="must not carry a row key"):
        PendingChange(ChangeKind.INSERT, TABLE, KEY, None, {"Code": "DE"})
    with pytest.raises(ValueError, match="requires after-values"):
        PendingChange(ChangeKind.INSERT, TABLE, None, None, None)


def test_update_validation() -> None:
    change = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TABLE,
        key=KEY,
        before={"Code": "DE", "Name": "Germany"},
        after={"Code": "DE", "Name": "Deutschland"},
    )
    assert change.changed_columns == ("Name",)
    with pytest.raises(ValueError, match="UPDATE requires a row key"):
        PendingChange(ChangeKind.UPDATE, TABLE, None, {"Code": "DE"}, {"Code": "DE"})
    with pytest.raises(ValueError, match="before- and after-values"):
        PendingChange(ChangeKind.UPDATE, TABLE, KEY, None, {"Code": "DE"})


def test_delete_validation() -> None:
    change = PendingChange(
        kind=ChangeKind.DELETE,
        table=TABLE,
        key=KEY,
        before={"Code": "DE"},
        after=None,
    )
    assert change.key == KEY
    with pytest.raises(ValueError, match="DELETE requires before-values"):
        PendingChange(ChangeKind.DELETE, TABLE, KEY, None, None)
    with pytest.raises(ValueError, match="must not carry after-values"):
        PendingChange(ChangeKind.DELETE, TABLE, KEY, {"Code": "DE"}, {"Code": "DE"})


def test_changed_columns_detects_added_and_modified() -> None:
    change = PendingChange(
        kind=ChangeKind.UPDATE,
        table=TABLE,
        key=KEY,
        before={"Code": "DE", "Name": "Germany", "Population": 1},
        after={"Code": "DE", "Name": "Deutschland", "Population": 1, "Iso3": "DEU"},
    )
    assert set(change.changed_columns) == {"Name", "Iso3"}


def test_changed_columns_only_for_update() -> None:
    delete = PendingChange(
        kind=ChangeKind.DELETE, table=TABLE, key=KEY, before={"Code": "DE"}, after=None
    )
    with pytest.raises(ValueError, match="only defined for UPDATE"):
        _ = delete.changed_columns


def test_change_mappings_are_defensive_copies() -> None:
    before = {"Code": "DE"}
    change = PendingChange(
        kind=ChangeKind.UPDATE, table=TABLE, key=KEY, before=before, after=dict(before)
    )
    before["Code"] = "XX"
    assert change.before is not None
    assert change.before["Code"] == "DE"
