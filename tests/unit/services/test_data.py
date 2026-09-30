"""DataService: page size, deterministic order, fetch-more, status wording (FR-3.8, S-8).

Runs against ``FakeProvider``, so the paging *semantics* are verified without a database
(NFR-5): what the grid shows, what "more?" means and how the status bar words it.
"""

from pathlib import Path

import pytest
from tests.fakes import FakeProvider

from sql_table_swiss_knife.domain import (
    AuthMode,
    Column,
    ConnectionProfile,
    PrimaryKey,
    Row,
    SortKey,
    Table,
    TableKind,
    UniqueConstraint,
)
from sql_table_swiss_knife.services import ConnectionService, DataService, status_text
from sql_table_swiss_knife.services.data import RowWindow
from sql_table_swiss_knife.storage import EphemeralSecretStore, ProfileStore

COUNTRY = Table(
    schema="dbo",
    name="Country",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Code", 1, "char", 2, None, None, False, None, False, is_primary_key=True),
        Column("Name", 2, "nvarchar", 200, None, None, False, None, False),
    ),
    primary_key=PrimaryKey("PK_Country", ("Code",)),
)

#: No key at all -> read-only rows (S-4); still ordered deterministically by column 1.
KEYLESS = Table(
    schema="dbo",
    name="AuditLog",
    kind=TableKind.BASE_TABLE,
    columns=(Column("Event", 1, "nvarchar", 200, None, None, True, None, False),),
)

#: Rows the fake provider serves, keyed by bare table name (its lookup convention).
ROWS: dict[str, list[dict[str, object]]] = {
    "Country": [
        {"Code": code, "Name": name}
        for code, name in (("DE", "Germany"), ("FR", "France"), ("JP", "Japan"))
    ],
    "AuditLog": [{"Event": event} for event in ("a", "b", "c", "d")],
}


def _profile() -> ConnectionProfile:
    return ConnectionProfile(
        name="catalog",
        provider="fake",
        host="localhost",
        database="test",
        username="sa",
        auth=AuthMode.SQL,
    )


async def _connected(tmp_path: Path, tables: tuple[Table, ...]) -> DataService:
    """A data service with an open session against a fake provider serving ``ROWS``."""
    provider = FakeProvider(tables, ROWS)
    connection = ConnectionService(
        profiles=ProfileStore(tmp_path / "profiles.toml"),
        secrets=EphemeralSecretStore(),
        provider_factory=lambda name: provider,
    )
    await connection.connect(_profile())
    return DataService(connection)


async def test_fetch_returns_rows_in_identity_order(tmp_path: Path) -> None:
    """FR-3.8: with a PK the default order is PK order, so paging is deterministic."""
    data = await _connected(tmp_path, (COUNTRY,))
    window = await data.fetch(COUNTRY)

    assert [row["Code"] for row in window.rows] == ["DE", "FR", "JP"]
    assert window.sort == (SortKey("Code"),)


async def test_fetch_orders_by_the_first_column_without_a_key(tmp_path: Path) -> None:
    """No PK: a stable deterministic order is still used (the table is read-only)."""
    data = await _connected(tmp_path, (KEYLESS,))
    window = await data.fetch(KEYLESS)

    assert window.sort == (SortKey("Event"),)
    assert [row["Event"] for row in window.rows] == ["a", "b", "c", "d"]


async def test_a_single_column_unique_key_is_used_as_the_order(tmp_path: Path) -> None:
    """OQ-5: a usable UNIQUE key is the identity, so it drives the order."""
    subject = Table(
        schema="dbo",
        name="Country",
        kind=TableKind.BASE_TABLE,
        columns=COUNTRY.columns,
        unique_constraints=(UniqueConstraint("UQ_Code", ("Code",)),),
    )
    data = await _connected(tmp_path, (subject,))
    window = await data.fetch(subject)

    assert window.sort == (SortKey("Code"),)


async def test_limit_and_offset_page_through_the_rows(tmp_path: Path) -> None:
    data = await _connected(tmp_path, (COUNTRY,))
    first = await data.fetch(COUNTRY, limit=2)
    second = await data.fetch(COUNTRY, limit=2, offset=2)

    assert [row["Code"] for row in first.rows] == ["DE", "FR"]
    assert [row["Code"] for row in second.rows] == ["JP"]
    assert first.has_more is True


async def test_fetch_more_appends_the_next_page(tmp_path: Path) -> None:
    """The "fetch more" action grows the window instead of replacing it."""
    data = await _connected(tmp_path, (KEYLESS,))
    first = await data.fetch(KEYLESS, limit=2)
    grown = await data.fetch_more(first)

    assert [row["Event"] for row in grown.rows] == ["a", "b", "c", "d"]
    assert grown.count == 4
    assert grown.has_more is False


async def test_fetch_more_on_a_complete_window_is_a_no_op(tmp_path: Path) -> None:
    """Asking for more when there is none returns the same window, never an error."""
    data = await _connected(tmp_path, (KEYLESS,))
    complete = await data.fetch(KEYLESS, limit=10)

    assert await data.fetch_more(complete) is complete


async def test_fetch_requires_a_connection(tmp_path: Path) -> None:
    """Calling the data service while disconnected is a programming error (FR-10)."""
    data = DataService(
        ConnectionService(
            profiles=ProfileStore(tmp_path / "profiles.toml"),
            secrets=EphemeralSecretStore(),
            provider_factory=lambda name: FakeProvider(),
        )
    )
    with pytest.raises(RuntimeError, match="not connected"):
        await data.fetch(COUNTRY)


def test_status_text_reports_the_span_and_whether_more_exist() -> None:
    """FR-3.8/S-8: the status bar says which rows are loaded and whether more exist."""
    empty = RowWindow(COUNTRY, (), 1000, has_more=False)
    assert status_text(empty) == "no rows"

    loaded = (Row({"Code": "DE"}), Row({"Code": "FR"}))
    assert status_text(RowWindow(COUNTRY, loaded, 1000, has_more=True)) == "rows 1-2 • more?"
    assert (
        status_text(RowWindow(COUNTRY, loaded, 1000, has_more=False, total_row_count=2))
        == "rows 1-2 of 2"
    )


def test_status_text_uses_the_same_wording_for_a_full_single_page() -> None:
    """One complete page says "of N" so the user knows the table was fully read."""
    window = RowWindow(COUNTRY, (Row({"Code": "DE"}),), 1, has_more=False, total_row_count=1)
    assert status_text(window) == "rows 1-1 of 1"
