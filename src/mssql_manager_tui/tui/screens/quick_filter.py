"""Quick-filter modal: filter one column, server-side (FR-3.3).

"Column contains X" and "column equals X" are different SQL predicates, and the user picks
which one: guessing wrong silently returns the wrong rows, which is worse than one extra
keypress. The screen returns a :class:`~services.view.QuickFilter`, and the *server* does
the filtering — the rows that were never loaded can still match (S-8).
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, RadioButton, RadioSet, Static

from ...domain.catalog import Table
from ...services.view import FilterMode, QuickFilter

__all__ = ["QuickFilterScreen"]


class QuickFilterScreen(ModalScreen[QuickFilter | None]):
    """Choose a column, a match mode and a term; dismisses with the filter, or ``None``."""

    DEFAULT_CSS = """
    QuickFilterScreen {
        align: center middle;
    }
    #filter-box {
        width: 64;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #filter-title {
        text-style: bold;
    }
    #filter-column {
        height: auto;
    }
    RadioButton {
        height: 1;
    }
    #filter-footer {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "apply", "Apply", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, table: Table, column: str, term: str = "", mode: FilterMode | None = None):
        super().__init__()
        self._table = table
        self._column = column
        self._term = term
        self._mode = mode if mode is not None else FilterMode.CONTAINS

    def compose(self) -> ComposeResult:
        with Vertical(id="filter-box"):
            yield Static(f"Filter {self._table.ref}", id="filter-title")
            yield Input(
                value=self._term,
                placeholder="value to match",
                id="filter-term",
            )
            with RadioSet(id="filter-mode"):
                for mode in FilterMode:
                    yield RadioButton(mode.label, id=f"mode-{mode.value}")
            yield Input(
                value=self._column,
                placeholder="column name",
                id="filter-column",
            )
            yield Static("enter apply · esc cancel", id="filter-footer")

    def on_mount(self) -> None:
        self.query_one(f"#mode-{self._mode.value}", RadioButton).value = True
        self.query_one("#filter-term", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the term field applies the filter (``Input`` eats the key)."""
        if event.input.id == "filter-term":
            self.action_apply()

    def action_apply(self) -> None:
        """Return the filter; an empty term clears it rather than matching nothing."""
        term = self.query_one("#filter-term", Input).value
        column = self.query_one("#filter-column", Input).value.strip()
        if not column:
            self.app.bell()
            return
        mode = (
            FilterMode.EQUALS
            if self.query_one("#mode-equals", RadioButton).value
            else (FilterMode.CONTAINS)
        )
        self.dismiss(QuickFilter(column, term, mode))

    def action_cancel(self) -> None:
        self.dismiss(None)
