"""Live integration tests for the SQL Server provider's **write path** (``execute_changes``).

The gap these close is named in PROGRESS.md: ``tests/live`` covered metadata and reads
but never applied a change, so the parameter-binding contract was only ever exercised
against a fake driver. That is how the NULL-guard bug survived a green suite.

Isolation: every test writes through its own rows in ``dbo.Simple``/``dbo.UniqueOnly``/
etc., and the ``clean_slate`` fixture restores the fixture data afterwards, so the suite
is order-independent and re-runnable. Assertions read the database back through the
provider, never from the client's cached state.

Run with::

    cd tests/live && docker compose up -d
    uv run pytest -m live
"""

from typing import Any

import pytest

from sql_table_swiss_knife.domain import ChangeKind, FetchSpec, PendingChange
from sql_table_swiss_knife.providers import ApplyOptions, QueryError
from sql_table_swiss_knife.providers.mssql.provider import CONFLICT_REASON, MssqlProvider
from tests.live.conftest import LiveServer

pytestmark = pytest.mark.live


async def table_of(conn: Any, schema: str, name: str) -> Any:
    """Fetch live metadata for one table."""
    return await MssqlProvider().get_table_metadata(conn, schema, name)


async def rows_of(conn: Any, schema: str, name: str, limit: int = 100) -> list[dict[str, Any]]:
    """Read a table's rows back through the provider, keyed by column name."""
    provider = MssqlProvider()
    table = await provider.get_table_metadata(conn, schema, name)
    page = await provider.fetch_rows(conn, table, FetchSpec(limit=limit))
    return [dict(row.values) for row in page.rows]


def insert_of(table: Any, values: dict[str, Any]) -> PendingChange:
    """An INSERT change targeting ``table``."""
    return PendingChange(kind=ChangeKind.INSERT, table=table.ref, after=values)


def update_of(table: Any, key: Any, before: dict[str, Any], after: dict[str, Any]) -> PendingChange:
    """An UPDATE change targeting ``table``."""
    return PendingChange(
        kind=ChangeKind.UPDATE, table=table.ref, key=key, before=before, after=after
    )


def delete_of(table: Any, key: Any, before: dict[str, Any]) -> PendingChange:
    """A DELETE change targeting ``table``."""
    return PendingChange(kind=ChangeKind.DELETE, table=table.ref, key=key, before=before)


async def test_insert_commits_and_is_readable_afterwards(
    live_connection: Any, clean_slate: None
) -> None:
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")
    before = len(await rows_of(live_connection, "dbo", "Simple"))

    result = await provider.execute_changes(
        live_connection, table, [insert_of(table, {"Name": "inserted", "Qty": 1})]
    )

    assert result.committed is True
    assert result.failed_index is None
    assert result.statement_count == 1
    assert result.results[0].rowcount == 1
    assert result.duration_ms >= 0
    rows = await rows_of(live_connection, "dbo", "Simple")
    assert len(rows) == before + 1
    assert any(row["Name"] == "inserted" for row in rows)


async def test_insert_applies_the_server_default_for_omitted_columns(
    live_connection: Any, clean_slate: None
) -> None:
    """Omitting ``Active`` must let DEFAULT (1) run — the provider must not invent a value."""
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")

    await provider.execute_changes(
        live_connection, table, [insert_of(table, {"Name": "defaulted"})]
    )

    (row,) = [
        r for r in await rows_of(live_connection, "dbo", "Simple") if r["Name"] == "defaulted"
    ]
    assert row["Active"] in (1, True)
    assert row["Qty"] is None


async def test_insert_returns_generated_identity_on_a_table_with_rowversion(
    live_connection: Any, clean_slate: None
) -> None:
    """``OUTPUT inserted.*`` is only emitted for rowversion tables; it must come back.

    Without it the grid could never learn the identity of a row it just inserted.
    """
    provider = MssqlProvider()
    region = await table_of(live_connection, "dbo", "Region")
    assert region.rowversion_column is not None

    result = await provider.execute_changes(
        live_connection,
        region,
        [insert_of(region, {"CountryCode": "DE", "Name": "Inserted", "SortOrder": 99})],
    )

    assert result.committed is True
    assert result.inserted_keys
    (key,) = result.inserted_keys
    region_id = dict(key)["RegionId"]
    assert isinstance(region_id, int)
    assert region_id > 0
    assert any(row["Name"] == "Inserted" for row in await rows_of(live_connection, "dbo", "Region"))


