"""Config file locations (DESIGN §8.3, M8).

The paths themselves are boring, which is exactly why they need pinning: a keymap that
resolves to a different directory than ``profiles.toml`` is a bug nobody notices until
their custom keys silently do nothing.
"""

import os
from pathlib import Path

import pytest

from sql_table_swiss_knife.storage import paths
from sql_table_swiss_knife.storage.paths import (
    KEYBINDINGS_ENV,
    atomic_write,
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


def test_atomic_write_leaves_no_partial_file_and_no_leftovers(tmp_path: Path) -> None:
    target = tmp_path / "profiles.toml"
    atomic_write(target, "first")
    atomic_write(target, "second")
    assert target.read_text(encoding="utf-8") == "second"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["profiles.toml"]


def test_atomic_write_cleans_up_when_the_rename_cannot_happen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed save must not leave a stray file next to the real config."""
    target = tmp_path / "settings.toml"

    def boom(src: object, dst: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, "content")
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_uses_a_temporary_name_unique_to_the_writer(tmp_path: Path) -> None:
    """Two instances of the app must not share one scratch file.

    The old code wrote to a fixed ``<name>.tmp`` sibling, so a second writer could read
    back the first writer's half-written content and then rename that file away, leaving
    the first to fail with ``FileNotFoundError`` on its own ``os.replace``. Pinning the
    name shape keeps the per-writer uniqueness from being quietly reverted.
    """
    target = tmp_path / "profiles.toml"
    seen: list[Path] = []
    real_replace = os.replace

    def recording_replace(src: object, dst: object) -> None:
        seen.append(Path(str(src)))
        real_replace(src, dst)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "replace", recording_replace)
        atomic_write(target, "a")
        atomic_write(target, "b")

    assert len(seen) == 2
    assert len({p.name for p in seen}) == 2, f"the scratch name was reused: {seen}"
    for scratch in seen:
        assert scratch.parent == tmp_path, "the scratch file must share the target's directory"
        assert not scratch.exists(), "the scratch file outlived the write"
