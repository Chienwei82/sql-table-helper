"""Connection profile persistence: a TOML file that never holds secrets (DESIGN §8.3).

Passwords are stored exclusively in the OS keyring (or prompted per session); a
``password``/``pwd`` key in the file is rejected loudly at load time.
"""

import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from ..domain.connection import (
    AuthMode,
    ConnectionOptions,
    ConnectionProfile,
    Environment,
)
from ..infra.errors import AppError
from .paths import atomic_write, profiles_path

__all__ = ["ProfileError", "ProfileStore"]

#: Keys that must never appear in the profiles file (defense in depth).
FORBIDDEN_KEYS = frozenset({"password", "pwd", "passwd", "secret"})

#: Top-level keys accepted in profiles.toml.
_TOP_LEVEL_KEYS = frozenset({"profile"})

_PROFILE_KEYS = frozenset(
    {
        "name",
        "provider",
        "host",
        "port",
        "database",
        "auth",
        "username",
        "secret_ref",
        "environment",
        "read_only",
        "options",
    }
)

_OPTION_KEYS = frozenset(
    {"encrypt", "trust_server_certificate", "connect_timeout_s", "driver", "application_intent"}
)


class ProfileError(AppError):
    """Profile file is missing, malformed, or violates the no-secrets rule."""


def _check_forbidden(context: str, keys: Any) -> None:
    offenders = FORBIDDEN_KEYS & {str(key).lower() for key in keys}
    if offenders:
        raise ProfileError(
            f"{context} contains secret key(s) {sorted(offenders)} — passwords never "
            "belong in profiles.toml; they are stored in the OS keyring instead"
        )


def _check_unknown(context: str, keys: Any, allowed: frozenset[str]) -> None:
    unknown = {str(key) for key in keys} - allowed
    if unknown:
        raise ProfileError(f"{context} has unknown key(s): {', '.join(sorted(unknown))}")


def _profile_to_table(profile: ConnectionProfile) -> dict[str, Any]:
    table: dict[str, Any] = {
        "name": profile.name,
        "provider": profile.provider,
        "host": profile.host,
        "port": profile.port,
        "auth": profile.auth.value,
    }
    if profile.database is not None:
        table["database"] = profile.database
    if profile.username is not None:
        table["username"] = profile.username
    if profile.secret_ref is not None:
        table["secret_ref"] = profile.secret_ref
    # ``environment`` is only written when it is not the default, so a file written by
    # this version stays as small as the previous ones; ``read_only`` is written
    # whenever it is set, because ``false`` is a deliberate opt-out and must survive.
    if profile.environment is not Environment.DEVELOPMENT:
        table["environment"] = profile.environment.value
    if profile.read_only is not None:
        table["read_only"] = profile.read_only
    table["options"] = _options_to_table(profile.options)
    return table


def _options_to_table(options: ConnectionOptions) -> dict[str, Any]:
    table: dict[str, Any] = {
        "encrypt": options.encrypt,
        "trust_server_certificate": options.trust_server_certificate,
        "connect_timeout_s": options.connect_timeout_s,
        "driver": options.driver,
    }
    if options.application_intent is not None:
        table["application_intent"] = options.application_intent
    return table