async def test_insert_refuses_a_server_managed_column(live_connection: Any) -> None:
    """A computed column cannot be written; the builder must refuse before any SQL runs."""
    provider = MssqlProvider()
    region = await table_of(live_connection, "dbo", "Region")
    before = len(await rows_of(live_connection, "dbo", "Region"))

    with pytest.raises(ValueError, match="computed column"):
        await provider.execute_changes(
            live_connection,
            region,
            [insert_of(region, {"CountryCode": "DE", "Name": "X", "NameUpper": "NOPE"})],
        )

    assert len(await rows_of(live_connection, "dbo", "Region")) == before


async def test_identity_column_cannot_be_written_without_the_opt_in(
    live_connection: Any, clean_slate: None
) -> None:
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")
    assert table.column("Id").is_identity

    with pytest.raises(ValueError, match="identity column"):
        await provider.execute_changes(
            live_connection,
            table,
            [insert_of(table, {"Id": 999999, "Name": "explicit id"})],
        )


async def test_identity_insert_opt_in_writes_an_explicit_value(
    live_connection: Any, clean_slate: None
) -> None:
    """With the opt-in the explicit value lands *and* IDENTITY_INSERT is off afterwards."""
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")

    result = await provider.execute_changes(
        live_connection,
        table,
        [insert_of(table, {"Id": 4242, "Name": "explicit"})],
        ApplyOptions(identity_insert=True),
    )

    assert result.committed is True, result.error
    assert any(row["Id"] == 4242 for row in await rows_of(live_connection, "dbo", "Simple"))
    # Leaving IDENTITY_INSERT on would block every other session's inserts.
    rows = await live_connection.afetch(
        "SELECT OBJECTPROPERTY(OBJECT_ID('[dbo].[Simple]'), 'IsIdentityInsert') AS on_flag"
    )
    assert rows[0]["on_flag"] in (0, None, False)


async def test_update_null_preimage_guard_applies_and_commits(
    live_connection: Any, clean_slate: None
) -> None:
    """The regression this file exists for.

    ``dbo.Simple`` has no rowversion, so ``compare_original_values=True`` is the only
    guard. The row chosen has ``Qty IS NULL``; the generated WHERE must use
    ``[Qty] IS NULL``. Emitting ``[Qty] = NULL`` would match 0 rows and roll the Apply
    back as a phantom conflict — invisible until a real driver runs the statement.
    """
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")
    # The guard compares the *unchanged* columns, so the NULL has to sit in the column the
    # statement does not write (Qty), while Name is the one being changed.
    (row,) = [r for r in await rows_of(live_connection, "dbo", "Simple") if r["Qty"] is None]
    key = (("Id", row["Id"]),)
    before = dict(row)

    result = await provider.execute_changes(
        live_connection,
        table,
        [update_of(table, key, before, {**before, "Name": "renamed"})],
        ApplyOptions(compare_original_values=True),
    )

    assert result.committed is True, result.error
    (statement,) = [entry.statement for entry in result.results]
    where = statement.sql_parametrized.split("WHERE")[1]
    assert "IS NULL" in where
    assert "[Name] = @p" not in where
    (stored,) = [r for r in await rows_of(live_connection, "dbo", "Simple") if r["Id"] == row["Id"]]
    assert stored["Name"] == "renamed"


async def test_update_on_rowversion_table_uses_the_rowversion_guard(
    live_connection: Any, clean_slate: None
) -> None:
    provider = MssqlProvider()
    region = await table_of(live_connection, "dbo", "Region")
    row = (await rows_of(live_connection, "dbo", "Region"))[0]
    key = (("RegionId", row["RegionId"]),)

    result = await provider.execute_changes(
        live_connection,
        region,
        [update_of(region, key, dict(row), {**dict(row), "SortOrder": row["SortOrder"] + 1})],
    )

    assert result.committed is True, result.error
    (statement,) = [entry.statement for entry in result.results]
    assert "[RowVer]" in statement.sql_parametrized


