"""Live tests for connection handling, error translation and the read path.

Two things a fake driver cannot tell you, and these cover:

* whether the **real** ODBC driver accepts the connection string the app builds (driver
  name, ``Encrypt``/``TrustServerCertificate``, the ``?`` parameter placeholders);
* whether a **real** server produces the error codes and SQLSTATEs the error mapper keys
  on — an auth failure is 18456, a missing object is 42S02, and so on. A mapper tested
  only against hand-written exceptions can easily disagree with the server.
"""

import socket
from typing import Any

import pytest

from sql_table_swiss_knife.domain import (
    AuthMode,
    ConnectionOptions,
    ConnectionProfile,
    FetchSpec,
    FilterOp,
    RowFilter,
    SortKey,
)
from sql_table_swiss_knife.providers import AuthError, ConnectError, MetadataError
from sql_table_swiss_knife.providers.mssql.connection import build_connection_string
from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider
from tests.live.conftest import LiveServer

pytestmark = pytest.mark.live


def _free_port() -> int:
    """A port nothing is listening on, for the "server unreachable" tests."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _probe_profile(**overrides: Any) -> ConnectionProfile:
    """A profile for a server that is *not* the fixture one (the error paths)."""
    fields: dict[str, Any] = {
        "name": "probe",
        "provider": "mssql",
        "host": "127.0.0.1",
        "port": _free_port(),
        "database": "master",
        "auth": AuthMode.SQL,
        "username": "sa",
        "options": ConnectionOptions(trust_server_certificate=True, connect_timeout_s=3),
    }
    fields.update(overrides)
    return ConnectionProfile(**fields)


async def test_connect_disconnect_round_trip(live_server: LiveServer, mssql_provider: Any) -> None:
    conn = await mssql_provider.connect(live_server.profile(), live_server.password)
    assert conn.closed is False
    assert conn.provider_name == "mssql"
    assert conn.database == live_server.database
    await mssql_provider.disconnect(conn)
    assert conn.closed is True


async def test_wrong_password_raises_auth_error_without_leaking_it(
    live_server: LiveServer, mssql_provider: Any
) -> None:
    """The real server answers 18456; the mapped message must not echo the secret."""
    with pytest.raises(AuthError) as caught:
        await mssql_provider.connect(live_server.profile(), "definitely-not-the-password")

    message = str(caught.value)
    assert "definitely-not-the-password" not in message
    assert "login failed" in message.lower()


async def test_unknown_user_raises_auth_error(live_server: LiveServer, mssql_provider: Any) -> None:
    profile = live_server.profile(username="no_such_login_12345")
    with pytest.raises(AuthError):
        await mssql_provider.connect(profile, live_server.password)


async def test_unreachable_port_raises_connect_error(mssql_provider: Any) -> None:
    with pytest.raises(ConnectError):
        await mssql_provider.connect(_probe_profile(), "irrelevant")


async def test_unknown_database_raises_a_mapped_error(
    live_server: LiveServer, mssql_provider: Any
) -> None:
    profile = live_server.profile(database="NoSuchDatabase_98765")
    with pytest.raises((ConnectError, MetadataError)) as caught:
        await mssql_provider.connect(profile, live_server.password)
    assert live_server.password not in str(caught.value)


async def test_missing_driver_name_is_reported_clearly(live_server: LiveServer) -> None:
    """A driver that is not installed must fail with the install hint, not an OSError."""
    profile = live_server.profile(
        options=ConnectionOptions(
            trust_server_certificate=True, driver="ODBC Driver 99 for SQL Server"
        )
    )
    with pytest.raises(ConnectError) as caught:
        await MssqlProvider().connect(profile, live_server.password)
    assert "SQL Server" in str(caught.value)


async def test_sql_auth_without_a_password_is_refused_before_any_io(
    live_server: LiveServer,
) -> None:
    """No secret, no connection: the builder refuses rather than dialling with PWD= empty."""
    with pytest.raises(AuthError):
        build_connection_string(live_server.profile(), None)


async def test_integrated_auth_rejects_a_password(live_server: LiveServer) -> None:
    profile = live_server.profile(auth=AuthMode.INTEGRATED, username=None)
    with pytest.raises(ValueError, match="does not use a password"):
        build_connection_string(profile, "secret")


async def test_connection_string_carries_the_expected_options(live_server: LiveServer) -> None:
    """The exact string the driver receives — the one thing a fake driver never validates."""
    from sql_table_swiss_knife.providers.mssql.connection import sanitize_connection_string

    string = build_connection_string(live_server.profile(), live_server.password)
    assert "DRIVER=ODBC Driver 18 for SQL Server" in string
    assert f"SERVER={live_server.host},{live_server.port}" in string
    assert f"DATABASE={live_server.database}" in string
    assert "Encrypt=yes" in string
    assert "TrustServerCertificate=yes" in string
    assert f"UID={live_server.username}" in string
    # Present, because the driver needs it — but the log-safe rendering must mask it.
    assert live_server.password in string
    assert live_server.password not in sanitize_connection_string(string)


async def test_self_signed_certificate_is_rejected_without_trust(
    live_server: LiveServer, mssql_provider: Any
) -> None:
    """``trust_server_certificate=False`` against the image's self-signed cert must fail.

    A pass here would mean the option is decorative and encryption is not negotiated.
    """
    profile = live_server.profile(options=ConnectionOptions(trust_server_certificate=False))
    with pytest.raises(ConnectError) as caught:
        await mssql_provider.connect(profile, live_server.password)
    message = str(caught.value).lower()
    assert "certificate" in message or "ssl" in message


async def test_filters_are_bound_not_interpolated(live_connection: Any) -> None:
    provider = MssqlProvider()
    table = await provider.get_table_metadata(live_connection, "dbo", "Country")

    page = await provider.fetch_rows(
        live_connection, table, FetchSpec(filters=(RowFilter("Code", FilterOp.EQ, "DE"),))
    )
    assert [row["Code"] for row in page.rows] == ["DE"]

    # A value that would be catastrophic if it ever reached the SQL text must match nothing.
    nothing = await provider.fetch_rows(
        live_connection,
        table,
        FetchSpec(filters=(RowFilter("Code", FilterOp.EQ, "'; DROP TABLE Country; --"),)),
    )
    assert nothing.count == 0
    assert (await provider.fetch_rows(live_connection, table, FetchSpec(limit=10))).count > 0


async def test_is_null_and_is_not_null_filters(live_connection: Any) -> None:
    provider = MssqlProvider()
    region = await provider.get_table_metadata(live_connection, "dbo", "Region")

    roots = await provider.fetch_rows(
        live_connection,
        region,
        FetchSpec(filters=(RowFilter("ParentRegionId", FilterOp.IS_NULL),), limit=50),
    )
    assert roots.count == 3
    assert all(row["ParentRegionId"] is None for row in roots.rows)

    children = await provider.fetch_rows(
        live_connection,
        region,
        FetchSpec(filters=(RowFilter("ParentRegionId", FilterOp.IS_NOT_NULL),), limit=50),
    )
    assert children.count == 1


async def test_sorting_descending_by_a_text_column(live_connection: Any) -> None:
    provider = MssqlProvider()
    country = await provider.get_table_metadata(live_connection, "dbo", "Country")

    page = await provider.fetch_rows(
        live_connection, country, FetchSpec(sort=(SortKey("Name", descending=True),), limit=10)
    )
    names = [str(row["Name"]) for row in page.rows]
    assert names == sorted(names, reverse=True)


async def test_offset_paging_partitions_the_table(live_connection: Any) -> None:
    provider = MssqlProvider()
    country = await provider.get_table_metadata(live_connection, "dbo", "Country")

    first = await provider.fetch_rows(live_connection, country, FetchSpec(limit=2, offset=0))
    second = await provider.fetch_rows(live_connection, country, FetchSpec(limit=2, offset=2))
    assert first.count == 2
    assert first.has_more is True
    assert second.count == 1
    assert second.has_more is False
    assert {r["Code"] for r in first.rows}.isdisjoint({r["Code"] for r in second.rows})


async def test_table_without_identity_still_reads(live_connection: Any) -> None:
    """A keyless table has no deterministic order, but reading it must still work."""
    provider = MssqlProvider()
    keyless = await provider.get_table_metadata(live_connection, "dbo", "Keyless")
    assert keyless.identity_columns == ()
    assert (await provider.fetch_rows(live_connection, keyless, FetchSpec(limit=10))).count == 2


async def test_keyset_paging_walks_every_row_exactly_once(live_connection: Any) -> None:
    """Keyset paging must neither skip nor repeat a row while walking the table."""
    provider = MssqlProvider()
    table = await provider.get_table_metadata(live_connection, "dbo", "Simple")
    await live_connection.afetch("DELETE FROM [dbo].[Simple]")
    await live_connection.afetch(
        "INSERT INTO [dbo].[Simple] (Name, Qty) SELECT N'k' + CAST(n AS nvarchar(10)), n "
        "FROM (SELECT TOP 40 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS n "
        "FROM sys.all_objects) AS numbered"
    )
    try:
        seen: list[int] = []
        after: Any = None
        for _ in range(12):  # 40 rows in pages of 5 => 8 pages; 12 is a safety margin
            page = await provider.fetch_rows(
                live_connection,
                table,
                FetchSpec(limit=5, sort=(SortKey("Id"),), after_key=after),
            )
            seen.extend(int(str(row["Id"])) for row in page.rows)
            if not page.has_more:
                break
            after = (("Id", page.rows[-1]["Id"]),)
        assert len(seen) == 40, seen
        assert len(set(seen)) == 40, "keyset paging repeated a row"
        assert seen == sorted(seen)
    finally:
        await live_connection.afetch("DELETE FROM [dbo].[Simple]")
        await live_connection.afetch(
            "INSERT INTO [dbo].[Simple] (Name, Qty) VALUES (N'with nulls', NULL), (N'populated', 7)"
        )


async def test_keyset_paging_over_a_composite_key_runs_on_a_real_server(
    live_connection: Any,
) -> None:
    """``dbo.RegionAlias`` is keyed by ``(RegionId, Lang)``, so keyset paging applies.

    The dialect rendered the multi-column predicate as a row-value comparison,
    ``([RegionId], [Lang]) > (@p0, @p1)``. That is PostgreSQL/MySQL syntax — T-SQL has no
    row-value constructor comparison — so every page after the first failed with a syntax
    error. Only the *single*-column case had ever been run against a server, which is why
    a green live suite did not catch it.
    """
    provider = MssqlProvider()
    table = await provider.get_table_metadata(live_connection, "dbo", "RegionAlias")
    assert table.identity_columns == ("RegionId", "Lang")
    sort = tuple(SortKey(name) for name in table.identity_columns)

    first = await provider.fetch_rows(
        live_connection, table, FetchSpec(limit=1, offset=0, sort=sort)
    )
    assert first.count == 1
    after = tuple((name, first.rows[0][name]) for name in table.identity_columns)

    second = await provider.fetch_rows(
        live_connection,
        table,
        FetchSpec(limit=10, offset=0, sort=sort, after_key=after),
    )
    assert second.rows, "the page after a composite key must not be empty"
    for row in second.rows:
        key = tuple(row[name] for name in table.identity_columns)
        assert key > tuple(value for _, value in after)


async def test_composite_keyset_paging_walks_every_row_exactly_once(live_connection: Any) -> None:
    """Valid SQL is not enough: the expanded predicate must be a *total* order."""
    provider = MssqlProvider()
    table = await provider.get_table_metadata(live_connection, "dbo", "RegionAlias")
    sort = tuple(SortKey(name) for name in table.identity_columns)

    everything = await provider.fetch_rows(
        live_connection, table, FetchSpec(limit=1000, offset=0, sort=sort)
    )
    seen: list[tuple[object, ...]] = []
    after: Any = None
    for _ in range(50):
        page = await provider.fetch_rows(
            live_connection, table, FetchSpec(limit=1, offset=0, sort=sort, after_key=after)
        )
        if not page.rows:
            break
        last = page.rows[-1]
        seen.append(tuple(last[name] for name in table.identity_columns))
        after = tuple((name, last[name]) for name in table.identity_columns)
    assert len(seen) == everything.count, seen
    assert len(set(seen)) == len(seen), "a composite-key page was returned twice"


async def test_query_against_a_missing_object_raises_metadata_error(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers import QueryError

    with pytest.raises((MetadataError, QueryError)):
        await live_connection.afetch("SELECT * FROM [dbo].[NoSuchTable_12345]")


async def test_syntax_error_is_reported_not_swallowed(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers import ProviderError

    with pytest.raises(ProviderError):
        await live_connection.afetch("SELEKT 1")
