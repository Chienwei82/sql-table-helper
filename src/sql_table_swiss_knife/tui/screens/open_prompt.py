"""Large-table prompt: offer a filter before opening a table that holds many rows (FR-2.5).

Opening a table is cheap, but *scanning* one is not: a table with twelve thousand rows is
the one a person cannot find anything in, and the filter is exactly the tool that makes it
navigable. So the browser asks once, before the first fetch, and the prompt is deliberately
a courtesy rather than a gate — "Open all rows" is always one keystroke away, because
sometimes you really do want the whole thing.

The decision that this prompt is warranted lives in
:func:`~services.catalog.needs_filter_prompt`, as a pure function; this screen only renders
it and returns the user's choice.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

__all__ = ["LargeTablePromptScreen"]

#: ``True`` → add a filter first; ``False`` → open the whole table; ``None`` → cancel.
OpenPromptResult = bool | None


class LargeTablePromptScreen(ModalScreen[OpenPromptResult]):
    """Ask whether to filter a large table before opening it."""

    DEFAULT_CSS = """
    LargeTablePromptScreen {
        align: center middle;
    }
    #open-prompt-box {
        width: 64;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #open-prompt-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #open-prompt-message {
        height: auto;
        margin-bottom: 1;
    }
    #open-prompt-buttons {
        height: auto;
        align-horizontal: right;
    }
    #open-prompt-buttons Button {
        margin-left: 2;
        min-width: 16;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f", "filter", "Filter first", show=True),
        Binding("a", "open_all", "Open all", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, table: str, row_count: int, *, threshold: int) -> None:
        super().__init__()
        self._table = table
        self._row_count = row_count
        self._threshold = threshold

    def compose(self) -> ComposeResult:
        with Vertical(id="open-prompt-box"):
            yield Static(f"Open {self._table}?", id="open-prompt-title")
            yield Static(self._message(), id="open-prompt-message")
            with Horizontal(id="open-prompt-buttons"):
                yield Button("Open all rows (a)", id="open-all")
                yield Button("Add a filter… (f)", id="filter", variant="primary")

    def on_mount(self) -> None:
        """Focus the filter button: filtering first is the recommended path."""
        self.query_one("#filter", Button).focus()

    def _message(self) -> str:
        """The prompt text, naming the size and the reason for asking."""
        return (
            f"This table has about {self._row_count:,} rows "
            f"(more than the {self._threshold:,}-row prompt).\n\n"
            "Adding a filter first loads only the rows you need. "
            "You can open every row instead, and filter later from the browser or the "
            "grid (f)."
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "filter")

    def action_filter(self) -> None:
        self.dismiss(True)

    def action_open_all(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(None)
