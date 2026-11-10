"""Database picker modal (FR-1.4).

Shown when a profile has no default database, and available later to switch the live
session's database. The listing is read in a worker with a spinner — servers can be
slow to enumerate databases, and the modal must stay responsive while they are.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from ...domain.connection import ConnectionProfile
from ..widgets import KeyHint, KeyHints, StatusLine
from .base import app_services

__all__ = ["DatabasePickerScreen"]


class DatabasePickerScreen(ModalScreen[str | None]):
    """Pick a database to connect to; dismisses with the chosen name or ``None``."""

    CSS = """
    DatabasePickerScreen {
        align: center middle;
    }
    #db-box {
        width: 64;
        height: 24;
        padding: 0 1;
        background: $surface;
        border: round $primary;
    }
    #db-title {
        height: 1;
        padding-bottom: 0;
    }
    #db-filter {
        height: 3;
    }
    #db-table {
        height: 1fr;
    }
    #db-empty {
        height: auto;
        color: $text-muted;
        padding: 1 0;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("enter", "choose", "Connect", show=True),
        Binding("r", "reload", "Reload", show=True),
    ]

    def __init__(self, profile: ConnectionProfile) -> None:
        super().__init__()
        self._profile = profile
        self._databases: tuple[tuple[str, bool], ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical(id="db-box"):
            yield Static(f"Database on {self._profile.host}", id="db-title")
            yield Input(placeholder="filter databases…", id="db-filter")
            yield Static(id="db-empty")
            yield DataTable(id="db-table", zebra_stripes=True, cursor_type="row")
            yield StatusLine()
            yield KeyHints()

    def on_mount(self) -> None:
        self.refresh_hints()
        self.load_databases()

    # -- data ---------------------------------------------------------------

    def load_databases(self) -> None:
        """Connect if needed, then list the server's databases in a worker.

        The connection is opened here (not by the caller) because a profile with no
        default database still has to reach the server to enumerate databases — the
        provider connects to its fallback database and we re-connect to the chosen one
        afterwards.
        """
        self.status.show_busy("loading databases…")
        self.run_worker(self._fetch(), name="databases", group="catalog", exit_on_error=False)

    async def _fetch(self) -> None:
        """Worker body: connect if needed, read the database list, render it."""
        services = app_services(self)
        try:
            if not services.connection.is_connected:
                await services.connection.connect(self._profile)
            databases = await services.catalog.list_databases()
        except Exception as exc:
            self.status.show_message(str(exc), level="error")
            self.notify(str(exc), title="Databases", severity="error")
            return
        self._databases = tuple((db.name, db.is_current) for db in databases)
        self._refresh_table(self.query_one("#db-filter", Input).value)

    def _refresh_table(self, query: str = "") -> None:
        needle = query.strip().lower()
        rows = [
            (name, "● current" if current else "")
            for name, current in self._databases
            if not needle or needle in name.lower()
        ]
        table = self.query_one("#db-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Database", "State")
        for index, (name, state) in enumerate(rows):
            table.add_row(name, state, key=str(index))
        empty = self.query_one("#db-empty", Static)
        empty.display = not rows
        if not rows and needle:
            empty.update(f"no database matches {needle!r}")
        elif not rows:
            empty.update("the login can see no user database")
        else:
            table.focus()
        self.status.show_message(f"{len(rows)} database(s)")

    # -- actions ------------------------------------------------------------

    def action_choose(self) -> None:
        """Dismiss with the database under the cursor."""
        table = self.query_one("#db-table", DataTable)
        if table.row_count == 0:
            self.notify("no database selected", severity="warning")
            return
        row = table.get_row_at(table.cursor_row)
        self.dismiss(str(row[0]))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_reload(self) -> None:
        self.load_databases()

    # -- events -------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a focused row picks that database (the table owns ``enter``)."""
        event.stop()
        self.action_choose()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "db-filter":
            self._refresh_table(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "db-filter":
            self.action_choose()

    # -- hints & chrome -----------------------------------------------------

    @property
    def status(self) -> StatusLine:
        return self.query_one(StatusLine)

    def refresh_hints(self) -> None:
        self.query_one(KeyHints).set_hints(
            (
                KeyHint("enter", "connect to selected"),
                KeyHint("r", "reload"),
                KeyHint("esc", "cancel"),
            )
        )
