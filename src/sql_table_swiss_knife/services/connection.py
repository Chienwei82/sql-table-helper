"""Connection lifecycle service: profiles, test/connect/disconnect, session state.

FR-1: the connection manager. This module owns the single live connection so that
exactly one handle exists per session; the TUI asks this service (never a provider
directly) and renders the state it exposes.
"""

import dataclasses
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, cast

from ..domain.connection import AuthMode, ConnectionProfile, ConnectionResult, Environment
from ..providers import ActiveConnection, DatabaseProvider, get_provider
from ..storage import ProfileStore, SecretStore, default_secret_store

__all__ = [
    "ConnectionService",
    "ConnectionState",
    "SessionInfo",
    "duplicate_profile",
    "is_connection_lost",
]


class ProviderFactory(Protocol):
    """Resolves the provider registered under a profile's provider name."""

    def __call__(self, name: str) -> DatabaseProvider: ...


class ConnectionState(Enum):
    """UI-visible connection state (DESIGN §9.3)."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Immutable snapshot of the live session, rendered in the app header."""

    profile_name: str
    provider: str
    server: str
    database: str
    #: Which environment the profile declared (M8 safety); drives the header badge.
    environment: Environment = Environment.DEVELOPMENT

    @property
    def label(self) -> str:
        """Short ``server/database`` form used in the header and window title."""
        return f"{self.server}/{self.database}"


def duplicate_profile(profile: ConnectionProfile, new_name: str) -> ConnectionProfile:
    """Copy a profile under a new name, re-deriving its secret reference (FR-1.1).

    The copy keeps host, port, credentials and options but points at a *new* secret
    reference, so a duplicate never silently shares the original credential slot.
    """
    return dataclasses.replace(profile, name=new_name, secret_ref=f"{new_name}@{profile.host}")


