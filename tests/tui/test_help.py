"""The F1 help screen: every binding, grouped, plus the safety summary (NFR-6).

The content is generated from the keybinding registry, so these tests assert on what a
reader can find rather than on the registry's shape — "is the Apply key in there?" is
the question the screen exists to answer.
"""

from textual.app import App
from textual.widgets import Static

from sql_table_swiss_knife.storage import ProfileStore
from sql_table_swiss_knife.tui.keymap import Keymap
from sql_table_swiss_knife.tui.screens import HelpScreen
from tests.fakes import FakeProvider
from tests.tui.conftest import AppFactory, active_screen


def _texts(app: App[None]) -> list[str]:
    """Every rendered string on the help screen, as a reader would scroll them."""
    return [str(widget.render()) for widget in app.screen.query(Static)]


async def test_f1_opens_the_help_screen(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        await pilot.press("f1")
        for _ in range(3):
            await pilot.pause()
        assert isinstance(active_screen(app, HelpScreen), HelpScreen)


async def test_the_help_lists_every_documented_binding(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """A binding the reader cannot find here is a binding nobody will use."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        app.push_screen(HelpScreen())
        for _ in range(3):
            await pilot.pause()
        body = "\n".join(_texts(app))
        for expected in ("ctrl+s", "f2", "f3", "ctrl+c", "ctrl+v", "enter", "escape", "f1"):
            assert expected in body


async def test_the_help_is_grouped_by_intent(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        app.push_screen(HelpScreen())
        for _ in range(3):
            await pilot.pause()
        body = "\n".join(_texts(app))
        assert "Safety" in body
        assert "Editing & staging" in body
        # The safety group comes before the view group: intent first, chrome later.
        assert body.index("Safety") < body.index("Connections")


async def test_the_help_states_the_safety_rules(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """The rules that make the app safe against production belong here, not in a manual."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        app.push_screen(HelpScreen())
        for _ in range(3):
            await pilot.pause()
        body = "\n".join(_texts(app)).lower()
        assert "one transaction" in body
        assert "read-only" in body
        assert "audit" in body
        assert "password" in body


async def test_the_help_shows_a_user_override(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """An override is marked as such, next to the default it replaces."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        app.push_screen(HelpScreen(Keymap({"apply": "ctrl+g"})))
        for _ in range(3):
            await pilot.pause()
        body = "\n".join(_texts(app))
        assert "ctrl+g" in body
        assert "default: ctrl+s" in body


async def test_escape_closes_the_help(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        app.push_screen(HelpScreen())
        for _ in range(3):
            await pilot.pause()
        await pilot.press("escape")
        for _ in range(3):
            await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)
