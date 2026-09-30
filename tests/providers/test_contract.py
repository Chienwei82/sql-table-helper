"""Provider contract tests — DBMS-independent semantics (DESIGN §11).

Runs against the in-memory FakeProvider always; the same expectations apply to the
SQL Server provider once it lands in M2 (env-gated live tests extend this suite).
"""

from typing import cast

import pytest

from sql_table_swiss_knife.domain import (
    AuthMode,
    ChangeKind,
    ConnectionProfile,
    FetchSpec,
    FilterOp,
    PendingChange,
    RowFilter,
    SortKey,
    Table,
)
from sql_table_swiss_knife.providers import (
    DatabaseProvider,
    ExecuteResult,
    MetadataError,
    ProviderCapabilities,
    SqlDialect,
)
from tests.fakes import FakeProvider


def _profile() -> ConnectionProfile:
    return ConnectionProfile(
        name="test",
        provider="fake",
        host="localhost",
        database="test",
        username="sa",
        auth=AuthMode.SQL,
    )


def test_fake_satisfies_protocol() -> None:
    provider: DatabaseProvider = FakeProvider()
    assert isinstance(provider, DatabaseProvider)
    assert isinstance(provider.dialect, object)
    assert isinstance(provider.capabilities, ProviderCapabilities)
    assert isinstance(provider.dialect, SqlDialect)


async def test_connect_disconnect_and_listing(
    country_table: Table, country_rows: list[dict[str, object]]
) -> None:
    provider = FakeProvider([country_table], {"Country": country_rows})
    conn = await provider.connect(_profile())
    assert conn.provider_name == "fake"
    assert conn.database == "test"

    databases = await provider.list_databases(conn)
    assert [db.name for db in databases] == ["test"]

    summaries = await provider.list_tables(conn, schema="dbo")
    assert summaries[0].name == "Country"
    assert summaries[0].has_primary_key

    metadata = await provider.get_table_metadata(conn, "dbo", "Country")
    assert metadata == country_table
    assert provider.quote_identifier("Code") == "[Code]"

    await provider.disconnect(conn)


async def test_fetch_rows_is_paged_sorted_and_filtered(
    country_table: Table, country_rows: list[dict[str, object]]
) -> None:
    provider = FakeProvider([country_table], {"Country": country_rows})
    conn = await provider.connect(_profile())

    page = await provider.fetch_rows(conn, country_table, FetchSpec(limit=2))
    assert page.count == 2
    assert page.has_more
    assert page.total_row_count == 5
    assert page.offset == 0

    page2 = await provider.fetch_rows(conn, country_table, FetchSpec(limit=2, offset=4))
    assert page2.count == 1
    assert not page2.has_more

    sorted_page = await provider.fetch_rows(
        conn,
        country_table,
        FetchSpec(limit=5, sort=(SortKey("Population", descending=True),)),
    )
    populations = cast("list[int]", [row["Population"] for row in sorted_page.rows])
    assert populations == sorted(populations, reverse=True)

    filtered = await provider.fetch_rows(
        conn,
        country_table,
        FetchSpec(limit=10, filters=(RowFilter("Code", FilterOp.EQ, "DE"),)),
    )
    assert filtered.count == 1
    assert filtered.rows[0]["Code"] == "DE"

    liked = await provider.fetch_rows(
        conn,
        country_table,
        FetchSpec(limit=10, filters=(RowFilter("Name", FilterOp.LIKE, "F%"),)),
    )
    assert [row["Name"] for row in liked.rows] == ["France"]


async def test_get_metadata_unknown_table_raises(country_table: Table) -> None:
    provider = FakeProvider([country_table])
    conn = await provider.connect(_profile())
    with pytest.raises(MetadataError, match="unknown table"):
        await provider.get_table_metadata(conn, "dbo", "Nope")


