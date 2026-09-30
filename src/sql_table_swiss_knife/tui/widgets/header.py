"""Application header: connection / database name on every screen (DESIGN §9.1).

The header is the persistent answer to "which server am I looking at?" — it shows
the screen title on the left and the live connection on the right, and it colours the
connection state with the theme's semantic error/warning roles so a lost connection is
obvious without reading the status line.
"""

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import var
from textual.widgets import Static

from ...services import ConnectionState

__all__ = ["AppHeader"]

#: Glyph + label for each connection state (never colour alone — FR-3.7 spirit).
STATE_LABEL: dict[ConnectionState, str] = {
    ConnectionState.DISCONNECTED: "○ offline",
    ConnectionState.CONNECTING: "◐ connecting…",
    ConnectionState.CONNECTED: "● connected",
    ConnectionState.ERROR: "✖ connection error",
}


class AppHeader(Horizontal):
    """Title bar carrying the active connection identity."""

    DEFAULT_CSS = """
    AppHeader {
        dock: top;
        height: 1;
        background: $panel;
        color: $text;
    }
    AppHeader > #header-title {
        width: 1fr;
        content-align: left middle;
        text-style: bold;
        padding: 0 1;
    }
    AppHeader > #header-session {
        width: auto;
        max-width: 60%;
        content-align: right middle;
        padding: 0 1;
        color: $muted;
    }
    AppHeader.-connected > #header-session {
        color: $success;
    }
    AppHeader.-connecting > #header-session {
        color: $warning;
    }
    AppHeader.-error > #header-session {
        color: $error;
    }
    """

    title: var[str] = var("", init=False)
    session_label: var[str] = var("○ offline", init=False)
    state: var[ConnectionState] = var(ConnectionState.DISCONNECTED, init=False)

    def compose(self) -> ComposeResult:
        yield Static(id="header-title")
        yield Static(id="header-session")

    def on_mount(self) -> None:
        self._refresh_view()

    def watch_title(self) -> None:
        self._refresh_view()

    def watch_session_label(self) -> None:
        self._refresh_view()

    def watch_state(self) -> None:
        self._refresh_view()

    def show_connection(self, title: str, state: ConnectionState, detail: str = "") -> None:
        """Set the title and the right-hand connection summary in one call."""
        session = STATE_LABEL[state]
        self.title = title
        self.state = state
        self.session_label = f"{session} · {detail}" if detail else session

    def _refresh_view(self) -> None:
        """Push the reactive state into the child widgets and CSS classes."""
        if not self.is_mounted:
            return  # on_mount renders once the children exist
        self.query_one("#header-title", Static).update(self.title)
        self.query_one("#header-session", Static).update(self.session_label)
        self.set_class(self.state is ConnectionState.CONNECTED, "-connected")
        self.set_class(self.state is ConnectionState.CONNECTING, "-connecting")
        self.set_class(self.state is ConnectionState.ERROR, "-error")
