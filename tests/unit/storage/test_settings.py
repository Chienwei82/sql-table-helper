"""User settings: defaults, validation, atomic write, theme round-trip (M3)."""

import os
import stat
from pathlib import Path

import pytest

from sql_table_swiss_knife.storage.settings import (
    DEFAULT_THEME,
    Settings,
    SettingsError,
    SettingsStore,
)


def test_absent_file_yields_defaults(tmp_path: Path) -> None:
    """First run is not an error: every setting has a documented default."""
    store = SettingsStore(tmp_path / "settings.toml")
    settings = store.load()
    assert settings.theme == DEFAULT_THEME
    assert settings.fetch_limit == 1000
    assert settings.delete_confirm_threshold == 50
    assert settings.last_profile is None


def test_save_load_round_trip_and_file_mode(tmp_path: Path) -> None:
    """The file is written atomically, 0600, and reloads identically."""
    store = SettingsStore(tmp_path / "settings.toml")
    saved = Settings(theme="high-contrast", fetch_limit=250, last_profile="catalog-prod")
    store.save(saved)
    assert store.load() == saved
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600
    assert not (tmp_path / "settings.toml.tmp").exists()


def test_unknown_keys_are_ignored_for_forward_compatibility(tmp_path: Path) -> None:
    """A file written by a newer version must not break an older one."""
    path = tmp_path / "settings.toml"
    path.write_text('theme = "light"\nfuture_option = 42\n', encoding="utf-8")
    settings = SettingsStore(path).load()
    assert settings.theme == "light"


def test_type_mismatch_is_reported_with_the_key_name(tmp_path: Path) -> None:
    """A malformed value names the setting instead of silently defaulting."""
    path = tmp_path / "settings.toml"
    path.write_text('fetch_limit = "many"\n', encoding="utf-8")
    with pytest.raises(SettingsError, match="fetch_limit"):
        SettingsStore(path).load()


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text("theme = \n", encoding="utf-8")
    with pytest.raises(SettingsError):
        SettingsStore(path).load()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"theme": "  "}, "theme"),
        ({"fetch_limit": 0}, "fetch_limit"),
        ({"delete_confirm_threshold": -1}, "delete_confirm_threshold"),
        ({"rowcount_advisory_above": -5}, "rowcount_advisory_above"),
    ],
)
def test_invalid_settings_are_rejected_at_construction(
    kwargs: dict[str, object], message: str
) -> None:
    """Bad values fail loudly when constructed, not when used later."""
    with pytest.raises(SettingsError, match=message):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_boolean_is_not_accepted_where_a_count_is_expected(tmp_path: Path) -> None:
    """``fetch_limit = true`` is a config mistake, not "one row"."""
    path = tmp_path / "settings.toml"
    path.write_text("fetch_limit = true\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="fetch_limit"):
        SettingsStore(path).load()


def test_with_helpers_return_copies(tmp_path: Path) -> None:
    """``with_*`` returns a new value; the original is untouched (frozen dataclass)."""
    store = SettingsStore(tmp_path / "settings.toml")
    original = Settings()
    switched = original.with_theme("light").with_last_profile("dev")
    assert original.theme == DEFAULT_THEME
    assert original.last_profile is None
    assert switched.theme == "light"
    assert switched.last_profile == "dev"
    assert store.path == tmp_path / "settings.toml"


def test_update_persists_the_change(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.toml")
    updated = store.update(Settings(), theme="high-contrast")
    assert updated.theme == "high-contrast"
    assert SettingsStore(store.path).load().theme == "high-contrast"
