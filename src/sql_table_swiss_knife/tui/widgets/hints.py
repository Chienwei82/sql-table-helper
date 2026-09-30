"""Context-sensitive key hints in the footer (DESIGN §9.2).

Instead of a static footer, every screen publishes the hints for *its* current state
— "no profile selected" shows connect hints, "filtering" shows clear/cancel hints —
so the footer always describes what the focused screen can actually do right now.
"""

from dataclasses import dataclass

from textual.content import Content
from textual.reactive import var
from textual.widgets import Static

__all__ = ["KeyHint", "KeyHints"]


@dataclass(frozen=True, slots=True)
class KeyHint:
    """One ``key → action`` pair shown in the footer."""

    key: str
    label: str

    def markup(self) -> str:
        """Markup fragment: the key stands out, the label stays muted.

        Uses Textual markup (not Rich) because ``$accent`` / ``$text-muted`` are
        Textual variables that only the app's stylesheet can resolve — which is
        exactly what makes the hints follow the active theme.
        """
        return f"[b $accent]{self.key}[/] {self.label}"


class KeyHints(Static):
    """Single-line footer listing the current screen's key hints."""

    DEFAULT_CSS = """
    KeyHints {
        dock: bottom;
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
        content-align: left middle;
    }
    """

    hints: var[tuple[KeyHint, ...]] = var((), init=False)

    def set_hints(self, hints: tuple[KeyHint, ...]) -> None:
        """Replace the displayed hints (called whenever the screen's state changes)."""
        self.hints = hints

    def on_mount(self) -> None:
        self._refresh_view()

    def watch_hints(self) -> None:
        self._refresh_view()

    def _refresh_view(self) -> None:
        if not self.is_mounted:
            return
        if not self.hints:
            self.update("")
            return
        self.update(Content.from_markup("  ·  ".join(hint.markup() for hint in self.hints)))
