"""The Apply confirmation dialog: counts, affected tables, and the typed word (FR-7.5).

This is the last gate before the only call in the application that writes to a
database, so it shows everything a person needs to catch a mistake *before* pressing
the button:

* the **counts** by kind (``2 inserts / 1 update / 3 deletes``);
* the **affected tables** — a count alone does not tell you *which* table you are
  about to change, and the whole point of a staging tool is that one change set is
  small enough to read;
* the **transaction guarantee** (all or nothing, FR-7.6);
* for production or a large delete batch, a **typed confirmation**: the confirm button
  stays disabled until the exact word has been typed.

The typed word is deliberately not dismissible by ``enter``. That is the entire
difference between "I read this" and "I meant this", and it only appears on the
operations that warrant it (see :meth:`~services.safety.SafetyPolicy.confirmation_for`).
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ...services import ApplyVerdict

__all__ = ["TRANSACTION_NOTE", "ApplyConfirmScreen"]

#: Shown under the counts: what committing does and does not mean (FR-7.6, FR-7.7).
TRANSACTION_NOTE = (
    "All statements run in ONE transaction. If any statement fails, the whole thing "
    "is rolled back and nothing is applied."
)


class ApplyConfirmScreen(ModalScreen[bool]):
    """Confirm an Apply. Dismisses ``True`` only when every requirement is met."""

    DEFAULT_CSS = """
    ApplyConfirmScreen {
        align: center middle;
    }
    #apply-box {
        width: 68;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #apply-box.-prod {
        border: round $error;
    }
    #apply-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #apply-counts {
        text-style: bold;
        margin-bottom: 1;
    }
    #apply-tables {
        height: auto;
        max-height: 8;
        margin-bottom: 1;
    }
    #apply-tables-label {
        color: $text-muted;
    }
    #apply-note {
        color: $text-muted;
        margin-bottom: 1;
    }
    #apply-typed {
        height: auto;
        margin-bottom: 1;
    }
    #apply-buttons {
        height: auto;
        align-horizontal: right;
    }
    #apply-buttons Button {
        margin-left: 2;
        min-width: 12;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        # Both accelerators route through ``try_confirm``, which re-checks the typed
        # word: binding them straight to a confirming action would let ``y`` bypass the
        # production prompt, which is precisely the hole the prompt exists to close.
        Binding("y", "try_confirm", "Apply", show=True),
        Binding("enter", "try_confirm", "Apply", show=False),
    ]

    def __init__(self, verdict: ApplyVerdict, *, title: str = "Apply staged changes") -> None:
        super().__init__()
        self._verdict = verdict
        self._title = title

    def compose(self) -> ComposeResult:
        verdict = self._verdict
        with Vertical(id="apply-box"):
            yield Static(self._title, id="apply-title")
            yield Static(f"About to apply: {verdict.summary}", id="apply-counts")
            with VerticalScroll(id="apply-tables"):
                yield Static("Affected tables:", id="apply-tables-label")
                for name in verdict.tables:
                    yield Static(f"  • {name}", classes="-table-name")
            yield Static(TRANSACTION_NOTE, id="apply-note")
            if verdict.confirmation.required:
                yield from self._typed_confirmation(verdict)
            with Horizontal(id="apply-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self._button_label(), id="confirm", variant="error")

    def _typed_confirmation(self, verdict: ApplyVerdict) -> ComposeResult:
        """The word the user must type, and the input that collects it."""
        word = verdict.confirmation.word
        assert word is not None  # guaranteed by ``TypedConfirmation.required``
        with Vertical(id="apply-typed"):
            yield Static(
                f"⚠ {verdict.confirmation.reason}.\nTo continue, type [b]{word}[/b] exactly:",
                id="apply-typed-hint",
            )
            yield Input(placeholder=word, id="apply-word")

    @property
    def is_production(self) -> bool:
        """Whether this is the production dialog (drives the red border)."""
        return "PRODUCTION" in self._verdict.confirmation.reason

    def on_mount(self) -> None:
        """Mark the production dialog and arm the confirm button correctly."""
        self.query_one("#apply-box").set_class(self.is_production, "-prod")
        self._refresh_button()
        if self._verdict.confirmation.required:
            self.query_one("#apply-word", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-evaluate the typed word on every keystroke."""
        if event.input.id == "apply-word":
            self._refresh_button()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.action_try_confirm()
        else:
            self.action_cancel()

    # -- actions -------------------------------------------------------------

    def action_try_confirm(self) -> None:
        """Confirm when the requirements are met; otherwise do nothing.

        Ignoring an unmet requirement is deliberate — the button is visibly disabled,
        so re-announcing "type the word" on every keystroke would be noise. A *wrong*
        word does get feedback, because the user may believe they are already done.
        """
        if self._requirements_met():
            self.dismiss(True)
            return
        if self._verdict.confirmation.required:
            self.query_one("#apply-typed-hint", Static).update(
                f"⚠ That is not the word. To continue, type "
                f"[b]{self._verdict.confirmation.word}[/b] exactly:"
            )

    def action_cancel(self) -> None:
        self.dismiss(False)

    # -- internals -----------------------------------------------------------

    def _requirements_met(self) -> bool:
        """Whether the confirm action is currently permitted."""
        confirmation = self._verdict.confirmation
        if not confirmation.required:
            return True
        return self.query_one("#apply-word", Input).value.strip() == confirmation.word

    def _button_label(self) -> str:
        """The confirm label, which states what is still missing when disabled."""
        if self._verdict.confirmation.required:
            word = self._verdict.confirmation.word
            return f"Type {word}"
        return "Apply now"

    def _refresh_button(self) -> None:
        """Enable the confirm button only once the requirements are met."""
        met = self._requirements_met()
        button = self.query_one("#confirm", Button)
        button.disabled = not met
        button.label = "Apply now" if met else self._button_label()
