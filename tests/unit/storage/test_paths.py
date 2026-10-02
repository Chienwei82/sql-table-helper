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
    APP_NAME,
    KEYBINDINGS_ENV,
    LEGACY_APP_NAME,
    atomic_write,
    audit_log_path,
    config_dir,
    keybindings_path,
    legacy_config_dir,
    migrate_legacy_config,
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


# -- the rename: config migration (sql-table-swiss-knife → sql-table-manager) ---------


def test_the_app_name_is_the_new_one() -> None:
    """The name drives the config dir *and* the OS-keyring service; pin both facts."""
    assert APP_NAME == "sql-table-manager"
    assert LEGACY_APP_NAME == "sql-table-swiss-knife"


def test_legacy_config_dir_is_none_when_the_dir_is_pinned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the env override, both names point at one place, so there is nothing to move."""
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path))
    assert legacy_config_dir() is None


def test_legacy_config_dir_uses_the_old_app_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.CONFIG_DIR_ENV, raising=False)
    legacy = legacy_config_dir()
    assert legacy is not None and LEGACY_APP_NAME in legacy.parts


def test_migration_copies_a_legacy_directory_into_the_new_one(tmp_path: Path) -> None:
    """A pre-rename install keeps its profiles, settings and audit log."""
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    (old / "profiles.toml").write_text("[[profiles]]\n", encoding="utf-8")
    (old / "settings.toml").write_text('theme = "halloween"\n', encoding="utf-8")

    result = migrate_legacy_config(legacy=old, target=new)

    assert result == new
    assert (new / "profiles.toml").read_text(encoding="utf-8") == "[[profiles]]\n"
    assert (new / "settings.toml").exists()
    # Never a move: a user who downgrades still finds the old directory intact.
    assert (old / "profiles.toml").exists()


def test_migration_only_fills_gaps_and_never_overwrites(tmp_path: Path) -> None:
    """A file already present under the new name wins; a missing one is carried over."""
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "profiles.toml").write_text("legacy", encoding="utf-8")
    (old / "settings.toml").write_text("from-legacy", encoding="utf-8")
    (new / "profiles.toml").write_text("current", encoding="utf-8")

    # profiles.toml already exists under the new name, so the migration is a no-op.
    assert migrate_legacy_config(legacy=old, target=new) is None
    assert (new / "profiles.toml").read_text(encoding="utf-8") == "current"
    assert not (new / "settings.toml").exists()


def test_migration_is_a_noop_without_a_legacy_directory(tmp_path: Path) -> None:
    assert migrate_legacy_config(legacy=tmp_path / "missing", target=tmp_path / "new") is None


def test_migration_is_a_noop_when_source_and_target_are_the_same(tmp_path: Path) -> None:
    (tmp_path / "profiles.toml").write_text("x", encoding="utf-8")
    assert migrate_legacy_config(legacy=tmp_path, target=tmp_path) is None
