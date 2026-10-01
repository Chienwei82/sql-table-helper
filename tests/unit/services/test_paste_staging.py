"""Staging a confirmed paste plan (FR-4.6) — the seam between "planned" and "staged".

The promise this milestone makes is narrow and testable: a paste reaches the staging area
only through :meth:`ChangeService.stage_paste`, only as a plan the user has confirmed, and
never half-parsed. These tests pin that promise against ``FakeProvider`` — no database, and
asserting on the change set Apply would be given.
"""

from pathlib import Path

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
from sql_table_swiss_knife.services import ChangeService, ConnectionService
from sql_table_swiss_knife.services.clipboard import (
    PasteMode,
    PastePlan,
    PasteTarget,
    parse_block,
    plan_paste,
)
from sql_table_swiss_knife.storage import EphemeralSecretStore, ProfileStore

COUNTRY = Table(
    schema="dbo",
    name="Country",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Code", 1, "char", 2, None, None, False, None, False, is_primary_key=True),
        Column("Name", 2, "nvarchar", 200, None, None, False, None, False),
        Column("Population", 3, "int", None, 10, 0, True, None, False),
    ),
    primary_key=PrimaryKey("PK_Country", ("Code",)),
)

EDITABLE = COUNTRY.columns
GERMANY: dict[str, object] = {"Code": "DE", "Name": "Germany", "Population": 83_000_000}
FRANCE: dict[str, object] = {"Code": "FR", "Name": "France", "Population": 67_000_000}
DE_KEY: RowKey = (("Code", "DE"),)
NAMES = tuple(column.name for column in COUNTRY.columns)


async def _service(tmp_path: Path, rows: list[dict[str, object]] | None = None) -> ChangeService:
    provider = FakeProvider([COUNTRY], {"Country": rows or [dict(GERMANY), dict(FRANCE)]})
    connection = ConnectionService(
        profiles=ProfileStore(tmp_path / "profiles.toml"),
        secrets=EphemeralSecretStore(),
        provider_factory=lambda name: provider,
    )
    await connection.connect(
        ConnectionProfile(
            name="catalog",
            provider="fake",
            host="localhost",
            port=1433,
            database="test",
            username="sa",
            auth=AuthMode.SQL,
        )
    )
    return ChangeService(connection, COUNTRY)


def _target(
    rows: tuple[dict[str, object], ...] = (GERMANY, FRANCE),
    *,
    anchor_row: int = 0,
    anchor_column: int = 0,
) -> PasteTarget:
    """The grid as the clipboard service sees it: rows, their keys and the cursor."""
    keys: tuple[RowKey | None, ...] = tuple((("Code", row["Code"]),) for row in rows)
    return PasteTarget(
        table=COUNTRY,
        columns=EDITABLE,
        rows=rows,
        keys=keys,
        anchor_row=anchor_row,
        anchor_column=anchor_column,
    )


async def test_a_confirmed_plan_is_staged_as_updates_and_inserts(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    rows = (dict(GERMANY), dict(FRANCE))
    block = parse_block("Code\tName\nDE\tDeutschland\nES\tSpain", known_columns=NAMES)
    plan = plan_paste(_target(rows), block)

    edit = service.stage_paste(plan, rows)

    assert edit.ok
    assert service.counts[ChangeKind.UPDATE] == 1
    assert service.counts[ChangeKind.INSERT] == 1
    staged = {staged.key: staged.change for staged in tuple(service.changes)}
    assert staged[DE_KEY].after is not None
    assert dict(staged[DE_KEY].after or {})["Name"] == "Deutschland"


async def test_a_plan_with_a_failing_cell_stages_nothing(tmp_path: Path) -> None:
    """FR-4.6: no half-parsed paste — one bad cell blocks the whole thing."""
    service = await _service(tmp_path)
    rows = (dict(GERMANY),)
    block = parse_block("Name\tPopulation\nDeutschland\tlots", known_columns=NAMES)
    plan = plan_paste(_target(rows), block)
    assert not plan.ok

    edit = service.stage_paste(plan, rows)

    assert not edit.ok
    assert "whole number" in edit.message
    assert service.is_empty


async def test_a_cell_paste_updates_one_existing_row(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    rows = (dict(GERMANY), dict(FRANCE))
    plan = plan_paste(_target(rows, anchor_row=0, anchor_column=1), parse_block("Deutschland"))

    assert service.stage_paste(plan, rows).ok

    assert service.counts[ChangeKind.UPDATE] == 1
    staged = service.changes.find(DE_KEY)
    assert staged is not None and staged.change.after is not None
    assert staged.change.after["Name"] == "Deutschland"
    assert staged.change.after["Population"] == 83_000_000  # untouched columns survive


async def test_a_paste_never_reaches_the_database_by_itself(tmp_path: Path) -> None:
    """S-1: staging is in memory; only Apply writes, and nothing here did."""
    service = await _service(tmp_path)
    rows = (dict(GERMANY), dict(FRANCE))
    plan = plan_paste(_target(rows), parse_block("Code\tName\nDE\tDeutschland"))
    service.stage_paste(plan, rows)

    provider = service._connection.provider()
    assert getattr(provider, "executed", []) == []


async def test_a_fill_onto_a_keyless_row_is_skipped_with_a_warning(tmp_path: Path) -> None:
    """S-4: a row with no identity cannot be named in a WHERE clause, so it is not staged."""
    service = await _service(tmp_path)
    rows = (dict(GERMANY),)
    target = PasteTarget(
        table=COUNTRY,
        columns=EDITABLE,
        rows=rows,
        keys=(None,),  # a row the provider could not key
        anchor_row=0,
        anchor_column=1,
    )
    plan = plan_paste(target, parse_block("Deutschland"), mode=PasteMode.FILL)

    edit = service.stage_paste(plan, rows)

    assert edit.ok
    assert service.is_empty
    assert any("no usable key" in hint.text for hint in edit.hints)


async def test_an_empty_plan_stages_nothing_and_says_so(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    rows = (dict(GERMANY),)

    edit = service.stage_paste(PastePlan(PasteMode.ROWS, ()), rows)

    assert edit.ok
    assert service.is_empty
    assert not edit.hints  # nothing happened, so nothing is claimed


async def test_one_paste_is_one_undo_step(tmp_path: Path) -> None:
    """A paste is one user action, so ctrl+z must take all of it back at once.

    Regression: ``stage_paste`` committed per cell, so a paste left one undo entry per
    cell and the user pressed ctrl+z once per pasted cell. This drives the real entry
    point, not ``ChangeSet.batch()`` directly, so unwiring the batch would fail here.
    """
    service = await _service(tmp_path)
    rows = (dict(GERMANY), dict(FRANCE))
    block = parse_block(
        "Code\tName\tPopulation\nDE\tDeutschland\t1\nFR\tFrance\t2", known_columns=NAMES
    )
    plan = plan_paste(_target(rows), block, mode=PasteMode.ROWS)

    assert service.stage_paste(plan, rows).ok
    assert service.counts[ChangeKind.UPDATE] == 2

    assert service.undo() is True
    assert service.is_empty is True
    assert service.undo() is False  # nothing left: the whole paste went at once
