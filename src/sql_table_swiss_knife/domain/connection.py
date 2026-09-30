"""Connection profile domain models."""

from dataclasses import dataclass
from enum import Enum

from .identifiers import validate_identifier

__all__ = ["AuthMode", "ConnectionOptions", "ConnectionProfile", "ConnectionResult"]


class AuthMode(Enum):
    """Authentication mode for a connection profile."""

    SQL = "sql"  # username + password (secret via SecretStore)
    INTEGRATED = "integrated"  # Windows integrated auth; no secret


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