async def test_execute_changes_commits_in_one_transaction(
    country_table: Table, country_rows: list[dict[str, object]]
) -> None:
    provider = FakeProvider([country_table], {"Country": country_rows})
    conn = await provider.connect(_profile())
    changes = [
        PendingChange(
            kind=ChangeKind.INSERT,
            table=country_table.ref,
            key=None,
            before=None,
            after={"Code": "ES", "Name": "Spain"},
        ),
        PendingChange(
            kind=ChangeKind.UPDATE,
            table=country_table.ref,
            key=(("Code", "DE"),),
            before={"Code": "DE", "Name": "Germany", "RowVer": b"\x01"},
            after={"Code": "DE", "Name": "Deutschland", "RowVer": b"\x01"},
        ),
        PendingChange(
            kind=ChangeKind.DELETE,
            table=country_table.ref,
            key=(("Code", "FR"),),
            before={"Code": "FR", "RowVer": b"\x02"},
        ),
    ]

    result: ExecuteResult = await provider.execute_changes(conn, country_table, changes)

    assert result.committed
    assert result.failed_index is None
    assert result.error is None
    assert result.duration_ms >= 0
    # Per-statement results arrive in apply order: DELETE → UPDATE → INSERT.
    assert [r.change.kind for r in result.results] == [
        ChangeKind.DELETE,
        ChangeKind.UPDATE,
        ChangeKind.INSERT,
    ]
    assert all(r.success and r.rowcount == 1 for r in result.results)
    assert provider.transaction_log == ["BEGIN", "COMMIT"]

    page = await provider.fetch_rows(conn, country_table, FetchSpec(limit=100))
    codes = {row["Code"] for row in page.rows}
    assert codes == {"DE", "JP", "US", "CH", "ES"}
    names = {row["Code"]: row["Name"] for row in page.rows}
    assert names["DE"] == "Deutschland"


async def test_execute_changes_failure_rolls_back_everything(
    country_table: Table, country_rows: list[dict[str, object]]
) -> None:
    provider = FakeProvider([country_table], {"Country": country_rows}, fail_on=1)
    conn = await provider.connect(_profile())
    changes = [
        PendingChange(
            kind=ChangeKind.INSERT,
            table=country_table.ref,
            key=None,
            before=None,
            after={"Code": "ES", "Name": "Spain"},
        ),
        PendingChange(
            kind=ChangeKind.UPDATE,
            table=country_table.ref,
            key=(("Code", "DE"),),
            before={"Code": "DE", "Name": "Germany", "RowVer": b"\x01"},
            after={"Code": "DE", "Name": "Deutschland", "RowVer": b"\x01"},
        ),
    ]

    result = await provider.execute_changes(conn, country_table, changes)

    assert not result.committed
    assert result.failed_index == 1
    assert result.error == "simulated failure"
    assert result.results[0].success and not result.results[1].success
    assert provider.transaction_log == ["BEGIN", "ROLLBACK"]

    page = await provider.fetch_rows(conn, country_table, FetchSpec(limit=100))
    codes = {row["Code"] for row in page.rows}
    assert "ES" not in codes  # the insert was rolled back
    names = {row["Code"]: row["Name"] for row in page.rows}
    assert names["DE"] == "Germany"  # the update was rolled back


async def test_execute_changes_detects_stale_row(
    country_table: Table, country_rows: list[dict[str, object]]
) -> None:
    provider = FakeProvider([country_table], {"Country": country_rows})
    conn = await provider.connect(_profile())
    stale = PendingChange(
        kind=ChangeKind.UPDATE,
        table=country_table.ref,
        key=(("Code", "ZZ"),),
        before={"Code": "ZZ", "Name": "Gone"},
        after={"Code": "ZZ", "Name": "Gone2"},
    )
    result = await provider.execute_changes(conn, country_table, [stale])
    assert not result.committed
    assert result.failed_index == 0
    assert result.error is not None and "optimistic concurrency" in result.error
    assert provider.transaction_log == ["BEGIN", "ROLLBACK"]


def test_execute_result_validation() -> None:
    with pytest.raises(ValueError, match="failed_index"):
        ExecuteResult(committed=True, results=(), duration_ms=0, failed_index=0)
    with pytest.raises(ValueError, match="failed_index"):
        ExecuteResult(committed=False, results=(), duration_ms=0)
    ok = ExecuteResult(committed=True, results=(), duration_ms=1)
    assert ok.statement_count == 0
