"""Provider-level error hierarchy (DESIGN §5.2)."""

from ..infra.errors import AppError
from .dialect import SqlStatement

__all__ = [
    "AuthError",
    "ConnectError",
    "MetadataError",
    "ProviderError",
    "QueryError",
    "UnknownProviderError",
]


class ProviderError(AppError):
    """Base class for database provider failures."""


class ConnectError(ProviderError):
    """Network / driver / server unreachable."""


class AuthError(ConnectError):
    """Login failed — the UI re-prompts instead of dumping driver messages."""


class MetadataError(ProviderError):
    """Catalog introspection failed (missing object, permissions…)."""


class QueryError(ProviderError):
    """Runtime SQL error with optional driver diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        sqlstate: str | None = None,
        vendor_code: int | None = None,
        statement: SqlStatement | None = None,
    ) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.vendor_code = vendor_code
        self.statement = statement


class UnknownProviderError(AppError):
    """Requested provider name is not registered."""
