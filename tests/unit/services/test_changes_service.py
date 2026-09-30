"""ChangeService: staging rules, status-bar counts, and Apply (FR-7).

Runs against ``FakeProvider``, so the *semantics* are proven without a database (NFR-5):
which edits may be staged at all, what the status bar says, and — most importantly — that
Apply runs in one transaction, rolls back completely on failure, keeps the user's staged
work when it does, and reports a concurrency conflict per row instead of aborting.
"""

from pathlib import Path

import pytest
from tests.conftest import COUNTRY_COLUMNS
from tests.fakes import FakeProvider

from sql_table_swiss_knife.domain import (
    AuthMode,
    ChangeKind,
    Column,
    ConnectionProfile,
    PrimaryKey,
    RowKey,
    Table,
    TableKind,
)
from sql_table_swiss_knife.services import (
    ApplyError,
    CellStatus,
    ChangeService,
    ConnectionService,
    map_database_error,
)
from sql_table_swiss_knife.storage import EphemeralSecretStore, ProfileStore

COUNTRY = Table(
    schema="dbo",
    name="Country",
    kind=TableKind.BASE_TABLE,
    columns=COUNTRY_COLUMNS,
    primary_key=PrimaryKey("PK_Country", ("Code",)),
)

#: A table whose key column is an identity — the IDENTITY_INSERT case.
IDENTITY_TABLE = Table(
    schema="dbo",
    name="Region",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("RegionId", 1, "int", None, 10, 0, False, None, True, is_primary_key=True),
        Column("Name", 2, "nvarchar", 100, None, None, False, None, False),
    ),
    primary_key=PrimaryKey("PK_Region", ("RegionId",)),
)

#: No primary key at all -> rows are read-only (S-4).
KEYLESS = Table(
    schema="dbo",
    name="AuditLog",
    kind=TableKind.BASE_TABLE,
    columns=(Column("Event", 1, "nvarchar", 100, None, None, True, None, False),),
)

COUNTRY_KEY: RowKey = (("Code", "DE"),)
GERMANY: dict[str, object] = {
    "Code": "DE",
    "Name": "Germany",
    "Population": 83_000_000,
    "NameUpper": "GERMANY",
    "RowVer": b"\x01",
}


def _profile() -> ConnectionProfile:
    return ConnectionProfile(
        name="catalog",
        provider="fake",
        host="localhost",
        port=1433,
        database="test",
        username="sa",
        auth=AuthMode.SQL,
    )


async def _service(
    tmp_path: Path,
    table: Table = COUNTRY,
    *,
    rows: dict[str, list[dict[str, object]]] | None = None,
    fail_on: int | None = None,
    conflict_on: int | None = None,
    identity_insert: bool = False,
    compare_original_values: bool = False,
) -> tuple[ChangeService, FakeProvider]:
    """A connected ChangeService over ``table``, with its fake provider for assertions."""
    provider = FakeProvider(
        [table],
        rows if rows is not None else {"Country": [dict(GERMANY)]},
        fail_on=fail_on,
        conflict_on=conflict_on,
    )
    connection = ConnectionService(
        profiles=ProfileStore(tmp_path / "profiles.toml"),
        secrets=EphemeralSecretStore(),
        provider_factory=lambda name: provider,
    )
    await connection.connect(_profile())
    changes = ChangeService(
        connection,
        table,
        identity_insert=identity_insert,
        compare_original_values=compare_original_values,
    )
    return changes, provider


# -- staging rules -------------------------------------------------------------


async def test_a_valid_edit_is_staged_and_counted(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)

    edit = service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)

    assert edit.ok is True
    assert service.is_empty is False
    assert service.summary == "1 update"
    assert service.counts[ChangeKind.UPDATE] == 1


async def test_an_invalid_value_is_refused_before_staging(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)

    edit = service.edit_cell(COUNTRY_KEY, "Population", "not a number", GERMANY)

    assert edit.ok is False
    assert edit.hints, "a refusal must explain itself"
    assert service.is_empty is True


async def test_a_not_null_column_refuses_an_empty_value(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)

    edit = service.edit_cell(COUNTRY_KEY, "Name", "", GERMANY)

    assert edit.ok is False
    assert "NOT NULL" in edit.message
    assert service.is_empty is True