async def test_update_detects_a_concurrent_change_as_a_conflict(
    live_connection: Any, clean_slate: None
) -> None:
    """Somebody else edits the row between fetch and Apply → 0 rows → conflict, no commit."""
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")
    (row,) = [
        r for r in await rows_of(live_connection, "dbo", "Simple") if r["Name"] == "populated"
    ]
    key = (("Id", row["Id"]),)
    stale = dict(row)

    # Simulate the other user, straight through SQL so the stale pre-image is untouched.
    await live_connection.aexecute(
        "UPDATE [dbo].[Simple] SET [Qty] = 999 WHERE [Id] = ?", (row["Id"],)
    )

    result = await provider.execute_changes(
        live_connection,
        table,
        [update_of(table, key, stale, {**stale, "Name": "mine"})],
        # dbo.Simple has no rowversion, so the optimistic guard only exists in the
        # compare-all mode; without it there is nothing to detect the conflict with.
        ApplyOptions(compare_original_values=True),
    )

    assert result.committed is False
    assert result.has_conflicts is True
    (conflict,) = result.conflicts
    assert conflict.row_key == key
    assert conflict.is_update is True
    # The wording is CONFLICT_REASON plus which row failed, as row_label renders the key.
    assert CONFLICT_REASON in (result.error or "")
    assert f"update of Id={row['Id']}" in (result.error or "")
    (stored,) = [r for r in await rows_of(live_connection, "dbo", "Simple") if r["Id"] == row["Id"]]
    assert stored["Qty"] == 999
    assert stored["Name"] == "populated"


async def test_delete_commits_and_removes_the_row(live_connection: Any, clean_slate: None) -> None:
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")
    (row,) = [
        r for r in await rows_of(live_connection, "dbo", "Simple") if r["Name"] == "populated"
    ]

    result = await provider.execute_changes(
        live_connection, table, [delete_of(table, (("Id", row["Id"]),), dict(row))]
    )

    assert result.committed is True
    assert result.results[0].rowcount == 1
    assert not any(r["Id"] == row["Id"] for r in await rows_of(live_connection, "dbo", "Simple"))


async def test_delete_of_an_already_deleted_row_is_a_conflict(
    live_connection: Any, clean_slate: None
) -> None:
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")
    (row,) = [
        r for r in await rows_of(live_connection, "dbo", "Simple") if r["Name"] == "populated"
    ]
    key = (("Id", row["Id"]),)
    await live_connection.aexecute("DELETE FROM [dbo].[Simple] WHERE [Id] = ?", (row["Id"],))

    result = await provider.execute_changes(
        live_connection, table, [delete_of(table, key, dict(row))]
    )

    assert result.committed is False
    assert result.has_conflicts is True
    assert result.conflicts[0].is_update is False


async def test_duplicate_key_rolls_the_whole_transaction_back(
    live_connection: Any, clean_slate: None
) -> None:
    """All-or-nothing (FR-7.6): the good insert before the bad one must not survive."""
    provider = MssqlProvider()
    unique = await table_of(live_connection, "dbo", "UniqueOnly")
    before = len(await rows_of(live_connection, "dbo", "UniqueOnly"))

    result = await provider.execute_changes(
        live_connection,
        unique,
        [
            insert_of(unique, {"Code": "ZZZ", "Label": "fresh"}),
            insert_of(unique, {"Code": "EUR", "Label": "duplicate"}),
        ],
    )

    assert result.committed is False
    assert result.failed_index == 1
    assert result.error
    rows = await rows_of(live_connection, "dbo", "UniqueOnly")
    assert len(rows) == before
    assert not any(row["Code"] == "ZZZ" for row in rows)


async def test_check_constraint_violation_names_the_constraint_and_rolls_back(
    live_connection: Any, clean_slate: None
) -> None:
    """``CK_Country_Code`` requires an upper-case code; the error must name the constraint."""
    provider = MssqlProvider()
    country = await table_of(live_connection, "dbo", "Country")

    result = await provider.execute_changes(
        live_connection, country, [insert_of(country, {"Code": "zz", "Name": "Lowercase"})]
    )

    assert result.committed is False
    assert result.failed_index == 0
    assert "CK_Country_Code" in (result.error or "")
    assert not any(
        r["Name"] == "Lowercase" for r in await rows_of(live_connection, "dbo", "Country")
    )


