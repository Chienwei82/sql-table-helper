"""Config file locations (DESIGN §8.3, M8).

The paths themselves are boring, which is exactly why they need pinning: a keymap that
resolves to a different directory than ``profiles.toml`` is a bug nobody notices until
their custom keys silently do nothing.
"""

from pathlib import Path

import pytest

from sql_table_swiss_knife.storage import paths
from sql_table_swiss_knife.storage.paths import (
    KEYBINDINGS_ENV,
    audit_log_path,
    config_dir,
    keybindings_path,
    profiles_path,
    settings_path,
)


def test_every_file_lives_in_the_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path))
    assert config_dir() == tmp_path
    assert profiles_path() == tmp_path / "profiles.toml"
    assert settings_path() == tmp_path / "settings.toml"
    assert audit_log_path() == tmp_path / "audit.log.jsonl"
    assert keybindings_path() == tmp_path / "keybindings.toml"


def test_the_keybindings_override_names_a_file_not_a_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A team shares one keymap file; a second config *directory* would be less useful."""
    shared = tmp_path / "team" / "keys.toml"
    monkeypatch.setenv(KEYBINDINGS_ENV, str(shared))
    assert keybindings_path() == shared


def test_the_keybindings_override_is_expanded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``~`` in an env var is a typo waiting to happen, not a literal directory name."""
    monkeypatch.setenv(KEYBINDINGS_ENV, "~/keys.toml")
    assert keybindings_path() == Path.home() / "keys.toml"


def test_the_config_dir_override_is_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, "~/cfg")
    assert config_dir() == Path.home() / "cfg"
