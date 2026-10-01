"""Fixtures for live SQL Server integration tests.

The whole module is skipped unless ``SWISSKNIFE_TEST_DB_URL`` points at a reachable
server, e.g.::

    export SWISSKNIFE_TEST_DB_URL="mssql://sa:SwissKnife%212022_Test@localhost:1433/SwissKnifeSample"
    uv run pytest -m live

Start the server with ``cd tests/live && docker compose up -d``.
"""

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

import pytest

from sql_table_swiss_knife.domain import AuthMode, ConnectionOptions, ConnectionProfile

#: Points at the docker compose server in tests/live/docker-compose.yml.
DEFAULT_TEST_DB_URL = "mssql://sa:SwissKnife%212022_Test@localhost:1433/SwissKnifeSample"

#: Environment variable enabling the live suite.
TEST_DB_URL_ENV = "SWISSKNIFE_TEST_DB_URL"


@dataclass(frozen=True, slots=True)
class LiveServer:
    """Connection details of the live test server."""

    host: str
    port: int
    database: str
    username: str
    password: str

    def profile(self, **overrides: Any) -> ConnectionProfile:
        """Build a connection profile for this server (no secret is ever persisted)."""
        defaults: dict[str, Any] = {
            "name": "live",
            "provider": "mssql",
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "auth": AuthMode.SQL,
            "username": self.username,
            # the docker image uses a self-signed certificate
            "options": ConnectionOptions(trust_server_certificate=True),
        }
        defaults.update(overrides)
        return ConnectionProfile(**defaults)


def _unavailable(reason: str) -> str:
    return (
        f"{reason} — start the sample server with "
        "'cd tests/live && docker compose up -d' and set "
        f"{TEST_DB_URL_ENV} (currently: {os.environ.get(TEST_DB_URL_ENV)!r})"
    )


def _parse_url(url: str) -> LiveServer:
    parsed = urlparse(url)
    if parsed.scheme not in {"mssql", "sqlserver"}:
        raise ValueError(f"unsupported scheme {parsed.scheme!r}; expected mssql://")
    return LiveServer(
        host=parsed.hostname or "localhost",
        port=parsed.port or 1433,
        database=(parsed.path or "/master").lstrip("/") or "master",
        username=unquote(parsed.username or "sa"),
        password=unquote(parsed.password or ""),
    )


@pytest.fixture(scope="session")
def live_server() -> Iterator[LiveServer]:
    """The live server description, or a skip when the server is not available."""
    url = os.environ.get(TEST_DB_URL_ENV, DEFAULT_TEST_DB_URL)
    try:
        server = _parse_url(url)
    except ValueError as exc:
        pytest.skip(_unavailable(str(exc)))
        return
    if not server.password:
        pytest.skip(_unavailable("no password in the test database URL"))
        return

    try:
        import pyodbc  # type: ignore[import-not-found]  # noqa: F401
    except Exception as exc:  # driver stack missing on the host
        pytest.skip(_unavailable(f"pyodbc is not importable ({exc})"))
        return

    yield server


@pytest.fixture
async def mssql_provider() -> AsyncIterator[Any]:
    """An ``MssqlProvider`` instance for the live suite."""
    from sql_table_swiss_knife.providers import get_provider

    yield get_provider("mssql")


@pytest.fixture
async def live_connection(live_server: LiveServer, mssql_provider: Any) -> AsyncIterator[Any]:
    """A connected :class:`MssqlConnection`, disconnected after the test."""
    from sql_table_swiss_knife.providers.mssql.provider import MssqlConnection

    conn: MssqlConnection | None = None
    try:
        conn = await mssql_provider.connect(live_server.profile(), live_server.password)
    except Exception as exc:
        pytest.skip(_unavailable(f"cannot connect to {live_server.host}: {exc}"))
    assert conn is not None
    try:
        yield conn
    finally:
        await mssql_provider.disconnect(conn)


@pytest.fixture
async def clean_slate(live_connection: Any) -> AsyncIterator[None]:
    """Restore the fixture data after a test that writes.

    The write tests must not depend on order or on a freshly seeded database, so every
    table they touch is truncated and re-seeded before *and* after the test. Restoring
    before matters as much as after: a previous aborted run must not poison this one.
    """
    await _reset(live_connection)
    try:
        yield
    finally:
        await _reset(live_connection)


#: Tables the live write tests are allowed to modify, with the rows they must contain.
#: dbo.Region/RegionAlias/AuditLog are left to the seed script on purpose: the region
#: fixtures carry a self-referencing FK, so truncating them in the wrong order breaks
#: the parent relationship. Tests that touch them delete only the rows they created.
_RESET_STATEMENTS: tuple[str, ...] = (
    "DELETE FROM [dbo].[AuditLog]",
    # dbo.Account is system-versioned, so the history is written by the server, not
    # inserted. Clearing it means switching versioning off for a moment: a temporal history
    # table rejects DELETE outright ("Cannot delete rows from a temporal history table",
    # Msg 13560). Deleting the current rows leaves the history rows behind on their own.
    "ALTER TABLE [dbo].[Account] SET (SYSTEM_VERSIONING = OFF)",
    "DELETE FROM [dbo].[Account]",
    "ALTER TABLE [dbo].[Account] "
    "SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[AccountHistory]))",
    "DELETE FROM [dbo].[RegionAlias]",
    "DELETE FROM [dbo].[Region]",
    "DELETE FROM [dbo].[Bulk]",
    "DELETE FROM [dbo].[Simple]",
    "DELETE FROM [dbo].[Typed]",
    "DELETE FROM [dbo].[UniqueOnly]",
    "DELETE FROM [dbo].[Item]",
    "DELETE FROM [dbo].[Store]",
    "DELETE FROM [dbo].[Supplier]",
    "DELETE FROM [Lookups].[Weird ]]Name]",
    "DELETE FROM [catálogos].[Moneda]",
    "DELETE FROM [dbo].[Keyless]",
    "DBCC CHECKDB ('SwissKnifeSample') WITH NO_INFOMSGS, PHYSICAL_ONLY",
)

