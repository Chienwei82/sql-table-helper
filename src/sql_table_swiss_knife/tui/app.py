"""Textual application shell (DESIGN §9)."""

from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType

from .screens.home import HomeScreen

__all__ = ["SwissKnifeApp"]


class SwissKnifeApp(App[None]):
    """sql-table-swiss-knife application: screen stack, theming, global bindings."""

    TITLE = "sql-table-swiss-knife"
    CSS = """
    Screen {
        align: center middle;
    }
    #banner {
        width: auto;
        height: auto;
        color: $accent;
        text-style: bold;
    }
    #message {
        width: auto;
        height: auto;
        color: $text-muted;
        margin: 1 0;
    }
    #hints {
        width: auto;
        height: auto;
        background: $panel;
        padding: 0 2;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())
