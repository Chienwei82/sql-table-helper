"""Generate SQL for…" action picker (FR-5.6).

A small modal listing the statements that can be produced for the focused row or for the
current filter. It exists because the useful set of statements depends on the row: SELECT
and DELETE work for any keyed row, MERGE needs a key *and* values, and the "insert script
for all rows" action applies to the whole table rather than the cursor.

Which entries are offered is decided here (presentation) from
:attr:`RowAction`'s own rules plus the row the screen passed in; the SQL itself is produced
by :func:`~services.sqlpreview.generate_for`. Returning the chosen
:class:`RowAction` — rather than a rendered string — keeps this screen free of any SQL
logic, so there is one place that decides what a statement looks like.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

from ...services.sqlpreview import RowAction

__all__ = ["SqlActionScreen"]


class SqlActionScreen(ModalScreen[RowAction | None]):
    """Pick a statement to generate; dismisses with the action, or ``None`` if cancelled."""

    DEFAULT_CSS = """
    SqlActionScreen {
        align: center middle;
    }
    #sql-action-box {
        width: 64;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #sql-action-title {
        height: auto;
        text-style: bold;
    }
    #sql-action-subject {
        height: auto;
        color: $text-muted;
    }
    #sql-action-list {
        height: auto;
        max-height: 12;
    }
    #sql-action-list > ListItem.--highlight {
        background: $primary 30%;
    }
    #sql-action-hint {
        height: auto;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "cancel", "Cancel", show=True),
    ]

    def __init__(
        self,
        actions: tuple[RowAction, ...],
        *,
        subject: str = "",
        unavailable: str = "",
    ) -> None:
        """Args:
        actions: the actions to offer, in display order.
        subject: what the statements will be about (the row, or the filter) — shown so the
            user can see which row they are about to generate SQL for.
        unavailable: why an action was left out, shown as a footnote when something is.
        """
        super().__init__()
        self._actions = actions
        self._subject = subject
        self._unavailable = unavailable

    def compose(self) -> ComposeResult:
        with Vertical(id="sql-action-box"):
            yield Static("Generate SQL for", id="sql-action-title")
            yield Static(self._subject, id="sql-action-subject")
            yield ListView(
                *(ListItem(Static(action.label)) for action in self._actions),
                id="sql-action-list",
            )
            if self._unavailable:
                yield Static(self._unavailable, id="sql-action-hint")
            else:
                yield Static("enter run · esc cancel · nothing is executed", id="sql-action-hint")

    def on_mount(self) -> None:
        listing = self.query_one("#sql-action-list", ListView)
        if listing.children:
            listing.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._actions):
            self.dismiss(self._actions[index])

    def action_cancel(self) -> None:
        self.dismiss(None)