_RESEED_STATEMENTS: tuple[str, ...] = (
    # RegionId is IDENTITY, and a previous run leaves the counter wherever it finished, so
    # the self-referencing child row has to look its parent up by key. Hardcoding 1 works
    # only on a freshly seeded database and fails the FK as soon as the counter drifts.
    "INSERT INTO [dbo].[Region] (CountryCode, ParentRegionId, Name, SortOrder) "
    "VALUES (N'DE', NULL, N'Bavaria', 1)",
    "INSERT INTO [dbo].[Region] (CountryCode, ParentRegionId, Name, SortOrder) "
    "SELECT N'DE', RegionId, N'Munich', 2 FROM [dbo].[Region] WHERE Name = N'Bavaria'",
    "INSERT INTO [dbo].[Region] (CountryCode, ParentRegionId, Name, SortOrder) "
    "VALUES (N'FR', NULL, N'Normandy', 3)",
    "INSERT INTO [dbo].[Region] (CountryCode, ParentRegionId, Name, SortOrder) "
    "VALUES (N'JP', NULL, N'Kanto', 4)",
    "INSERT INTO [dbo].[RegionAlias] (RegionId, Lang, Label) "
    "SELECT RegionId, N'de', Name FROM [dbo].[Region] WHERE Name = N'Bavaria'",
    "INSERT INTO [dbo].[RegionAlias] (RegionId, Lang, Label) "
    "SELECT RegionId, N'en', N'Bavaria (EN)' FROM [dbo].[Region] WHERE Name = N'Bavaria'",
    "INSERT INTO [dbo].[RegionAlias] (RegionId, Lang, Label) "
    "SELECT RegionId, N'fr', N'Normandie' FROM [dbo].[Region] WHERE Name = N'Normandy'",
    "INSERT INTO [dbo].[UniqueOnly] (Code, Label) VALUES (N'EUR', N'Euro'), (N'USD', N'Dollar')",
    "INSERT INTO [dbo].[Simple] (Name, Qty) VALUES (N'with nulls', NULL), (N'populated', 7)",
    # StoreId/Supplier are IDENTITY and the counters carry over between runs, so the child
    # rows resolve their parents by name instead of assuming ids 1 and 1 still exist.
    "INSERT INTO [dbo].[Supplier] (Name) VALUES (N'Acme')",
    "INSERT INTO [dbo].[Store] (Name) VALUES (N'Berlin'), (N'Madrid')",
    "INSERT INTO [dbo].[Item] (StoreId, Supplier, Qty) "
    "SELECT s.StoreId, p.SupplierId, 10 FROM [dbo].[Store] AS s, [dbo].[Supplier] AS p "
    "WHERE s.Name = N'Berlin' AND p.Name = N'Acme'; "
    "INSERT INTO [dbo].[Item] (StoreId, Supplier, Qty) "
    "SELECT s.StoreId, p.SupplierId, 5 FROM [dbo].[Store] AS s, [dbo].[Supplier] AS p "
    "WHERE s.Name = N'Berlin' AND p.Name = N'Acme'",
    "INSERT INTO [dbo].[Keyless] (PartA, PartB, Note) VALUES (1, 1, N'first'), (1, 2, N'second')",
    # dbo.Account is system-versioned: its period columns are GENERATED ALWAYS, so only
    # Balance is supplied and the server writes ValidFrom/ValidTo itself.
    "INSERT INTO [dbo].[Account] (Balance) VALUES (100.00)",
    # dbo.Typed.Flag is NOT NULL, so a long-value write cannot omit it.
    "INSERT INTO [dbo].[Typed] (TextShort, Amount, Flag) VALUES (N'corta', 12345.6789, 1)",
    # [Col with space] is IDENTITY: the server assigns it.
    "INSERT INTO [Lookups].[Weird ]]Name] ([select], [Cola]]B]) VALUES (N'primero', 42)",
    "INSERT INTO [catálogos].[Moneda] ([Código], [Descripción]) VALUES (N'EUR', N'euro')",
)


async def _reset(conn: Any) -> None:
    """Truncate the writable fixture tables and put the seed rows back."""
    for statement in _RESET_STATEMENTS:
        await conn.afetch(statement)
    # Reseed the identities before inserting, not after: the counters then restart from
    # the (now empty) tables every run, so the seeded ids are the same whatever a previous
    # run left behind and tests can assert on them.
    for table in ("[dbo].[Region]", "[dbo].[Store]", "[dbo].[Supplier]", "[dbo].[Item]"):
        await conn.afetch(f"DBCC CHECKIDENT ('{table}', RESEED) WITH NO_INFOMSGS")
    for statement in _RESEED_STATEMENTS:
        await conn.afetch(statement)


@pytest.fixture
async def second_connection(live_server: LiveServer, mssql_provider: Any) -> AsyncIterator[Any]:
    """An independent connection, for proving a write is really committed on the server."""
    conn = await mssql_provider.connect(live_server.profile(), live_server.password)
    try:
        yield conn
    finally:
        await mssql_provider.disconnect(conn)
