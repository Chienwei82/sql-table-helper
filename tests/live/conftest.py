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
