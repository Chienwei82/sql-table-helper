"""Tests for staged changes: PendingChange validation and changed_columns."""

import pytest

from sql_table_swiss_knife.domain import (
    ChangeKind,
    ChangeSet,
    PendingChange,
    TableRef,
    describe_changes,
    is_new_row,
    row_label,
)

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


# -- ChangeSet: the staging area (M5) -----------------------------------------


def _changeset() -> ChangeSet:
    return ChangeSet(TABLE)


def _germany() -> dict[str, object]:
    return {"Code": "DE", "Name": "Germany", "Population": 83_000_000}


def test_a_new_changeset_is_empty_and_says_so() -> None:
    changes = _changeset()
    assert changes.is_empty is True
    assert len(changes) == 0
    assert changes.summary == "no changes"
    assert changes.can_undo is False
    assert changes.can_redo is False
    assert changes.undo() is False
    assert changes.redo() is False


def test_editing_a_cell_stages_one_update() -> None:
    changes = _changeset()
    original = _germany()

    staged = changes.stage_cell(KEY, "Name", "Deutschland", original)

    assert staged is not None
    assert staged.key == KEY
    assert staged.kind is ChangeKind.UPDATE
    assert staged.change.before == original
    assert staged.change.after is not None
    assert staged.change.after["Name"] == "Deutschland"
    assert changes.counts == {ChangeKind.INSERT: 0, ChangeKind.UPDATE: 1, ChangeKind.DELETE: 0}
    assert changes.summary == "1 update"


def test_editing_two_cells_of_one_row_collapses_into_a_single_update() -> None:
    changes = _changeset()
    original = _germany()

    changes.stage_cell(KEY, "Name", "Deutschland", original)
    staged = changes.stage_cell(KEY, "Population", 84_000_000, original)

    assert len(changes) == 1
    assert staged is not None
    assert set(staged.change.changed_columns) == {"Name", "Population"}


def test_editing_back_to_the_original_value_unstages_the_row() -> None:
    changes = _changeset()
    original = {"Code": "DE", "Name": "Germany"}

    changes.stage_cell(KEY, "Name", "Deutschland", original)
    change = changes.stage_cell(KEY, "Name", "Germany", original)

    assert change is None
    assert changes.is_empty is True


def test_staging_a_null_is_a_real_change() -> None:
    changes = _changeset()
    original = {"Code": "DE", "Note": "old"}

    staged = changes.stage_cell(KEY, "Note", None, original)

    assert staged is not None
    assert staged.change.after is not None
    assert staged.change.after["Note"] is None
    assert changes.counts[ChangeKind.UPDATE] == 1


def test_a_new_row_gets_a_synthetic_key_that_never_collides() -> None:
    changes = _changeset()

    first = changes.stage_insert({"Code": "AT", "Name": "Austria"})
    second = changes.stage_insert({"Code": "BE", "Name": "Belgium"})

    assert is_new_row(first) is True
    assert is_new_row(second) is True
    assert first != second
    assert changes.counts[ChangeKind.INSERT] == 2
    assert changes.find(first) is not None
    # A real row key can never be mistaken for a staged one.
    assert is_new_row(KEY) is False


def test_editing_a_staged_insert_updates_the_insert_not_an_update() -> None:
    changes = _changeset()
    key = changes.stage_insert({"Code": "AT", "Name": "Austria"})

    staged = changes.stage_cell(key, "Name", "Österreich", {})

    assert staged is not None
    assert staged.kind is ChangeKind.INSERT
    assert staged.change.after is not None
    assert staged.change.after["Name"] == "Österreich"
    # An INSERT must never carry a row key: the server assigns the real one on Apply.
    assert staged.change.key is None
    assert len(changes) == 1


def test_deleting_a_row_with_staged_edits_drops_the_update_and_keeps_the_originals() -> None:
    changes = _changeset()
    original = _germany()
    changes.stage_cell(KEY, "Name", "Deutschland", original)

    changes.stage_delete(KEY, original)

    assert len(changes) == 1
    staged = changes.find(KEY)
    assert staged is not None
    assert staged.kind is ChangeKind.DELETE
    # The WHERE clause still needs the fetched originals (rowversion guard, compare-all).
    assert staged.change.before == original
    assert staged.change.after is None


def test_deleting_a_staged_insert_removes_it_entirely() -> None:
    changes = _changeset()
    key = changes.stage_insert({"Code": "AT", "Name": "Austria"})

    changes.stage_delete(key, {})

    assert changes.is_empty is True


def test_editing_a_deleted_row_undeletes_it_with_the_new_value() -> None:
    changes = _changeset()
    original = {"Code": "DE", "Name": "Germany"}
    changes.stage_delete(KEY, original)

    staged = changes.stage_cell(KEY, "Name", "Deutschland", original)

    assert staged is not None
    assert staged.kind is ChangeKind.UPDATE
    assert staged.change.after is not None
    assert staged.change.after["Name"] == "Deutschland"
    assert changes.counts[ChangeKind.DELETE] == 0


