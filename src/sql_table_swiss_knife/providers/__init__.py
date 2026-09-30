"""Provider registry — future DBMS are plugins registered here (DESIGN §5.3)."""

from collections.abc import Callable
from importlib import metadata
from typing import cast

from .base import (
    ActiveConnection,
    DatabaseProvider,
    ExecuteResult,
    ProviderCapabilities,
    StatementResult,
)
from .dialect import SqlDialect, SqlParam, SqlStatement
from .errors import (
    AuthError,
    ConnectError,
    MetadataError,
    ProviderError,
    QueryError,
    UnknownProviderError,
)
from .sqlgen import (
    SelectStatement,
    build_delete,
    build_insert,
    build_select,
    build_statements,
    build_update,
    sort_for_apply,
)

__all__ = [
    "BUILTIN_PROVIDERS",
    "ENTRY_POINT_GROUP",
    "ActiveConnection",
    "AuthError",
    "ConnectError",
    "DatabaseProvider",
    "ExecuteResult",
    "MetadataError",
    "ProviderCapabilities",
    "ProviderError",
    "QueryError",
    "SelectStatement",
    "SqlDialect",
    "SqlParam",
    "SqlStatement",
    "StatementResult",
    "UnknownProviderError",
    "available_providers",
    "build_delete",
    "build_insert",
    "build_select",
    "build_statements",
    "build_update",
    "get_provider",
    "load_entry_point_providers",
    "register_builtin_providers",
    "register_provider",
    "sort_for_apply",
]

#: Entry point group third-party packages use to ship provider plugins.
ENTRY_POINT_GROUP = "sql_table_swiss_knife.providers"

ProviderFactory = Callable[[], DatabaseProvider]


def _mssql_factory() -> DatabaseProvider:
    """Instantiate the built-in SQL Server provider (driver import stays lazy)."""
    from .mssql import MssqlProvider

    return MssqlProvider()


#: Providers shipped in this package, mapped to their factories.
BUILTIN_PROVIDERS: dict[str, ProviderFactory] = {"mssql": _mssql_factory}

_PROVIDERS: dict[str, ProviderFactory] = {}


def register_builtin_providers() -> tuple[str, ...]:
    """Register the providers that ship with the app (idempotent)."""
    for name, factory in BUILTIN_PROVIDERS.items():
        register_provider(name, factory, replace=True)
    return tuple(sorted(BUILTIN_PROVIDERS))


def register_provider(name: str, factory: ProviderFactory, *, replace: bool = False) -> None:
    """Register a provider factory under ``name`` (case-insensitive).

    Raises:
        ValueError: if the name is empty or already registered without ``replace``.
    """
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("provider name must not be empty")
    if normalized in _PROVIDERS and not replace:
        raise ValueError(f"provider {normalized!r} is already registered")
    _PROVIDERS[normalized] = factory


def get_provider(name: str) -> DatabaseProvider:
    """Instantiate the provider registered under ``name``.

    Raises:
        UnknownProviderError: if no provider is registered under that name.
    """
    normalized = name.strip().lower()
    factory = _PROVIDERS.get(normalized)
    if factory is None and normalized in BUILTIN_PROVIDERS:
        # Built-ins are registered lazily so the registry stays driver-free on import.
        register_provider(normalized, BUILTIN_PROVIDERS[normalized], replace=True)
        factory = _PROVIDERS[normalized]
    if factory is None:
        known = ", ".join(available_providers()) or "(none)"
        raise UnknownProviderError(f"unknown provider {normalized!r}; available: {known}")
    return factory()


def available_providers() -> tuple[str, ...]:
    """Names of all registered providers, sorted."""
    return tuple(sorted(_PROVIDERS))


def load_entry_point_providers() -> tuple[str, ...]:
    """Discover provider factories advertised under ``ENTRY_POINT_GROUP``.

    Each entry point must resolve to a ``ProviderFactory``. Collisions with existing
    registrations raise, surfacing packaging conflicts early.
    """
    loaded: list[str] = []
    for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
        factory = cast("ProviderFactory", entry_point.load())
        register_provider(entry_point.name, factory)
        loaded.append(entry_point.name)
    return tuple(loaded)
