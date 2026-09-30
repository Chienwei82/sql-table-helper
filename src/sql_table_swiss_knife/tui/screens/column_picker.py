"""Column visibility modal: choose which columns the grid shows (FR-3.3).

Hiding a column is a *view* change, not a schema change: the rows are still fetched and a
staged edit in a hidden column is still applied. That is why this screen returns the set of
hidden column names rather than a redefinition of the table.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Static

from ...domain.catalog import Table

__all__ = ["ColumnPickerScreen"]


class ColumnPickerScreen(ModalScreen[frozenset[str] | None]):
    """Toggle columns on and off; dismisses with the new hidden set, or ``None`` to cancel."""

    DEFAULT_CSS = """
    ColumnPickerScreen {
        align: center middle;
    }
    #columns-box {
        width: 60;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #columns-title {
        text-style: bold;
    }
    #columns-list {
        height: auto;
        max-height: 20;
        overflow-y: auto;
    }
    ColumnPickerScreen Checkbox {
        display: block;
        height: 1;
    }
    #columns-footer {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "apply", "Apply", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, table: Table, hidden: frozenset[str]) -> None:
        super().__init__()
        self._table = table
        self._hidden = hidden

    def compose(self) -> ComposeResult:
        with Vertical(id="columns-box"):
            yield Static(f"Columns of {self._table.ref}", id="columns-title")
            yield Input(placeholder="filter columns…", id="columns-filter")
            with Vertical(id="columns-list"):
                for column in self._table.columns:
                    yield Checkbox(
                        column.name,
                        value=column.name not in self._hidden,
                        id=f"col-{column.name}",
                    )
            yield Static("enter apply · esc cancel", id="columns-footer")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter the list locally: the column *set* is metadata already in memory."""
        needle = event.value.strip().lower()
        for column in self._table.columns:
            self.query_one(f"#col-{column.name}", Checkbox).display = (
                not needle or needle in column.name.lower()
            )

    def action_apply(self) -> None:
        hidden = frozenset(
            column.name
            for column in self._table.columns
            if not self.query_one(f"#col-{column.name}", Checkbox).value
        )
        if len(hidden) == len(self._table.columns):
            self.app.bell()  # a grid with no columns shows nothing at all
            return
        self.dismiss(hidden)

    def action_cancel(self) -> None:
        self.dismiss(None)
