"""The cell expand view: long text and binary cells in full (FR-3.1, M8).

A grid column is sized for the majority of its rows, which makes a column holding a
paragraph unreadable — and a column holding a ``varbinary(64)`` not merely unreadable
but unrepresentable as text at all. This screen is what ``expand`` opens for those
cells, and the choice of what to show is made by
:mod:`sql_table_swiss_knife.services.cellview`, not here:

* **text** — the complete value, hard-wrapped, in a scrollable view;
* **binary** — a hex dump with an ASCII column, because that is the only form of a
  rowversion or a UUID anybody can actually verify.

It is deliberately read-only and deliberately *not* an editor: expanding a cell is a
reading action. Editing goes through the cell editor, which validates and stages, so a
view that also allowed typing would create a second, unvalidated path into the
staging area.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea

from ...services.cellview import CellView, wrap_text

__all__ = ["CellViewScreen"]


class CellViewScreen(ModalScreen[None]):
    """Show one cell's full value, read-only, with a size/type header."""

    DEFAULT_CSS = """
    CellViewScreen {
        align: center middle;
    }
    #cellview-box {
        width: 80%;
        max-width: 100;
        height: 70%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #cellview-title {
        text-style: bold;
    }
    #cellview-meta {
        color: $text-muted;
        margin-bottom: 1;
    }
    #cellview-body {
        height: 1fr;
        border: round $panel;
    }
    #cellview-hint {
        color: $text-muted;
        height: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "close", "Close", show=True),
    ]

    #: Width the text is hard-wrapped to. Fixed rather than derived from the terminal so
    #: the wrapped text is stable while the user resizes, and so it matches the unit test.
    WRAP_WIDTH = 80

    def __init__(self, value: object, *, title: str = "", view: CellView | None = None) -> None:
        super().__init__()
        self._value = value
        self._view = view
        self._title = title

    def compose(self) -> ComposeResult:
        view = self._view
        if view is None:
            from ...services.cellview import cell_view  # local: keeps the import graph flat

            view = cell_view(self._value, width=self.WRAP_WIDTH)
        with Vertical(id="cellview-box"):
            yield Static(self._title or "Cell value", id="cellview-title")
            yield Static(view.summary, id="cellview-meta")
            body = "\n".join(self.lines_for(view))
            yield TextArea(body, read_only=True, show_line_numbers=False, id="cellview-body")
            yield Static(
                "esc to close · this view is read-only; press enter on the grid to edit",
                id="cellview-hint",
            )

    def action_close(self) -> None:
        self.dismiss(None)

    @staticmethod
    def lines_for(view: CellView) -> list[str]:
        """The wrapped lines a long text value is shown as.

        Exposed as a static method so the wrapping is testable without mounting the
        screen, and so the screen and its test cannot disagree about the layout.
        """
        if view.kind.value == "binary":
            return view.full.split("\n")
        return wrap_text(view.full, CellViewScreen.WRAP_WIDTH)
