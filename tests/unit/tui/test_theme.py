"""Themes: three built-ins, a consistent semantic palette, persistence (FR-3.7)."""

from pathlib import Path

import pytest

from sql_table_swiss_knife.storage import Settings, SettingsError, SettingsStore
from sql_table_swiss_knife.tui.theme import (
    DEFAULT_THEME,
    SEMANTIC_ROLES,
    THEMES,
    apply_theme,
    cycle_theme,
    load_theme,
    next_theme,
    theme_names,
)


def test_at_least_three_themes_with_the_expected_names() -> None:
    """The spec asks for dark, light and a high-contrast option."""
    assert {"default-dark", "light", "high-contrast"} <= set(theme_names())
    assert len(theme_names()) >= 3


@pytest.mark.parametrize("name", theme_names())
def test_every_theme_defines_the_full_semantic_palette(name: str) -> None:
    """A screen names a *role*, never a colour; all three themes must supply it."""
    theme = THEMES[name]
    missing = [role for role in SEMANTIC_ROLES if role not in theme.variables]
    assert not missing, f"{name} is missing {missing}"


def test_every_theme_declares_its_lightness() -> None:
    """``dark`` decides how widgets pick contrast; getting it wrong reads badly."""
    assert THEMES["default-dark"].dark is True
    assert THEMES["light"].dark is False
    assert THEMES["high-contrast"].dark is True


def test_cycle_visits_every_theme_and_wraps_around() -> None:
    current = DEFAULT_THEME
    visited = [current]
    for _ in range(len(theme_names()) - 1):
        current = cycle_theme(current)
        visited.append(current)
    assert sorted(visited) == sorted(theme_names())
    assert next_theme(current) == DEFAULT_THEME  # wrapped


def test_next_theme_of_an_unknown_name_falls_back_to_the_default() -> None:
    assert next_theme("does-not-exist") == DEFAULT_THEME


def test_load_theme_prefers_the_argument_then_the_setting_then_the_default(
    tmp_path: Path,
) -> None:
    store = SettingsStore(tmp_path / "settings.toml")

    assert load_theme(store) == DEFAULT_THEME  # no file yet
    assert load_theme(store, "light") == "light"  # explicit wins
    assert load_theme(store, "nope") == DEFAULT_THEME  # unknown argument is ignored

    store.save(Settings(theme="high-contrast"))
    assert load_theme(store) == "high-contrast"


def test_load_theme_survives_a_broken_settings_file(tmp_path: Path) -> None:
    """A bad settings file must not stop the app from starting."""
    path = tmp_path / "settings.toml"
    path.write_text("theme = 42\n", encoding="utf-8")
    assert load_theme(SettingsStore(path)) == DEFAULT_THEME


def test_apply_theme_persists_the_choice(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.toml")
    updated = apply_theme(store, Settings(), "light")
    assert updated.theme == "light"
    assert SettingsStore(store.path).load().theme == "light"


def test_apply_theme_rejects_an_unknown_theme(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="unknown theme"):
        apply_theme(SettingsStore(tmp_path / "settings.toml"), Settings(), "neon")
