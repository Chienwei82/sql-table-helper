"""The help screen (F1): every keybinding, grouped, with the safety notes (NFR-6).

The content is rendered from :mod:`sql_table_swiss_knife.tui.keybindings`, never typed
out here, so this screen cannot fall behind the bindings it documents. It shows the
user's own overrides where they exist, and it is grouped by *what you are trying to do*
rather than alphabetically — a reference is read by intent, not by scanning.

The bottom half is the part that is not a key list: the safety rules, stated plainly,
because they are the reason the app is usable against a production catalog and they are
easy to forget between sessions.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..keybindings import ActionId, action, groups
from ..keymap import Keymap

__all__ = ["SAFETY_NOTES", "HelpScreen"]

#: The rules that make the app safe to point at production, in plain language.
SAFETY_NOTES: tuple[str, ...] = (
    "Nothing is written until you press ctrl+s — edits, pastes and imports are "
    "staged in memory and shown in the pending strip.",
    "Apply runs every statement in one transaction: if any fails, none of it is "
    "applied and your staged changes stay exactly where they were.",
    "A profile marked production opens read-only (f5 toggles it; --read-only locks "
    "it). Against production an Apply needs a typed confirmation.",
    "Rows with no primary key are read-only — an UPDATE or DELETE cannot be scoped "
    "to one row, so the app refuses rather than guess.",
    "Every applied script is appended to audit.log.jsonl in your config directory, "
    "with the timestamp, the profile and the statements. It never contains a "
    "password.",
    "Passwords live in the OS keyring, never in profiles.toml and never in this app's logs.",
)


class HelpScreen(ModalScreen[None]):
    """The keybinding reference and the safety summary."""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-box {
        width: 84%;
        max-width: 100;
        height: 90%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #help-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #help-body {
        height: 1fr;
    }
    .help-group {
        text-style: bold;
        color: $accent;
    }
    .help-binding {
        padding-left: 2;
    }
    #help-footer {
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q,question_mark", "close", "Close", show=True),
    ]

    def __init__(self, keymap: Keymap | None = None) -> None:
        super().__init__()
        self._keymap = keymap if keymap is not None else Keymap({})

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-box"):
            yield Static("Key bindings", id="help-title")
            yield from self._group_widgets()
            yield Static("Safety", classes="help-group")
            for note in SAFETY_NOTES:
                yield Static(f"• {note}", classes="help-binding")
            yield Static(self._footer_text(), id="help-footer")

    def _group_widgets(self) -> ComposeResult:
        """One heading and one line per action, in :data:`GROUP_ORDER`."""
        for group, bindings in groups():
            yield Static(group, classes="help-group")
            for doc in bindings:
                yield Static(
                    f"{self._key_label(doc.action, doc.keys)} — {doc.description}",
                    classes="help-binding",
                )

    def _key_label(self, action_name: str, default_keys: tuple[str, ...]) -> str:
        """The key(s) to show, with the user's override marked when present."""
        override = self._keymap.get(action_name)
        if override is not None:
            return f"{override}  (default: {' / '.join(default_keys)})"
        return " / ".join(default_keys)

    def _footer_text(self) -> str:
        """The provenance line: where this list comes from, and how to change it."""
        return (
            "Bindings are read from the app's own declarations; edit keybindings.toml in "
            f"your config directory to override what this screen shows "
            f"(current override for help: {action(ActionId.HELP, self._keymap.overrides)})."
        )

    def action_close(self) -> None:
        self.dismiss(None)
