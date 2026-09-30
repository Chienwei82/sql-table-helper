"""Runtime theme switching and persistence, plus the ctrl+p command palette (M3)."""

from typing import cast

from textual.app import App
from textual.command import CommandPalette
from textual.widgets import OptionList

from sql_table_swiss_knife.storage import ProfileStore, Settings, SettingsStore
from sql_table_swiss_knife.tui.app import SwissKnifeApp
from sql_table_swiss_knife.tui.screens import ConnectionsScreen, TableBrowserScreen
from sql_table_swiss_knife.tui.theme import DEFAULT_THEME, SEMANTIC_ROLES, theme_names
from tests.tui.conftest import AppFactory, active_screen


def _settings(app: App[None]) -> SettingsStore:
    """The settings store of a running app (an attribute only our app class has)."""
    return cast("SwissKnifeApp", app).settings_store


async def test_all_themes_are_registered_at_startup(app_factory: AppFactory) -> None:
    app = app_factory()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        assert set(app.available_themes) >= set(theme_names())
        assert app.theme == DEFAULT_THEME


async def test_ctrl_t_cycles_through_every_theme(app_factory: AppFactory) -> None:
    """FR-3.7: the theme can be changed without leaving the screen."""
    app = app_factory()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        seen = [app.theme]
        for _ in range(len(theme_names())):
            await pilot.press("ctrl+t")
            await pilot.pause()
            seen.append(app.theme)
        assert set(seen) == set(theme_names())


async def test_the_chosen_theme_is_persisted(app_factory: AppFactory) -> None:
    """A restart must come back with the same colours."""
    app = app_factory()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause()
        chosen = app.theme

    assert _settings(app).load().theme == chosen

    # A fresh app reading the same settings file starts on the same theme.
    again = app_factory()
    async with again.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        assert again.theme == chosen


async def test_startup_honours_a_persisted_theme(app_factory: AppFactory) -> None:
    app = app_factory()
    _settings(app).save(Settings(theme="high-contrast"))
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        assert app.theme == "high-contrast"


async def test_semantic_roles_reach_the_widgets_in_every_theme(app_factory: AppFactory) -> None:
    """Every theme exposes the whole palette to the running app (FR-3.7)."""
    app = app_factory()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        for name in theme_names():
            app.set_theme(name)
            await pilot.pause()
            missing = [role for role in SEMANTIC_ROLES if role not in app.theme_variables]
            assert not missing, f"{name} is missing {missing}"


async def test_unknown_theme_is_refused_with_a_warning(app_factory: AppFactory) -> None:
    app = app_factory()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.set_theme("neon-disco")
        await pilot.pause()
        assert app.theme == DEFAULT_THEME  # unchanged


async def test_command_palette_lists_the_screen_actions(app_factory, seeded_profiles) -> None:  # type: ignore[no-untyped-def]
    """Ctrl+P fuzzy-searches the actions of whatever screen is active."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, CommandPalette)

        await pilot.press(*"duplicate")
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        texts = [str(option.prompt).lower() for option in options.options]
        assert any("duplicate" in text for text in texts)


async def test_command_palette_offers_the_theme_switches(app_factory: AppFactory) -> None:
    app = app_factory()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press(*"high-contrast")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == "high-contrast"


async def test_command_palette_offers_the_current_screen_actions(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """The palette shows what the *active* screen can do, not a global list."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        connections_actions = {
            action.name for action in active_screen(app, ConnectionsScreen).command_actions()
        }
        assert "Connect" in connections_actions
        assert "Open" not in connections_actions

        await app.connection.connect(seeded_profiles.get("catalog"))
        app.push_screen(TableBrowserScreen())
        for _ in range(4):
            await pilot.pause()
        browser_actions = {
            action.name for action in active_screen(app, TableBrowserScreen).command_actions()
        }
        assert "Open" in browser_actions  # the picker's own action
        assert "Search" in browser_actions
