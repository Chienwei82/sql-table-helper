"""Cell editor modal: type a value, see what the column accepts, stage it (FR-3.5).

A modal rather than an inline editor on purpose. The whole point of opening a cell is to
learn something about the column — how long the string may be, whether the value fits its
type, whether the server has defaults — and that needs room; a one-cell-wide inline box in
a dense grid cannot show it. It also means a half-typed value can never reach the grid's
rendered state.

Everything shown here comes from :mod:`services.validation`, the same validation the
staging path uses, so a value that could never be stored is refused here rather than at
Apply. Foreign-key columns do not come here at all: they open the lookup picker instead
(``LookupPickerScreen``), because typing a raw identifier helps nobody.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from ...domain.catalog import Column, Table
from ...services.validation import ParsedValue, validate_input

__all__ = ["CellEditorScreen", "as_text"]


class CellEditorScreen(ModalScreen[ParsedValue | None]):
    """Edit one cell's text; dismisses with the parsed value, or ``None`` when cancelled.

    ``enter`` returns the parsed value (the caller stages it), ``escape`` cancels. The
    caller is responsible for staging: this screen only *parses and validates*, so the rule
    "an invalid value is never staged" lives in one place rather than two.
    """

    DEFAULT_CSS = """
    CellEditorScreen {
        align: center middle;
    }
    #cell-editor-box {
        width: 64;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #cell-editor-title {
        text-style: bold;
    }
    #cell-editor-hints {
        height: auto;
        max-height: 8;
    }
    #cell-editor-footer {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "commit", "Stage", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, table: Table, column: Column, initial: object) -> None:
        super().__init__()
        self._table = table
        self._column = column
        self._initial = as_text(initial)
        self._parsed = validate_input(table, column, self._initial)

    def compose(self) -> ComposeResult:
        with Vertical(id="cell-editor-box"):
            yield Static(f"{self._table.ref} · {self._column.name}", id="cell-editor-title")
            yield Input(
                value=self._initial,
                placeholder="empty or NULL for SQL NULL",
                id="cell-editor-input",
            )
            yield Static("", id="cell-editor-hints")
            yield Static("enter stage · esc cancel", id="cell-editor-footer")

    def on_mount(self) -> None:
        self.query_one("#cell-editor-input", Input).focus()
        self._show(self._parsed)

    # -- live validation ----------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-validate on every keystroke so the hints track what is being typed (FR-3.5)."""
        self._parsed = validate_input(self._table, self._column, event.value)
        self._show(self._parsed)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter inside the field commits.

        ``Input`` binds ``enter`` to its own ``submit`` action, which does not bubble, so
        the screen-level binding would never fire while the field has the focus.
        """
        if event.input.id == "cell-editor-input":
            self.action_commit()

    def _show(self, parsed: ParsedValue) -> None:
        self.query_one("#cell-editor-hints", Static).update(
            "\n".join(f"{hint.glyph} {hint.text}" for hint in parsed.hints)
        )

    # -- actions ------------------------------------------------------------

    def action_commit(self) -> None:
        """Return the parsed value, or stay open when a hint blocks staging."""
        if not self._parsed.ok:
            self.app.bell()  # the blocking hint is already on screen
            return
        self.dismiss(self._parsed)

    def action_cancel(self) -> None:
        self.dismiss(None)


def as_text(value: object) -> str:
    """The editor's starting text for a cell value (round-trips the grid's own format)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, bytes | bytearray | memoryview):
        return "0x" + bytes(value).hex()
    return str(value)