async def test_a_server_managed_column_is_never_staged(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)

    for column in ("NameUpper", "RowVer"):
        edit = service.edit_cell(COUNTRY_KEY, column, "whatever", GERMANY)
        assert edit.ok is False, column
        assert "read-only" in edit.message

    assert service.is_empty is True


async def test_an_identity_column_needs_the_explicit_opt_in(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path, IDENTITY_TABLE, rows={"Region": []})
    new_key = service.insert_row({"Name": "Hesse"})

    refused = service.fill_new_row(new_key, "RegionId", 7)
    allowed = service.fill_new_row(new_key, "Name", "Hessen")

    assert refused.ok is False
    assert allowed.ok is True
    assert allowed.change is not None
    assert "RegionId" not in (allowed.change.change.after or {})


async def test_identity_insert_mode_allows_an_explicit_identity_value(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path, IDENTITY_TABLE, rows={"Region": []}, identity_insert=True)
    new_key = service.insert_row({"Name": "Hesse"})

    edit = service.fill_new_row(new_key, "RegionId", 7)

    assert edit.ok is True
    assert service.requires_identity_insert() is True
    assert edit.change is not None and edit.change.change.after is not None
    assert edit.change.change.after["RegionId"] == 7


async def test_staging_counts_cover_all_three_kinds(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)
    service.delete_row((("Code", "FR"),), {"Code": "FR", "Name": "France"})
    service.insert_row({"Code": "AT", "Name": "Austria"})

    assert service.summary == "1 insert, 1 update, 1 delete"


async def test_status_reflects_each_cell_state(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    other: RowKey = (("Code", "FR"),)

    assert service.status_for(COUNTRY_KEY, "Name") is CellStatus.UNCHANGED

    service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)
    assert service.status_for(COUNTRY_KEY, "Name") is CellStatus.MODIFIED
    assert service.status_for(COUNTRY_KEY, "Population") is CellStatus.UNCHANGED

    service.delete_row(other, {"Code": "FR", "Name": "France"})
    assert service.status_for(other, "Name") is CellStatus.DELETED

    new_key = service.insert_row({"Code": "AT"})
    assert service.status_for(new_key, "Name") is CellStatus.NEW

    assert service.status_for(COUNTRY_KEY, "RowVer") is CellStatus.READ_ONLY


async def test_duplicate_drops_the_key_and_server_managed_columns(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)

    key = service.duplicate_row(COUNTRY_KEY, GERMANY)
    staged = service.pending_for(key)

    assert staged is not None
    values = staged.change.after or {}
    assert "Code" not in values  # the copy needs its own key
    assert "NameUpper" not in values
    assert "RowVer" not in values
    assert values["Name"] == "Germany"


async def test_undo_and_redo_are_exposed_through_the_service(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)

    assert service.can_undo is True
    assert service.undo() is True
    assert service.is_empty is True
    assert service.can_redo is True
    assert service.redo() is True
    assert service.summary == "1 update"


async def test_revert_all_clears_the_staging_area(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)
    service.insert_row({"Code": "AT"})

    service.revert_all()

    assert service.is_empty is True
    assert service.summary == "no changes"


# -- SQL preview (FR-5.1, FR-5.4) ---------------------------------------------