async def test_foreign_key_violation_is_sanitized_and_rolls_back(
    live_connection: Any, live_server: LiveServer, clean_slate: None
) -> None:
    """A bad FK must fail cleanly, and no secret may leak into the message."""
    provider = MssqlProvider()
    region = await table_of(live_connection, "dbo", "Region")

    result = await provider.execute_changes(
        live_connection,
        region,
        [insert_of(region, {"CountryCode": "XX", "Name": "Nowhere", "SortOrder": 1})],
    )

    assert result.committed is False
    assert "FK_Region_Country" in (result.error or "")
    # The real password never reaches a message. (The database is *named* SwissKnifeSample,
    # so the driver's own text legitimately contains "SwissKnife" as part of that name.)
    assert live_server.password not in (result.error or "")
    assert "PWD=" not in (result.error or "").upper()


async def test_truncation_violation_is_reported(live_connection: Any, clean_slate: None) -> None:
    provider = MssqlProvider()
    unique = await table_of(live_connection, "dbo", "UniqueOnly")

    result = await provider.execute_changes(
        live_connection, unique, [insert_of(unique, {"Code": "X" * 100})]
    )

    assert result.committed is False
    assert result.error


async def test_view_rows_are_refused_for_writes(live_connection: Any) -> None:
    """OQ-6: views are read-only in v1, so Apply must refuse before generating SQL."""
    provider = MssqlProvider()
    view = await table_of(live_connection, "dbo", "v_Country")
    assert view.updatable is False

    with pytest.raises(QueryError, match="read-only"):
        await provider.execute_changes(
            live_connection,
            view,
            [update_of(view, (("Code", "DE"),), {"Code": "DE", "Name": "x"}, {"Name": "y"})],
        )


async def test_keyless_table_rows_are_refused_for_writes(live_connection: Any) -> None:
    """S-4: no PK and no single-column UNIQUE means no deterministic identity."""
    provider = MssqlProvider()
    keyless = await table_of(live_connection, "dbo", "Keyless")
    assert keyless.primary_key is None
    assert keyless.identity_columns == ()
    assert keyless.updatable is False

    with pytest.raises(QueryError, match="read-only"):
        await provider.execute_changes(
            live_connection,
            keyless,
            [delete_of(keyless, (("PartA", 1), ("PartB", 1)), {"PartA": 1, "PartB": 1})],
        )


async def test_writing_a_temporal_period_column_is_refused_before_the_driver(
    live_connection: Any, clean_slate: None
) -> None:
    """The period columns are GENERATED ALWAYS, so they must be rejected up front."""
    provider = MssqlProvider()
    account = await table_of(live_connection, "dbo", "Account")
    before = await rows_of(live_connection, "dbo", "Account")

    # The period columns are GENERATED ALWAYS (metadata reports generated_always_type 1/2),
    # so the write is refused locally with this exact wording before the driver sees it.
    with pytest.raises(ValueError, match=r"server-managed \(GENERATED ALWAYS="):
        await provider.execute_changes(
            live_connection,
            account,
            [
                insert_of(
                    account,
                    # AccountId is an IDENTITY and is rejected first, so it is left to the
                    # server: the point of the test is that ValidFrom is refused.
                    {"Balance": 1, "ValidFrom": "2000-01-01T00:00:00"},
                )
            ],
        )

    assert await rows_of(live_connection, "dbo", "Account") == before


async def test_single_column_unique_is_accepted_as_row_identity(
    live_connection: Any, clean_slate: None
) -> None:
    provider = MssqlProvider()
    unique = await table_of(live_connection, "dbo", "UniqueOnly")
    assert unique.primary_key is None
    assert unique.identity_columns == ("Code",)
    assert unique.updatable is True
    row = (await rows_of(live_connection, "dbo", "UniqueOnly"))[0]

    result = await provider.execute_changes(
        live_connection,
        unique,
        [update_of(unique, (("Code", row["Code"]),), dict(row), {**dict(row), "Label": "renamed"})],
    )

    assert result.committed is True, result.error
    assert any(
        r["Code"] == row["Code"] and r["Label"] == "renamed"
        for r in await rows_of(live_connection, "dbo", "UniqueOnly")
    )