def test_revert_drops_one_rows_change_and_leaves_the_others() -> None:
    changes = _changeset()
    other = (("Code", "FR"),)
    changes.stage_cell(KEY, "Name", "Deutschland", {"Code": "DE", "Name": "Germany"})
    changes.stage_cell(other, "Name", "Frankreich", {"Code": "FR", "Name": "France"})

    assert changes.revert(KEY) is True

    assert changes.find(KEY) is None
    assert len(changes) == 1
    assert changes.revert(KEY) is False  # nothing left to revert


def test_clear_discards_everything_but_stays_undoable() -> None:
    changes = _changeset()
    changes.stage_cell(KEY, "Name", "Deutschland", {"Code": "DE", "Name": "Germany"})

    changes.clear()

    assert changes.is_empty is True
    assert changes.can_undo is True
    assert changes.undo() is True
    assert changes.counts[ChangeKind.UPDATE] == 1


def test_clear_on_an_empty_changeset_records_nothing() -> None:
    changes = _changeset()

    changes.clear()

    assert changes.can_undo is False


def test_undo_and_redo_walk_the_whole_history() -> None:
    changes = _changeset()
    original = _germany()
    changes.stage_cell(KEY, "Name", "Deutschland", original)
    changes.stage_cell(KEY, "Population", 84_000_000, original)

    assert changes.undo() is True
    undone = changes.find(KEY)
    assert undone is not None and undone.change.after is not None
    # Only the first edit remains after one undo.
    assert undone.change.after["Population"] == 83_000_000
    assert set(undone.change.changed_columns) == {"Name"}
    assert changes.can_redo is True

    assert changes.undo() is True
    assert changes.is_empty is True
    assert changes.undo() is False  # the history is exhausted

    assert changes.redo() is True
    assert changes.counts[ChangeKind.UPDATE] == 1
    assert changes.redo() is True
    assert changes.can_redo is False
    assert changes.redo() is False


def test_a_new_edit_discards_the_redo_history() -> None:
    changes = _changeset()
    original = _germany()
    changes.stage_cell(KEY, "Name", "Deutschland", original)
    changes.undo()

    assert changes.can_redo is True
    changes.stage_cell(KEY, "Name", "BRD", original)

    assert changes.can_redo is False


def test_a_no_op_edit_does_not_pollute_the_undo_history() -> None:
    changes = _changeset()
    original = _germany()
    first = changes.stage_cell(KEY, "Name", "Deutschland", original)
    assert first is not None

    # Staging the identical value again is not a user action worth undoing.
    again = changes.stage_cell(KEY, "Name", "Deutschland", original)

    assert again is not None
    assert changes.undo() is True
    assert changes.is_empty is True


def test_values_for_overlays_staged_over_original() -> None:
    changes = _changeset()
    original = _germany()
    changes.stage_cell(KEY, "Name", "Deutschland", original)

    assert changes.values_for(KEY, original) == {
        "Code": "DE",
        "Name": "Deutschland",
        "Population": 83_000_000,
    }
    untouched = (("Code", "FR"),)
    assert changes.values_for(untouched, {"Code": "FR", "Name": "France"}) == {
        "Code": "FR",
        "Name": "France",
    }


def test_describe_changes_wording_is_singular_and_plural_aware() -> None:
    assert describe_changes({}) == "no changes"
    assert describe_changes({ChangeKind.INSERT: 1}) == "1 insert"
    assert describe_changes({ChangeKind.INSERT: 2, ChangeKind.UPDATE: 1}) == "2 inserts, 1 update"
    assert describe_changes({ChangeKind.DELETE: 3}) == "3 deletes"


def test_row_label_names_the_key_for_error_messages() -> None:
    assert row_label(None) == "(new row)"
    assert row_label((("Code", "DE"),)) == "Code='DE'"
    assert row_label((("A", 1), ("B", 2))) == "A=1, B=2"


def test_an_undo_snapshot_is_copied_not_shared() -> None:
    changes = _changeset()
    original = _germany()
    changes.stage_cell(KEY, "Name", "Deutschland", original)
    changes.stage_cell(KEY, "Population", 84_000_000, original)

    assert changes.undo() is True  # back to the state holding only the Name edit
    undone = changes.find(KEY)
    assert undone is not None and undone.change.after is not None
    assert undone.change.after["Population"] == 83_000_000

    # An in-place edit of the live staged values must not rewrite the undo history.
    changes.rows[0].change.after["Population"] = 999  # type: ignore[index]

    assert changes.undo() is True
    assert changes.is_empty is True
    assert changes.redo() is True
    restored = changes.find(KEY)
    assert restored is not None and restored.change.after is not None
    # The redo returns the state the user actually had, with both edits, unmodified by
    # the in-place mutation above (which only touched the live list).
    assert restored.change.after["Population"] == 999