async def test_the_preview_is_the_sql_apply_will_run(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    service.edit_cell(COUNTRY_KEY, "Name", "O'Brien", GERMANY)

    statements = service.statements()

    assert len(statements) == 1
    assert service.preview_script().startswith("BEGIN TRANSACTION;")
    assert service.preview_script().rstrip().endswith("COMMIT TRANSACTION;")


async def test_the_preview_quotes_a_value_exactly_as_it_is_sent(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    service.edit_cell(COUNTRY_KEY, "Name", "O'Brien", GERMANY)

    statement = service.statements()[0]

    assert "'@p0'" not in statement.sql_literal
    assert "N'O''Brien'" in statement.sql_literal
    # The first parameter is the SET value; the rest are the WHERE guards (key +
    # rowversion). Both renderings come from the same builder, so they agree (FR-5.4).
    assert statement.param_values[0] == "O'Brien"
    assert statement.param_values[1:] == ("DE", GERMANY["RowVer"])
    row_version = GERMANY["RowVer"]
    assert isinstance(row_version, bytes)
    assert statement.sql_literal == (
        "UPDATE [dbo].[Country] SET [Name] = N'O''Brien' "
        f"WHERE [Code] = N'DE' AND [RowVer] = 0x{row_version.hex()}"
    )


async def test_the_preview_never_mentions_a_server_managed_column(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    service.insert_row({"Code": "AT", "Name": "Austria"})

    sql = service.preview_script()

    assert "[NameUpper]" not in sql
    assert "[RowVer]" not in sql


# -- apply: commit -------------------------------------------------------------


async def test_apply_commits_the_staged_changes_and_clears_staging(tmp_path: Path) -> None:
    service, provider = await _service(tmp_path)
    service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)

    result = await service.apply()

    assert result.committed is True
    assert provider.transaction_log == ["BEGIN", "COMMIT"]
    # The staging area is cleared so a second ctrl+s cannot apply the same edit twice.
    assert service.is_empty is True
    assert provider._rows["dbo.Country"][0]["Name"] == "Deutschland"


async def test_apply_sends_delete_update_insert_in_that_order(tmp_path: Path) -> None:
    service, provider = await _service(
        tmp_path,
        rows={"Country": [dict(GERMANY), {"Code": "FR", "Name": "France"}]},
    )
    service.insert_row({"Code": "AT", "Name": "Austria"})
    service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)
    service.delete_row((("Code", "FR"),), {"Code": "FR", "Name": "France"})

    await service.apply()

    assert [sql.split()[0] for sql in provider.executed] == ["DELETE", "UPDATE", "INSERT"]


async def test_apply_with_nothing_staged_is_refused(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)

    with pytest.raises(ValueError, match="nothing staged"):
        await service.apply()


async def test_apply_refuses_a_table_whose_rows_are_read_only(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path, KEYLESS, rows={"AuditLog": [{"Event": "x"}]})
    service.changes.stage_insert({"Event": "y"})  # bypass the UI guard to test Apply itself

    with pytest.raises(ApplyError, match="read-only"):
        await service.apply()


# -- apply: rollback -----------------------------------------------------------


async def test_a_failed_apply_rolls_back_and_keeps_the_staged_work(tmp_path: Path) -> None:
    service, provider = await _service(tmp_path, fail_on=0)
    service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)

    with pytest.raises(ApplyError) as caught:
        await service.apply()

    assert caught.value.result.committed is False
    assert provider.transaction_log == ["BEGIN", "ROLLBACK"]
    # Nothing was written...
    assert provider._rows["dbo.Country"][0]["Name"] == "Germany"
    # ...and the user's work is still staged so they can fix it and retry.
    assert service.summary == "1 update"


async def test_a_failure_rolls_back_the_earlier_statements_too(tmp_path: Path) -> None:
    """A delete that succeeds must not survive when a later statement fails."""
    service, provider = await _service(
        tmp_path,
        rows={"Country": [dict(GERMANY), {"Code": "FR", "Name": "France"}]},
        fail_on=1,  # the UPDATE fails, after the DELETE already ran
    )
    service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)
    service.delete_row((("Code", "FR"),), {"Code": "FR", "Name": "France"})

    with pytest.raises(ApplyError):
        await service.apply()

    assert provider.transaction_log == ["BEGIN", "ROLLBACK"]
    codes = [row["Code"] for row in provider._rows["dbo.Country"]]
    assert codes == ["DE", "FR"]  # the delete was rolled back too


async def test_a_database_error_is_mapped_to_the_offending_row_and_column(
    tmp_path: Path,
) -> None:
    service, _ = await _service(
        tmp_path,
        fail_on=0,
        rows={"Country": [dict(GERMANY)]},
    )
    service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)

    with pytest.raises(ApplyError) as caught:
        await service.apply()

    mapped = caught.value.mapped
    assert mapped is not None
    # The row comes from the failing change; the column from what it writes.
    assert mapped.row_key == COUNTRY_KEY
    assert mapped.column == "Name"
    assert "Code='DE'" in mapped.describe()


# -- apply: optimistic concurrency (FR-7.8) ------------------------------------


