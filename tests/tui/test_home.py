"""End-to-end (Pilot) tests for the M1 app shell and home screen."""

from textual.widgets import Footer, Static

from sql_table_swiss_knife.tui.app import SwissKnifeApp
from sql_table_swiss_knife.tui.screens.home import BANNER, HomeScreen


async def test_home_screen_shows_banner_and_hints() -> None:
    app = SwissKnifeApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()  # let on_mount push + compose HomeScreen
        assert isinstance(app.screen, HomeScreen)
        assert app._running  # app started

        # App.query_one walks the bottom of the screen stack — query the active screen.
        banner = str(app.screen.query_one("#banner", Static).render())
        assert "SQL TABLE SWISS KNIFE" in banner
        assert "edit catalog tables without SQL" in banner
        assert "SQL TABLE SWISS KNIFE" in BANNER

        message = str(app.screen.query_one("#message", Static).render())
        assert "Milestone 1" in message

        hints = str(app.screen.query_one("#hints", Static).render())
        assert "ctrl+q" in hints
        assert "Quit" in hints

        assert app.screen.query_one(Footer) is not None
        await pilot.pause()


async def test_ctrl_q_quits() -> None:
    app = SwissKnifeApp()
    async with app.run_test(size=(100, 30)) as pilot:
        assert app._running
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert not app._running