async def test_write_on_a_table_with_quoted_identifiers(
    live_connection: Any, clean_slate: None
) -> None:
    """A schema, table and columns whose names need bracketed quoting, written for real."""
    provider = MssqlProvider()
    weird = await table_of(live_connection, "Lookups", "Weird ]Name")
    assert weird.column("Col with space").is_identity
    assert weird.column("Cola]B").name == "Cola]B"

    result = await provider.execute_changes(
        live_connection,
        weird,
        [insert_of(weird, {"select": "con ] corchete", "Cola]B": 7})],
    )

    assert result.committed is True, result.error
    (statement,) = [entry.statement for entry in result.results]
    assert "[Lookups].[Weird ]]Name]" in statement.sql_parametrized
    assert "[Cola]]B]" in statement.sql_parametrized
    assert any(r["Cola]B"] == 7 for r in await rows_of(live_connection, "Lookups", "Weird ]Name"))


async def test_write_on_an_accented_schema(live_connection: Any, clean_slate: None) -> None:
    provider = MssqlProvider()
    moneda = await table_of(live_connection, "catálogos", "Moneda")

    result = await provider.execute_changes(
        live_connection,
        moneda,
        [insert_of(moneda, {"Código": "GBP", "Descripción": "libra"})],
    )

    assert result.committed is True, result.error
    assert any(r["Código"] == "GBP" for r in await rows_of(live_connection, "catálogos", "Moneda"))


async def test_escaping_hostile_values_round_trips_through_the_driver(
    live_connection: Any, clean_slate: None
) -> None:
    """Quotes, brackets, backslashes, emoji and a long string must survive binding."""
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")
    # dbo.Simple.Name is nvarchar(100), so the hostile payload has to fit it; the point is
    # that the driver binds it as a value, never as SQL text.
    hostile = "O'Brien \"; DROP TABLE [dbo].[Simple]; -- \\ ]] emoji \U0001f600 ñ 漢字 " + "x" * 20

    result = await provider.execute_changes(
        live_connection, table, [insert_of(table, {"Name": hostile, "Qty": -1})]
    )

    assert result.committed is True, result.error
    (stored,) = [r for r in await rows_of(live_connection, "dbo", "Simple") if r["Name"] == hostile]
    assert stored["Qty"] == -1
    assert await rows_of(live_connection, "dbo", "Simple")  # the table still exists


async def test_a_very_long_value_round_trips_through_a_max_column(
    live_connection: Any, clean_slate: None
) -> None:
    """A payload past the 4000-character boundary still binds as one value."""
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Typed")
    long_text = "á漢😀" * 1500  # ~6000 chars, beyond nvarchar(4000)

    result = await provider.execute_changes(
        live_connection,
        table,
        # Flag is NOT NULL, so it has to be supplied for the server to accept the row.
        [insert_of(table, {"TextMax": long_text, "Flag": 1})],
    )

    assert result.committed is True, result.error
    (stored,) = [
        r for r in await rows_of(live_connection, "dbo", "Typed") if r["TextMax"] == long_text
    ]
    assert len(stored["TextMax"]) == len(long_text)


async def test_parameter_binding_of_every_awkward_type(
    live_connection: Any, clean_slate: None
) -> None:
    """One INSERT carrying every type the fixture declares; all must read back equal."""
    import datetime as dt
    import decimal
    import uuid

    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Typed")
    values: dict[str, Any] = {
        "TextMax": "l" * 4000,
        "TextShort": "comillas ' y \" y ]]",
        "Binary": b"\x00\x01\xfe\xff",
        "BinaryMax": b"\xde\xad\xbe\xef" * 100,
        "Amount": decimal.Decimal("12345.6789"),
        "Ratio": 0.5,
        "Fixed": decimal.Decimal("99.99"),
        "Cash": decimal.Decimal("12.34"),
        "Uid": uuid.UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3301"),
        "Ts": dt.datetime(2024, 2, 29, 13, 45, 56, 123456),
        "Offset": dt.datetime(
            2024, 2, 29, 13, 45, 56, 123456, tzinfo=dt.timezone(dt.timedelta(hours=2))
        ),
        "Day": dt.date(2024, 2, 29),
        "Moment": dt.time(13, 45, 56, 123456),
        "Flag": True,
        "Json": '{"k": "v"}',
    }

    result = await provider.execute_changes(live_connection, table, [insert_of(table, values)])

    assert result.committed is True, result.error
    (stored,) = [
        r
        for r in await rows_of(live_connection, "dbo", "Typed")
        if r["TextMax"] == values["TextMax"]
    ]
    assert stored["TextShort"] == values["TextShort"]
    assert bytes(stored["Binary"]) == values["Binary"]
    assert bytes(stored["BinaryMax"]) == values["BinaryMax"]
    assert decimal.Decimal(stored["Amount"]) == values["Amount"]
    assert stored["Ratio"] == values["Ratio"]
    assert decimal.Decimal(stored["Cash"]) == values["Cash"]
    assert str(stored["Uid"]).lower() == str(values["Uid"])
    assert stored["Ts"] == values["Ts"]
    assert stored["Day"] == values["Day"]
    assert stored["Moment"] == values["Moment"]
    assert stored["Flag"] in (1, True)


