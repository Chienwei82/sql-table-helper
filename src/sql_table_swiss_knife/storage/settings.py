"""User settings (``settings.toml``) — preferences only, never secrets (DESIGN §13.2).

The file is a flat TOML table. Unknown keys are ignored so that a newer version's
settings never break an older build, while known keys are type-checked on load.
Writes are atomic and the file is chmod 0600 like the profile store.
"""

import os
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

import tomli_w

from ..infra.errors import AppError
from .paths import settings_path

__all__ = [
    "COPY_FORMATS",
    "DATE_FORMATS",
    "NUMBER_LOCALES",
    "Settings",
    "SettingsError",
    "SettingsStore",
]

#: Theme used when the file is absent or names a theme that is not registered.
DEFAULT_THEME = "default-dark"

#: Accepted ``copy_format`` values (FR-4.2).
COPY_FORMATS = frozenset({"tsv", "csv", "json"})

#: Accepted ``paste_number_locale`` values: ``en`` and ``de`` decimal conventions.
NUMBER_LOCALES = frozenset({"en", "de"})

#: Accepted ``paste_date_format`` values (day-first, month-first, ISO).
DATE_FORMATS = frozenset({"iso", "dmy", "mdy"})


class SettingsError(AppError):
    """Settings file is missing, malformed, or holds an unusable value."""


@dataclass(frozen=True, slots=True)
class Settings:
    """User preferences (DESIGN §13.2).

    Values are validated on construction so a bad file fails loudly instead of
    producing a half-configured session.
    """

    theme: str = DEFAULT_THEME
    fetch_limit: int = 1000
    copy_null_repr: str = ""
    delete_confirm_threshold: int = 50
    clipboard_read_fallback: bool = False
    rowcount_advisory_above: int = 50000
    last_profile: str | None = None
    # -- M7: clipboard (DESIGN §13.2) --
    #: Format the data-copy action uses: ``tsv`` (Excel/Sheets), ``csv`` or ``json``.
    copy_format: str = "tsv"
    #: Text that means SQL NULL on paste (FR-4.6). Empty disables the token entirely.
    paste_null_token: str = "NULL"
    #: When True the null token is pasted as ordinary text instead of NULL.
    paste_null_as_literal: bool = False
    #: How pasted numbers are written: ``en`` (1,234.56) or ``de`` (1.234,56).
    paste_number_locale: str = "en"
    #: How pasted dates are written: ``iso`` (2026-01-31), ``dmy`` (31.01.2026) or
    #: ``mdy`` (01/31/2026).
    paste_date_format: str = "iso"
    #: Upper bound on pasted rows; a bigger block is refused with a message rather
    #: than staging thousands of rows by accident.
    paste_max_rows: int = 5000

    def __post_init__(self) -> None:
        if not self.theme.strip():
            raise SettingsError("theme must not be empty")
        if self.fetch_limit <= 0:
            raise SettingsError(f"fetch_limit must be > 0, got {self.fetch_limit}")
        if self.delete_confirm_threshold < 0:
            raise SettingsError(
                f"delete_confirm_threshold must be >= 0, got {self.delete_confirm_threshold}"
            )
        if self.rowcount_advisory_above < 0:
            raise SettingsError(
                f"rowcount_advisory_above must be >= 0, got {self.rowcount_advisory_above}"
            )
        if self.copy_format not in COPY_FORMATS:
            raise SettingsError(
                f"copy_format must be one of {sorted(COPY_FORMATS)}, got {self.copy_format!r}"
            )
        if self.paste_number_locale not in NUMBER_LOCALES:
            raise SettingsError(
                f"paste_number_locale must be one of {sorted(NUMBER_LOCALES)}, "
                f"got {self.paste_number_locale!r}"
            )
        if self.paste_date_format not in DATE_FORMATS:
            raise SettingsError(
                f"paste_date_format must be one of {sorted(DATE_FORMATS)}, "
                f"got {self.paste_date_format!r}"
            )
        if self.paste_max_rows < 1:
            raise SettingsError(f"paste_max_rows must be >= 1, got {self.paste_max_rows}")

    def with_theme(self, theme: str) -> Settings:
        """Return a copy using a different theme (the way the app persists a switch)."""
        return replace(self, theme=theme)

    def with_last_profile(self, name: str | None) -> Settings:
        """Return a copy remembering the profile of the last successful session."""
        return replace(self, last_profile=name)


def _coerce(name: str, raw: Any, default: Any) -> Any:
    """Coerce one TOML value to the type of the matching dataclass field."""
    expected = type(default)
    if raw is None:
        return default
    if expected is bool:
        if not isinstance(raw, bool):
            raise SettingsError(f"{name}: expected a boolean, got {raw!r}")
        return raw
    if expected is int:
        # bool is an int subclass; a boolean here is a config mistake, not a count.
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SettingsError(f"{name}: expected an integer, got {raw!r}")
        return raw
    if expected is str:
        if not isinstance(raw, str):
            raise SettingsError(f"{name}: expected a string, got {raw!r}")
        return raw
    # ``last_profile`` is the one optional field; its default is None, so the type is
    # taken from the annotation rather than from the default's type.
    if expected is type(None) or name == "last_profile":
        if raw is not None and not isinstance(raw, str):
            raise SettingsError(f"{name}: expected a string or nothing, got {raw!r}")
        return raw
    raise SettingsError(f"{name}: unsupported setting type {expected!r}")


class SettingsStore:
    """Load/save :class:`Settings` from ``settings.toml`` (atomic write, mode 0600).

    An absent file means "all defaults" — first run is not an error. A malformed
    file raises :class:`SettingsError` so the user can see what is wrong instead of
    silently losing preferences.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else settings_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Settings:
        """Read the settings file, falling back to defaults when it does not exist."""
        defaults = Settings()
        if not self._path.exists():
            return defaults
        try:
            raw = tomllib.loads(self._path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
            raise SettingsError(f"cannot read {self._path}: {exc}") from exc
        known = {field.name for field in fields(Settings)}
        values: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in known:
                continue  # forward compatibility: ignore keys from newer versions
            values[key] = _coerce(key, value, getattr(defaults, key))
        try:
            return Settings(**values)
        except (SettingsError, ValueError) as exc:
            raise SettingsError(f"{self._path}: {exc}") from exc

    def save(self, settings: Settings) -> None:
        """Persist the settings atomically; the file is chmod 0600."""
        document: dict[str, Any] = {}
        for field_info in fields(Settings):
            value = getattr(settings, field_info.name)
            if value is not None:
                document[field_info.name] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(tomli_w.dumps(document), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def update(self, settings: Settings, **changes: Any) -> Settings:
        """Apply keyword changes to ``settings`` and persist the result."""
        updated = replace(settings, **changes)
        self.save(updated)
        return updated
