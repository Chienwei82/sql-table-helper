"""File path prompt for CSV/JSON import and export (M7).

A modal with one input, because that is all "export to…" and "import from…" need: the
surrounding work (encoding, header handling, validation, the preview dialog) belongs to
:mod:`services.transfer` and the paste pipeline, not to a file chooser.

The suffix in the path *is* the format (``rows.csv`` → CSV, ``rows.json`` → JSON), so the
field doubles as the format choice and the dialog can say what it will do before the user
commits.
"""

import os
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

__all__ = ["PathPromptScreen"]


class PathPromptScreen(ModalScreen[Path | None]):
    """Ask for a file path; dismisses with the resolved path, or ``None`` when cancelled."""

    DEFAULT_CSS = """
    PathPromptScreen {
        align: center middle;
    }
    #path-box {
        width: 72;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #path-title {
        text-style: bold;
    }
    #path-hint {
        color: $text-muted;
    }
    #path-error {
        color: $error;
        height: auto;
    }
    #path-footer {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "confirm", "Continue", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(
        self,
        prompt: str,
        *,
        hint: str = "",
        initial: str = "",
        must_exist: bool = False,
    ) -> None:
        """Args:
        prompt: The dialog title, e.g. ``Export dbo.Country as CSV``.
        hint: A line explaining what will happen (shown under the field).
        initial: Pre-filled path (the export dialog offers a sensible default).
        must_exist: Import validates that the file is there before dismissing, so the
            error appears next to the field instead of as a toast afterwards.
        """
        super().__init__()
        self._prompt = prompt
        self._hint = hint
        self._initial = initial
        self._must_exist = must_exist

    def compose(self) -> ComposeResult:
        with Vertical(id="path-box"):
            yield Static(self._prompt, id="path-title")
            yield Input(value=self._initial, placeholder="/path/to/file.csv", id="path-input")
            yield Static(self._hint, id="path-hint")
            yield Static("", id="path-error")
            yield Static("enter continue · esc cancel", id="path-footer")

    def on_mount(self) -> None:
        self.query_one("#path-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the field continues (``Input`` consumes the key otherwise)."""
        if event.input.id == "path-input":
            self.action_confirm()

    def action_confirm(self) -> None:
        """Return the expanded path, or stay open when it is empty/missing."""
        raw = self.query_one("#path-input", Input).value.strip()
        error = self.query_one("#path-error", Static)
        if not raw:
            error.update("enter a file path")
            return
        path = Path(os.path.expanduser(raw))
        if self._must_exist and not path.is_file():
            error.update(f"{path} does not exist")
            return
        self.dismiss(path)

    def action_cancel(self) -> None:
        self.dismiss(None)