async def test_after_trigger_fires_on_a_real_update(
    live_connection: Any, clean_slate: None
) -> None:
    """``trg_Region_AfterUpdate`` must actually run: the audit row proves the server ran it."""
    provider = MssqlProvider()
    region = await table_of(live_connection, "dbo", "Region")
    row = (await rows_of(live_connection, "dbo", "Region"))[0]
    key = (("RegionId", row["RegionId"]),)

    result = await provider.execute_changes(
        live_connection,
        region,
        [update_of(region, key, dict(row), {**dict(row), "Name": "Triggered"})],
    )

    assert result.committed is True, result.error
    audits = await live_connection.afetch(
        "SELECT [Action], [RowKey] FROM [dbo].[AuditLog] "
        "WHERE [TableName] = N'Region' AND [RowKey] = ?",
        (str(row["RegionId"]),),
    )
    assert audits, "the AFTER UPDATE trigger wrote nothing to AuditLog"
    assert audits[0]["Action"] == "UPDATE"


async def test_instead_of_trigger_view_is_readable_but_not_writable(
    live_connection: Any, clean_slate: None
) -> None:
    provider = MssqlProvider()
    view = await table_of(live_connection, "dbo", "v_Country")
    assert view.triggers[0].firing == "INSTEAD OF"
    assert (await provider.fetch_rows(live_connection, view, FetchSpec(limit=10))).count > 0

    with pytest.raises(QueryError, match="read-only"):
        await provider.execute_changes(
            live_connection,
            view,
            [update_of(view, (("Code", "DE"),), {"Code": "DE", "Name": "x"}, {"Name": "y"})],
        )


async def test_many_rows_apply_in_one_transaction(live_connection: Any, clean_slate: None) -> None:
    """500 inserts in a single transaction: rowcounts and atomicity all hold."""
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Bulk")
    await live_connection.afetch("DELETE FROM [dbo].[Bulk]")
    changes = [insert_of(table, {"Name": f"bulk-{index}"}) for index in range(500)]

    result = await provider.execute_changes(live_connection, table, changes)

    assert result.committed is True, result.error
    assert result.statement_count == 500
    assert all(entry.rowcount == 1 for entry in result.results)
    rows = await rows_of(live_connection, "dbo", "Bulk", limit=1000)
    assert {row["Name"] for row in rows} == {f"bulk-{i}" for i in range(500)}


async def test_one_bad_row_in_a_large_batch_rolls_everything_back(
    live_connection: Any, clean_slate: None
) -> None:
    """The 50th statement is over-long: the first 49 must be gone from the table afterwards."""
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")
    changes = [insert_of(table, {"Name": f"row-{index}", "Qty": index}) for index in range(100)]
    changes.insert(50, insert_of(table, {"Name": "z" * 300, "Qty": 1}))  # nvarchar(100) overflow

    result = await provider.execute_changes(live_connection, table, changes)

    assert result.committed is False
    assert result.failed_index == 50
    assert not any(
        r["Name"].startswith("row-") for r in await rows_of(live_connection, "dbo", "Simple")
    )


async def test_empty_change_list_commits_nothing(live_connection: Any) -> None:
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")

    result = await provider.execute_changes(live_connection, table, [])

    assert result.committed is True
    assert result.statement_count == 0
    assert result.inserted_keys == ()