async def test_a_concurrency_conflict_is_reported_per_row_without_aborting(
    tmp_path: Path,
) -> None:
    service, provider = await _service(tmp_path, conflict_on=0)
    service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)

    with pytest.raises(ApplyError) as caught:
        await service.apply()

    error = caught.value
    assert error.result.committed is False
    assert provider.transaction_log == ["BEGIN", "ROLLBACK"]
    conflicts = error.conflicts
    assert len(conflicts) == 1
    assert conflicts[0].row_key == COUNTRY_KEY
    assert conflicts[0].is_update is True
    assert "0 rows affected" in conflicts[0].reason
    # The message names the row and tells the user what to do about it.
    assert "Code='DE'" in service.conflict_message(conflicts[0])
    assert "refresh" in service.conflict_message(conflicts[0])
    # The work is kept so the user can re-stage it after refreshing.
    assert service.summary == "1 update"


async def test_a_conflict_on_a_delete_is_reported_as_a_delete(tmp_path: Path) -> None:
    service, _ = await _service(
        tmp_path,
        rows={"Country": [dict(GERMANY), {"Code": "FR", "Name": "France"}]},
        conflict_on=0,
    )
    service.delete_row((("Code", "FR"),), {"Code": "FR", "Name": "France"})

    with pytest.raises(ApplyError) as caught:
        await service.apply()

    conflict = caught.value.conflicts[0]
    assert conflict.is_update is False
    assert "delete" in service.conflict_message(conflict)


async def test_a_row_deleted_by_somebody_else_is_a_conflict_not_a_silent_success(
    tmp_path: Path,
) -> None:
    """The in-memory row is gone: the UPDATE matches 0 rows, so it must be reported."""
    service, _ = await _service(tmp_path, rows={"Country": []})
    service.changes.stage_delete(COUNTRY_KEY, dict(GERMANY))

    with pytest.raises(ApplyError) as caught:
        await service.apply()

    assert caught.value.conflicts, "a 0-rowcount DELETE is a conflict"
    assert caught.value.result.committed is False


async def test_a_conflict_does_not_leave_other_staged_changes_applied(tmp_path: Path) -> None:
    service, provider = await _service(
        tmp_path,
        rows={"Country": [dict(GERMANY), {"Code": "FR", "Name": "France"}]},
        conflict_on=1,  # the UPDATE conflicts, after the DELETE already ran
    )
    service.edit_cell(COUNTRY_KEY, "Name", "Deutschland", GERMANY)
    service.delete_row((("Code", "FR"),), {"Code": "FR", "Name": "France"})

    with pytest.raises(ApplyError):
        await service.apply()

    codes = [row["Code"] for row in provider._rows["dbo.Country"]]
    assert codes == ["DE", "FR"]  # the delete rolled back with everything else
    assert service.summary == "1 update, 1 delete"  # both still staged


# -- error mapping -------------------------------------------------------------


def test_a_foreign_key_error_names_the_constraint_and_the_row() -> None:
    change = None
    mapped = map_database_error(
        "The INSERT statement conflicted with the FOREIGN KEY constraint 'FK_Region_Country'.",
        COUNTRY,
        change,
    )
    assert mapped.constraint == "FK_Region_Country"
    assert mapped.kind == "fk"
    assert "referenced row is missing" in mapped.message


def test_an_error_without_a_change_still_maps() -> None:
    mapped = map_database_error("something went wrong", COUNTRY, None)

    assert mapped.row_key is None
    assert mapped.column is None
    assert mapped.constraint is None
    assert mapped.describe() == "something went wrong"


@pytest.mark.parametrize(
    ("message", "expected_column"),
    [
        ("Cannot insert the value NULL into column 'Name'", "Name"),
        ("String or binary data would be truncated in table 'dbo.Country', column 'Name'.", "Name"),
    ],
)
def test_the_column_named_in_the_message_wins(message: str, expected_column: str) -> None:
    assert map_database_error(message, COUNTRY, None).column == expected_column


def test_a_delete_error_has_no_guessed_column() -> None:
    from sql_table_swiss_knife.domain import PendingChange, TableRef

    delete = PendingChange(
        kind=ChangeKind.DELETE,
        table=TableRef(schema="dbo", name="Country"),
        key=(("Code", "DE"),),
        before={"Code": "DE"},
    )
    mapped = map_database_error("could not delete the row", COUNTRY, delete)

    assert mapped.column is None
    assert mapped.row_key == (("Code", "DE"),)
