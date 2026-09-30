"""Small yes/no confirmation modal (S-2 style guard for destructive actions)."""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

__all__ = ["ConfirmScreen"]


class ConfirmScreen(ModalScreen[bool]):
    """Ask for confirmation; dismisses with ``True`` (yes) or ``False`` (no).

    Dismissal always yields a value so the caller never has to handle ``None``.
    """

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #confirm-box {
        width: 56;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #confirm-message {
        height: auto;
        margin-bottom: 1;
    }
    #confirm-buttons {
        height: auto;
        align-horizontal: right;
    }
    #confirm-buttons Button {
        margin-left: 2;
        min-width: 10;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y,enter", "confirm", "Yes", show=True),
        Binding("n,escape", "cancel", "No", show=True),
    ]

    def __init__(self, message: str, *, title: str = "Please confirm") -> None:
        super().__init__()
        self._message = message
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self._title, id="confirm-title")
            yield Static(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel (n)", id="cancel")
                yield Button("Confirm (y)", id="confirm", variant="error")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")
