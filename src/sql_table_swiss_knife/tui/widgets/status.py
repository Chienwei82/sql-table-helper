"""Status line: loading indicator plus the last action's outcome (FR-10, DESIGN §9.3).

Every DB call runs in a Textual worker; while one is in flight this widget shows a
spinner and the pending description, so the user always sees progress within 100 ms
instead of a frozen UI. Errors are surfaced here *and* as a toast.
"""

from textual.app import ComposeResult
from textual.reactive import var
from textual.widgets import LoadingIndicator, Static

__all__ = ["StatusLine"]


class StatusLine(Static):
    """One-line status strip: spinner (when busy) plus a message."""

    DEFAULT_CSS = """
    StatusLine {
        dock: bottom;
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
        layout: horizontal;
        align: left middle;
    }
    StatusLine > LoadingIndicator {
        width: 2;
        height: 1;
        display: none;
        margin-right: 1;
    }
    StatusLine.-busy > LoadingIndicator {
        display: block;
    }
    StatusLine > #status-message {
        width: 1fr;
        height: 1;
        content-align: left middle;
    }
    StatusLine.-error > #status-message {
        color: $error;
    }
    StatusLine.-warning > #status-message {
        color: $warning;
    }
    """

    message: var[str] = var("", init=False)
    level: var[str] = var("", init=False)  # "", "error", "warning", "success"
    busy: var[bool] = var(False, init=False)

    def compose(self) -> ComposeResult:
        yield LoadingIndicator(id="status-spinner")
        yield Static(id="status-message")

    def on_mount(self) -> None:
        self._refresh_view()

    def watch_message(self) -> None:
        self._refresh_view()

    def watch_level(self) -> None:
        self._refresh_view()

    def watch_busy(self) -> None:
        self._refresh_view()

    # -- public API ---------------------------------------------------------

    def show_busy(self, message: str) -> None:
        """Announce a long-running operation and show the spinner."""
        self.busy = True
        self.level = ""
        self.message = message

    def show_message(self, message: str, level: str = "") -> None:
        """Report an idle outcome (``level`` styles it: error/warning/success)."""
        self.busy = False
        self.level = level
        self.message = message

    def clear(self) -> None:
        """Reset to an empty, idle status line."""
        self.show_message("")

    # -- internals ----------------------------------------------------------

    def _refresh_view(self) -> None:
        if not self.is_mounted:
            return
        self.set_class(self.busy, "-busy")
        self.set_class(self.level == "error", "-error")
        self.set_class(self.level == "warning", "-warning")
        self.query_one("#status-message", Static).update(self.message)