async def test_apply_order_is_delete_then_insert(live_connection: Any, clean_slate: None) -> None:
    """FK-friendly ordering, asserted both on the builder and on the executed result."""
    from sql_table_swiss_knife.providers.sqlgen import sort_for_apply

    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")
    row = (await rows_of(live_connection, "dbo", "Simple"))[0]
    changes = [
        insert_of(table, {"Name": "new"}),
        delete_of(table, (("Id", row["Id"]),), dict(row)),
    ]

    assert [change.kind for change in sort_for_apply(changes)] == [
        ChangeKind.DELETE,
        ChangeKind.INSERT,
    ]

    result = await provider.execute_changes(live_connection, table, changes)
    assert result.committed is True, result.error
    assert [entry.change.kind for entry in result.results] == [
        ChangeKind.DELETE,
        ChangeKind.INSERT,
    ]


async def test_update_without_compare_mode_applies_over_a_concurrent_change(
    live_connection: Any, clean_slate: None
) -> None:
    """Without the guard the write lands: this documents that the mode is opt-in."""
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")
    (row,) = [
        r for r in await rows_of(live_connection, "dbo", "Simple") if r["Name"] == "populated"
    ]
    stale = dict(row)
    await live_connection.aexecute(
        "UPDATE [dbo].[Simple] SET [Qty] = 999 WHERE [Id] = ?", (row["Id"],)
    )

    result = await provider.execute_changes(
        live_connection, table, [update_of(table, (("Id", row["Id"]),), stale, {**stale, "Qty": 5})]
    )

    assert result.committed is True, result.error
    (stored,) = [r for r in await rows_of(live_connection, "dbo", "Simple") if r["Id"] == row["Id"]]
    assert stored["Qty"] == 5  # our value won; the other user's 999 did not raise a conflict


async def test_no_open_transaction_is_left_behind_after_a_failure(
    live_connection: Any, clean_slate: None
) -> None:
    """A leaked open transaction would block every later test; assert @@TRANCOUNT is 0."""
    provider = MssqlProvider()
    table = await table_of(live_connection, "dbo", "Simple")

    await provider.execute_changes(
        live_connection,
        table,
        [insert_of(table, {"Name": "y" * 300})],  # truncation → rollback
    )

    rows = await live_connection.afetch("SELECT @@TRANCOUNT AS trancount")
    assert rows[0]["trancount"] == 0


async def test_persisted_computed_column_is_readable_and_not_writable(
    live_connection: Any, clean_slate: None
) -> None:
    provider = MssqlProvider()
    order_line = await table_of(live_connection, "dbo", "OrderLine")
    total = order_line.column("Total")
    assert total.is_computed is True
    assert total.computed_persisted is True
    row = (await rows_of(live_connection, "dbo", "OrderLine"))[0]
    assert float(row["Total"]) == float(row["Qty"]) * float(row["Price"])

    with pytest.raises(ValueError, match="computed column"):
        await provider.execute_changes(
            live_connection,
            order_line,
            [insert_of(order_line, {"Qty": 1, "Price": 2, "Total": 99})],
        )


async def test_system_versioned_table_reports_its_history(live_connection: Any) -> None:
    account = await table_of(live_connection, "dbo", "Account")
    assert account.is_system_versioned is True
    assert account.history_schema == "dbo"
    assert account.history_table == "AccountHistory"
    assert (await table_of(live_connection, "dbo", "AccountHistory")).is_history_table is True


async def test_temporal_table_keeps_history_on_update(
    live_connection: Any, clean_slate: None
) -> None:
    """The server, not the app, owns the history table: a row lands there on UPDATE."""
    provider = MssqlProvider()
    account = await table_of(live_connection, "dbo", "Account")
    row = (await rows_of(live_connection, "dbo", "Account"))[0]
    before = await live_connection.afetch("SELECT COUNT(*) AS n FROM [dbo].[AccountHistory]")

    result = await provider.execute_changes(
        live_connection,
        account,
        [
            update_of(
                account,
                (("AccountId", row["AccountId"]),),
                dict(row),
                {**dict(row), "Balance": 250.00},
            )
        ],
    )

    assert result.committed is True, result.error
    after = await live_connection.afetch("SELECT COUNT(*) AS n FROM [dbo].[AccountHistory]")
    assert after[0]["n"] == before[0]["n"] + 1