def _require_str(table: dict[str, Any], key: str, context: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ProfileError(f"{context}: {key!r} must be a non-empty string")
    return value


def _profile_from_table(table: Any, *, index: int) -> ConnectionProfile:
    if not isinstance(table, dict):
        raise ProfileError(f"profile #{index + 1} must be a table")
    context = f"profile #{index + 1}"
    _check_forbidden(context, table)
    _check_unknown(context, table, _PROFILE_KEYS)

    name = _require_str(table, "name", context)
    context = f"profile {name!r}"
    provider = _require_str(table, "provider", context)
    host = _require_str(table, "host", context)

    port = table.get("port", 1433)
    if not isinstance(port, int) or isinstance(port, bool):
        raise ProfileError(f"{context}: 'port' must be an integer")

    auth_raw = table.get("auth", AuthMode.SQL.value)
    try:
        auth = AuthMode(str(auth_raw))
    except ValueError as exc:
        known = ", ".join(mode.value for mode in AuthMode)
        raise ProfileError(f"{context}: 'auth' must be one of {known}, got {auth_raw!r}") from exc

    database = table.get("database")
    if database is not None and not isinstance(database, str):
        raise ProfileError(f"{context}: 'database' must be a string")

    environment_raw = table.get("environment", Environment.DEVELOPMENT.value)
    try:
        environment = Environment.parse(str(environment_raw))
    except ValueError as exc:
        raise ProfileError(f"{context}: {exc}") from exc

    read_only = table.get("read_only")
    if read_only is not None and not isinstance(read_only, bool):
        raise ProfileError(f"{context}: 'read_only' must be true or false")
    username = table.get("username")
    if username is not None and not isinstance(username, str):
        raise ProfileError(f"{context}: 'username' must be a string")
    secret_ref = table.get("secret_ref")
    if secret_ref is not None and not isinstance(secret_ref, str):
        raise ProfileError(f"{context}: 'secret_ref' must be a string")

    options_raw = table.get("options", {})
    if not isinstance(options_raw, dict):
        raise ProfileError(f"{context}: 'options' must be a table")
    _check_unknown(f"{context} options", options_raw, _OPTION_KEYS)
    options = _options_from_table(options_raw, context=context)

    try:
        return ConnectionProfile(
            name=name,
            provider=provider,
            host=host,
            port=port,
            database=database,
            auth=auth,
            username=username,
            secret_ref=secret_ref,
            options=options,
            environment=environment,
            read_only=read_only,
        )
    except ValueError as exc:
        raise ProfileError(f"{context}: {exc}") from exc


def _options_from_table(table: dict[str, Any], *, context: str) -> ConnectionOptions:
    defaults = ConnectionOptions()

    def _bool(key: str, default: bool) -> bool:
        value = table.get(key, default)
        if not isinstance(value, bool):
            raise ProfileError(f"{context}: options.{key} must be true/false")
        return value

    timeout = table.get("connect_timeout_s", defaults.connect_timeout_s)
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise ProfileError(f"{context}: options.connect_timeout_s must be an integer")
    driver = table.get("driver", defaults.driver)
    if not isinstance(driver, str):
        raise ProfileError(f"{context}: options.driver must be a string")
    intent = table.get("application_intent")
    if intent is not None and not isinstance(intent, str):
        raise ProfileError(f"{context}: options.application_intent must be a string")

    try:
        return ConnectionOptions(
            encrypt=_bool("encrypt", defaults.encrypt),
            trust_server_certificate=_bool(
                "trust_server_certificate", defaults.trust_server_certificate
            ),
            connect_timeout_s=timeout,
            driver=driver,
            application_intent=intent,
        )
    except ValueError as exc:
        raise ProfileError(f"{context}: {exc}") from exc


class ProfileStore:
    """Load/save named connection profiles in a TOML file (atomic write, mode 0600)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else profiles_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> tuple[ConnectionProfile, ...]:
        """Return all stored profiles; an absent file means "no profiles yet"."""
        if not self._path.exists():
            return ()
        try:
            raw = tomllib.loads(self._path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
            raise ProfileError(f"cannot read {self._path}: {exc}") from exc
        _check_forbidden(str(self._path), raw)
        _check_unknown(str(self._path), raw, _TOP_LEVEL_KEYS)

        entries = raw.get("profile", [])
        if not isinstance(entries, list):
            raise ProfileError(f"{self._path}: 'profile' must be an array of tables")

        profiles = tuple(_profile_from_table(entry, index=i) for i, entry in enumerate(entries))
        seen: set[str] = set()
        for profile in profiles:
            if profile.name in seen:
                raise ProfileError(f"{self._path}: duplicate profile name {profile.name!r}")
            seen.add(profile.name)
        return profiles

    def save_all(self, profiles: tuple[ConnectionProfile, ...]) -> None:
        """Persist the full profile set (atomic replace; file is chmod 0600)."""
        names = [profile.name for profile in profiles]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ProfileError(f"duplicate profile name(s): {sorted(duplicates)}")
        document = {"profile": [_profile_to_table(profile) for profile in profiles]}
        atomic_write(self._path, tomli_w.dumps(document))

    def get(self, name: str) -> ConnectionProfile:
        """Look up one profile by exact name.

        Raises:
            ProfileError: if no profile with that name exists (message lists alternatives).
        """
        profiles = self.load()
        for profile in profiles:
            if profile.name == name:
                return profile
        known = ", ".join(sorted(profile.name for profile in profiles)) or "(none)"
        raise ProfileError(f"unknown profile {name!r}; available: {known}")

    def upsert(self, profile: ConnectionProfile) -> None:
        """Insert or replace the profile with the same name (sorted by name)."""
        profiles = [p for p in self.load() if p.name != profile.name]
        profiles.append(profile)
        profiles.sort(key=lambda p: p.name)
        self.save_all(tuple(profiles))

    def delete(self, name: str) -> None:
        """Remove the named profile.

        Raises:
            ProfileError: if it does not exist.
        """
        profiles = self.load()
        remaining = tuple(p for p in profiles if p.name != name)
        if len(remaining) == len(profiles):
            raise ProfileError(f"unknown profile {name!r}")
        self.save_all(remaining)
