"""Home screen: title banner and key hints (M1 skeleton)."""

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Static

from ... import __version__

__all__ = ["BANNER", "HomeScreen"]

_BOX_WIDTH = 39


def _box_line(text: str) -> str:
    return "║" + text.center(_BOX_WIDTH) + "║"


BANNER = "\n".join(
    [
        "╔" + "═" * _BOX_WIDTH + "╗",
        _box_line("SQL TABLE SWISS KNIFE"),
        _box_line("edit catalog tables without SQL"),
        _box_line(f"v{__version__}"),
        "╚" + "═" * _BOX_WIDTH + "╝",
    ]
)


class HomeScreen(Screen[None]):
    """Empty home screen: banner plus key hints (M1)."""

    def compose(self) -> ComposeResult:
        yield Static(BANNER, id="banner")
        yield Static("Milestone 1 — skeleton home screen.", id="message")
        yield Static("Keys:  ctrl+q  Quit", id="hints")
        yield Footer()
