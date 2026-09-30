"""Foreign-key picker modal: choose a referenced row instead of typing an id (FR-4.4).

Opening a foreign-key cell shows the referenced rows — key plus a best-guess description
column — filtered **server-side** as the user types, so a reference into a large table is
searchable rather than a guess. ``enter`` picks, ``escape`` cancels.

The list is a service product (:class:`services.lookup.LookupService`); this screen only
runs the search in a worker and renders the result, which is why the picking behaviour can
be tested against ``FakeProvider`` without a terminal.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from ...domain.catalog import ForeignKey
from ...services.lookup import LookupChoice, LookupService
from .base import app_services

__all__ = ["LookupPickerScreen"]


class LookupPickerScreen(ModalScreen[LookupChoice | None]):
    """Searchable list of the rows a foreign key may point at."""

    DEFAULT_CSS = """
    LookupPickerScreen {
        align: center middle;
    }
    #lookup-box {
        width: 70;
        height: 24;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #lookup-title {
        text-style: bold;
    }
    #lookup-grid {
        height: 1fr;
    }
    #lookup-status {
        color: $text-muted;
        height: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "choose", "Choose", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, foreign_key: ForeignKey, *, title: str = "") -> None:
        super().__init__()
        self._fk = foreign_key
        self._title = title or f"{foreign_key.referenced_schema}.{foreign_key.referenced_table}"
        self._choices: tuple[LookupChoice, ...] = ()
        #: Bumped per search so a slow earlier search cannot overwrite a newer result.
        self._generation = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="lookup-box"):
            yield Static(f"Pick a value for {self._title}", id="lookup-title")
            yield Input(placeholder="search…", id="lookup-search")
            yield DataTable(id="lookup-grid", cursor_type="row", zebra_stripes=True)
            yield Static("", id="lookup-status")

    def on_mount(self) -> None:
        self.query_one("#lookup-search", Input).focus()
        self.run_search("")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "lookup-search":
            self.run_search(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the search box picks the highlighted row (``Input`` eats the key)."""
        if event.input.id == "lookup-search":
            self.action_choose()

    # -- search -------------------------------------------------------------

    def run_search(self, term: str) -> None:
        """Search the referenced table in a worker, guarded by a generation counter.

        Every keystroke starts a search; without the guard a slow earlier search could land
        after a newer one and leave the list showing the wrong rows.
        """
        self._generation += 1
        generation = self._generation
        self.query_one("#lookup-status", Static).update("searching…")
        services = app_services(self)
        lookup: LookupService = services.lookup

        async def work() -> None:
            try:
                result = await lookup.search(self._fk, term)
            except Exception as exc:  # a missing reference must not kill the screen
                if generation == self._generation:
                    self.query_one("#lookup-status", Static).update(str(exc))
                return
            if generation == self._generation:
                self._show(result.choices, result.header(), result.truncated)

        self.run_worker(work(), name="lookup", exit_on_error=False)

    def _show(self, choices: tuple[LookupChoice, ...], description: str, truncated: bool) -> None:
        self._choices = choices
        grid = self.query_one("#lookup-grid", DataTable)
        grid.clear(columns=True)
        grid.add_column("key", key=None)
        grid.add_column(description, key=None)
        for choice in choices:
            grid.add_row(_key_label(choice), choice.label)
        status = self.query_one("#lookup-status", Static)
        if not choices:
            status.update("no matching rows")
        elif truncated:
            status.update(f"{len(choices)} shown — keep typing to narrow")
        else:
            status.update(f"{len(choices)} row(s) · enter to choose")

    # -- actions ------------------------------------------------------------

    def action_choose(self) -> None:
        """Return the highlighted choice, or stay put when the list is empty."""
        grid = self.query_one("#lookup-grid", DataTable)
        row = grid.cursor_row
        if not 0 <= row < len(self._choices):
            self.app.bell()
            return
        self.dismiss(self._choices[row])

    def action_cancel(self) -> None:
        self.dismiss(None)


def _key_label(choice: LookupChoice) -> str:
    """The key columns of one choice, as one readable cell."""
    return " / ".join(_format(value) for _, value in choice.key)


def _format(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes | bytearray | memoryview):
        return "0x" + bytes(value)[:8].hex()
    return str(value)
