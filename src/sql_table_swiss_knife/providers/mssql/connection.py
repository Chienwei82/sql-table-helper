"""ODBC connection-string construction and driver import for Microsoft SQL Server.

The only place allowed to build a full connection string; ``sanitize_connection_string()``
is the only function whose output may reach a log or a user-facing message (DESIGN §8.2).
"""

import asyncio
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, TypeVar

from ...domain.connection import AuthMode, ConnectionProfile
from ..errors import AuthError, ConnectError
from .errors import map_pyodbc_error

#: Result type of a driver call dispatched to the worker thread.
_T = TypeVar("_T")

__all__ = [
    "PyodbcModule",
    "build_connection_string",
    "import_pyodbc",
    "sanitize_connection_string",
]


class PyodbcModule(Protocol):
    """Structural subset of the ``pyodbc`` module we use (keeps tests driver-free)."""

    def connect(self, connection_string: str, **kwargs: Any) -> Any: ...

    def Cursor(self) -> Any: ...

    def Error(self) -> type[BaseException]: ...


def import_pyodbc() -> PyodbcModule:
    """Import ``pyodbc`` lazily, translating a missing driver stack into ``ConnectError``."""
    try:
        import pyodbc  # type: ignore[import-not-found]  # no stubs on some hosts
    except ImportError as exc:  # pragma: no cover - depends on host setup
        raise ConnectError(
            "the 'pyodbc' driver could not be loaded; install unixODBC and the "
            "'ODBC Driver 18 for SQL Server' driver, then retry"
        ) from exc
    module: PyodbcModule = pyodbc
    return module


def _quote(value: str) -> str:
    """Brace-quote an ODBC value containing ``;``, ``{`` or ``}``."""
    if any(char in value for char in ";{}"):
        return "{" + value + "}"
    return value


def build_connection_string(profile: ConnectionProfile, password: str | None = None) -> str:
    """Build the ODBC connection string for a profile.

    Raises:
        AuthError: SQL authentication without a password (caller must prompt or fail).
        ValueError: a password was supplied for an integrated-auth profile.
    """
    options = profile.options
    parts = [
        f"DRIVER={_quote(options.driver)}",
        f"SERVER={_quote(f'{profile.host},{profile.port}')}",
    ]
    if profile.database:
        parts.append(f"DATABASE={_quote(profile.database)}")

    if profile.auth is AuthMode.INTEGRATED:
        if password is not None:
            raise ValueError("integrated authentication does not use a password")
        parts.append("Trusted_Connection=yes")
    else:
        if not password:
            raise AuthError(
                f"no password available for profile {profile.name!r}; "
                "prompt for it or store it in the OS keyring"
            )
        parts.append(f"UID={_quote(profile.username or '')}")
        parts.append(f"PWD={_quote(password)}")

    parts.append("Encrypt=yes" if options.encrypt else "Encrypt=no")
    parts.append(
        "TrustServerCertificate=yes"
        if options.trust_server_certificate
        else "TrustServerCertificate=no"
    )
    parts.append(f"Connection Timeout={options.connect_timeout_s}")
    if options.application_intent:
        parts.append(f"ApplicationIntent={options.application_intent}")
    return ";".join(parts)


def sanitize_connection_string(connection_string: str) -> str:
    """Return a log/UI-safe rendering with the password masked."""
    parts: list[str] = []
    for chunk in connection_string.split(";"):
        key, separator, _ = chunk.partition("=")
        if not separator:
            parts.append(chunk)
        elif key.strip().upper() in {"PWD", "PASSWORD"} or key.strip().upper() in {"UID", "USER"}:
            parts.append(f"{key}=***")
        else:
            parts.append(chunk)
    return ";".join(parts)


class SingleThreadRunner:
    """Serializes driver calls onto one dedicated thread (DESIGN §5.1).

    pyodbc connections are not thread-safe; the design mandates a single worker thread
    per connection. The executor is closed on :meth:`close`.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stsk-mssql")

    async def run(self, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, lambda: func(*args, **kwargs))
        except BaseException as exc:
            raise map_pyodbc_error(exc) from exc

    async def close(self) -> None:
        self._executor.shutdown(wait=True)


def fetch_all(
    cursor: Any, sql: str, params: Sequence[object] | None = None
) -> list[dict[str, Any]]:
    """Execute a query and return rows as plain dicts (no driver types leak out)."""
    cursor.execute(sql, tuple(params) if params is not None else None)
    description = cursor.description or ()
    columns = [str(item[0]) for item in description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
