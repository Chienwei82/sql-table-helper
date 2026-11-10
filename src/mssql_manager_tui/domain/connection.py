"""Connection profile domain models."""

from dataclasses import dataclass
from enum import Enum
from typing import Final

from .identifiers import validate_identifier

__all__ = [
    "AuthMode",
    "ConnectionOptions",
    "ConnectionProfile",
    "ConnectionResult",
    "Environment",
]


class AuthMode(Enum):
    """Authentication mode for a connection profile."""

    SQL = "sql"  # username + password (secret via SecretStore)
    INTEGRATED = "integrated"  # Windows integrated auth; no secret


class Environment(Enum):
    """Which environment a profile points at (M8 safety).

    The environment drives two visible things: the badge in the app header, and
    whether the profile opens **read-only** by default (:attr:`read_only_by_default`).
    A production profile that can write by accident is exactly the failure this
    milestone exists to prevent, so the safe answer is the default one and turning
    writing on is always an explicit, visible act.
    """

    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def read_only_by_default(self) -> bool:
        """True for environments that open read-only unless the profile says otherwise."""
        return self is Environment.PRODUCTION

    @property
    def badge(self) -> str:
        """Short label for the header badge: ``PROD``, ``STG``, ``TEST``, ``DEV``."""
        return _ENVIRONMENT_BADGE[self]

    @property
    def requires_typed_confirmation(self) -> bool:
        """Environments where an Apply must be confirmed by typing a word (FR-7.5)."""
        return self is Environment.PRODUCTION

    @classmethod
    def parse(cls, value: str) -> Environment:
        """Parse a stored/typed environment name.

        Raises:
            ValueError: when the name is not a known environment.
        """
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            known = ", ".join(item.value for item in cls)
            raise ValueError(f"unknown environment {value!r}; expected one of {known}") from exc


#: Header badge text per environment (M8: colour + word, never colour alone — FR-3.7).
_ENVIRONMENT_BADGE: Final[dict[Environment, str]] = {
    Environment.DEVELOPMENT: "DEV",
    Environment.TEST: "TEST",
    Environment.STAGING: "STG",
    Environment.PRODUCTION: "PROD",
}


@dataclass(frozen=True, slots=True)
class ConnectionOptions:
    """Provider-level connection options."""

    encrypt: bool = True
    trust_server_certificate: bool = False
    connect_timeout_s: int = 5
    driver: str = "ODBC Driver 18 for SQL Server"
    application_intent: str | None = None

    def __post_init__(self) -> None:
        if not self.driver:
            raise ValueError("driver must not be empty")
        if self.connect_timeout_s <= 0:
            raise ValueError("connect_timeout_s must be > 0")


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    """Saved connection profile — never contains a password (secret_ref only)."""

    name: str
    provider: str
    host: str
    port: int = 1433
    database: str | None = None
    auth: AuthMode = AuthMode.SQL
    username: str | None = None
    secret_ref: str | None = None
    options: ConnectionOptions = ConnectionOptions()
    # -- M8 safety --
    #: Which environment this profile points at; drives the header badge and the
    #: default read-only posture (:class:`Environment`).
    environment: Environment = Environment.DEVELOPMENT
    #: Per-profile read-only override. ``None`` means "follow the environment"
    #: (production → read-only). An explicit ``True`` forces read-only anywhere;
    #: an explicit ``False`` opts a production profile into writing, which the UI
    #: only ever offers as a deliberate, visible act.
    read_only: bool | None = None

    @property
    def read_only_effective(self) -> bool:
        """The read-only posture this profile opens with (M8 safety, S-9)."""
        if self.read_only is not None:
            return self.read_only
        return self.environment.read_only_by_default

    def __post_init__(self) -> None:
        validate_identifier(self.name, kind="profile name")
        if not self.provider:
            raise ValueError("provider must not be empty")
        if not self.host:
            raise ValueError("host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError(f"port must be in 1..65535, got {self.port}")
        if self.database is not None:
            validate_identifier(self.database, kind="database name")
        if self.auth is AuthMode.SQL and not self.username:
            raise ValueError("username is required for SQL authentication")
        if self.secret_ref is not None:
            validate_identifier(self.secret_ref, kind="secret reference")


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    """Outcome of a successful connection attempt."""

    server_version: str
    server_name: str
    database: str
    latency_ms: int
    auth_used: AuthMode

    def __post_init__(self) -> None:
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be >= 0")
        validate_identifier(self.database, kind="database name")