class ConnectionService:
    """Profile lifecycle plus the single active connection (DESIGN §9.1).

    Passwords are read from the :class:`~sql_table_swiss_knife.storage.SecretStore`
    on demand and never held beyond the call (FR-1.6, S-sec-1). Every driver call
    is awaited here; the TUI runs these coroutines inside Textual workers so the
    event loop never blocks (FR-10).
    """

    def __init__(
        self,
        profiles: ProfileStore | None = None,
        secrets: SecretStore | None = None,
        provider_factory: ProviderFactory = get_provider,
    ) -> None:
        self._profiles = profiles if profiles is not None else ProfileStore()
        self._secrets = secrets if secrets is not None else default_secret_store()
        self._provider_factory = provider_factory
        self._provider: DatabaseProvider | None = None
        self._conn: ActiveConnection | None = None
        self._session: SessionInfo | None = None
        self._state: ConnectionState = ConnectionState.DISCONNECTED
        self._last_error: str | None = None
        self._in_flight: int = 0

    # -- observable state ---------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        """Current state; ``CONNECTING`` while a connect/test worker is running."""
        return self._state

    @property
    def session(self) -> SessionInfo | None:
        """Snapshot of the live session, or ``None`` when disconnected."""
        return self._session

    @property
    def last_error(self) -> str | None:
        """Message of the most recent failure, kept for the status line."""
        return self._last_error

    @property
    def is_busy(self) -> bool:
        """True while a connect/test operation is in flight (drives the spinner)."""
        return self._in_flight > 0

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    @property
    def secret_store(self) -> SecretStore:
        """The secret store backing this service (used by the profile editor)."""
        return self._secrets

    # -- profile management -------------------------------------------------

    def list_profiles(self) -> tuple[ConnectionProfile, ...]:
        """All stored profiles, sorted by name."""
        return self._profiles.load()

    def get_profile(self, name: str) -> ConnectionProfile:
        """Look up one profile.

        Raises:
            ProfileError: if no profile with that name exists.
        """
        return self._profiles.get(name)

    def save_profile(self, profile: ConnectionProfile) -> None:
        """Insert or replace a profile."""
        self._profiles.upsert(profile)

    def delete_profile(self, name: str) -> None:
        """Delete a stored profile.

        Raises:
            ProfileError: if the profile is unknown.
            ValueError: if it belongs to the live session (disconnect first).
        """
        if self._session is not None and self._session.profile_name == name:
            raise ValueError("cannot delete the profile of the active session")
        self._profiles.delete(name)

    def store_password(self, profile: ConnectionProfile, password: str) -> None:
        """Save a password in the OS keyring under the profile's secret reference.

        Raises:
            SecretStoreError: if the keyring refuses the write — the TUI reports it
                rather than silently dropping the credential (FR-1.6).
        """
        self._secrets.set(self._secret_ref(profile), password)

    @staticmethod
    def _secret_ref(profile: ConnectionProfile) -> str:
        """Keyring account name for a profile (``name@host``)."""
        return profile.secret_ref or f"{profile.name}@{profile.host}"

    # -- connection lifecycle -----------------------------------------------

    async def test_connection(self, profile: ConnectionProfile) -> ConnectionResult:
        """Open a throwaway connection, report the server, then close it (FR-1.3).

        Raises:
            ProviderError: on any driver failure. The message comes from the
                provider's sanitizer, so credentials never appear (S-sec-4).
        """
        provider = self._provider_factory(profile.provider)
        self._begin_operation()
        try:
            password = self._resolve_password(profile)
            info = await _probe(provider, profile, password)
        except Exception as exc:
            self._state = ConnectionState.ERROR
            self._last_error = str(exc)
            raise
        else:
            return ConnectionResult(
                server_version=str(info.get("server_version") or "unknown"),
                server_name=str(info.get("server_name") or profile.host),
                database=str(info.get("database_name") or profile.database or "master"),
                latency_ms=_as_latency(info.get("latency_ms")),
                auth_used=profile.auth,
            )
        finally:
            self._end_operation()

    async def connect(self, profile: ConnectionProfile) -> SessionInfo:
        """Connect, replacing any current session, and remember the session."""
        self._begin_operation()
        try:
            provider = self._provider_factory(profile.provider)
            password = self._resolve_password(profile)
            await self.disconnect()  # never leak the previous connection
            conn = await provider.connect(profile, password)
        except Exception as exc:
            self._state = ConnectionState.ERROR
            self._last_error = str(exc)
            raise
        else:
            self._provider = provider
            self._conn = conn
            self._session = SessionInfo(
                profile_name=profile.name,
                provider=profile.provider,
                server=conn.server,
                database=conn.database,
                environment=profile.environment,
            )
            self._state = ConnectionState.CONNECTED
            self._last_error = None
            return self._session
        finally:
            self._end_operation()

    async def disconnect(self) -> None:
        """Close the active connection if any. Safe to call repeatedly."""
        provider, conn = self._provider, self._conn
        self._provider, self._conn, self._session = None, None, None
        self._state = ConnectionState.DISCONNECTED
        if provider is None or conn is None:
            return
        try:
            await provider.disconnect(conn)
        except Exception as exc:  # a failed close must never block quit
            self._last_error = str(exc)

    def connection(self) -> ActiveConnection:
        """Return the active connection handle.

        Raises:
            RuntimeError: when disconnected — check :attr:`is_connected` first.
        """
        if self._conn is None:
            raise RuntimeError("not connected")
        return self._conn

    def provider(self) -> DatabaseProvider:
        """Return the provider that owns the active connection.

        Raises:
            RuntimeError: when disconnected.
        """
        if self._provider is None:
            raise RuntimeError("not connected")
        return self._provider

    # -- internals ----------------------------------------------------------

    def _resolve_password(self, profile: ConnectionProfile) -> str | None:
        """Look the password up in the secret store; integrated auth has none."""
        if profile.auth is AuthMode.INTEGRATED:
            return None
        return self._secrets.get(self._secret_ref(profile))

    def _begin_operation(self) -> None:
        self._in_flight += 1
        self._state = ConnectionState.CONNECTING
        self._last_error = None

    def _end_operation(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        still_connecting = self._state is ConnectionState.CONNECTING
        if self._in_flight == 0 and self._conn is None and still_connecting:
            # The operation failed and left nothing connected: back to idle.
            self._state = ConnectionState.DISCONNECTED


def _as_latency(value: object) -> int:
    """Coerce a provider-reported latency to whole milliseconds."""
    try:
        return max(0, int(cast("int | float | str", value)))
    except TypeError, ValueError:
        return 0


async def _probe(
    provider: DatabaseProvider, profile: ConnectionProfile, password: str | None
) -> dict[str, object]:
    """Measure round-trip latency and read the server identification.

    ``test_connection`` is an optional provider extra, so providers without it fall
    back to connect → read the current database → disconnect, taking the server name
    from the handle. The connection is always closed before returning.
    """
    tester = getattr(provider, "test_connection", None)
    if tester is not None:
        started = time.perf_counter()
        info: dict[str, object] = dict(await tester(profile, password))
        info["latency_ms"] = int((time.perf_counter() - started) * 1000)
        return info

    started = time.perf_counter()
    conn = await provider.connect(profile, password)
    try:
        databases = await provider.list_databases(conn)
    finally:
        await provider.disconnect(conn)
    current = next((db.name for db in databases if db.is_current), conn.database)
    return {
        "server_name": conn.server,
        "server_version": provider.name,
        "database_name": current,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


#: Substrings that mark a provider error as "the link went away" rather than
#: "the database said no". Matching on the message is the only handle available: the
#: drivers report a dropped TCP connection, a closed handle and a login timeout with
#: three different exception classes and no common "is this fatal" flag.
_CONNECTION_LOST_MARKERS: tuple[str, ...] = (
    "connection was terminated",
    "connection is busy",
    "no connection",
    "not connected",
    "server was not found",
    "communications link failure",  # the exact ODBC wording, plural
    "connection reset",
    "broken pipe",
    "connection timeout",
    "login timeout",
    "08001",  # SQLSTATE: client unable to establish connection
    "08s01",  # SQLSTATE: communication link failure
    "hyt00",  # ODBC timeout
    "hyt01",  # ODBC connection timeout
    "08s02",  # SQLSTATE: connection name in use
    "invalid connection",
    "socket",
)


#: Exception types that *are* a lost connection regardless of their message. A broken
#: pipe or a reset socket says so by its very type, and a driver that wraps it in a
#: terse message would otherwise hide the one unambiguous signal available.
_CONNECTION_LOST_TYPES: tuple[type[BaseException], ...] = (
    ConnectionResetError,
    BrokenPipeError,
    ConnectionAbortedError,
)


def is_connection_lost(error: BaseException | str) -> bool:
    """Whether ``error`` means the session is gone, rather than the query failing.

    This distinction decides the whole shape of the recovery: a constraint violation
    leaves the session usable and the user should fix a cell, whereas a dropped
    connection means every subsequent call will fail too and the only useful action is
    to reconnect. Misreading the second as the first leaves the user editing into a
    void; misreading the first as the second discards their work for nothing.

    Two signals, in order: an unambiguous exception type, then a marker in the message.
    The message check is one-directional — only a known marker counts — so an
    unrecognised error stays an ordinary query failure, which is the recoverable one.
    """
    if isinstance(error, _CONNECTION_LOST_TYPES):
        return True
    text = str(error).lower()
    return any(marker in text for marker in _CONNECTION_LOST_MARKERS)
