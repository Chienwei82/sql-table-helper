"""Paste Preview: what the paste *will* do, before anything is staged (FR-4.6).

The whole point of this dialog is that a paste is a bulk operation nobody wants to
discover the shape of afterwards. It answers four questions in one screen:

* **Which columns does this go to?** The source→target mapping, positional or by header
  name, plus the columns the block does not touch.
* **What will each value become?** The pasted text next to the converted value, so a
  ``31.01.2026`` → ``2026-01-31`` or ``1.234,56`` → ``1234.56`` conversion is visible
  rather than assumed.
* **UPDATE or INSERT?** Per row, with the counts, so nobody pastes 500 new rows into a
  table expecting 500 updates.
* **What is refused?** Every per-cell validation error, with its row and column. A plan
  with errors **cannot** be staged — there is no "stage the valid ones" button, because a
  half-pasted block is the failure mode this milestone exists to prevent.

The dialog is a renderer over :class:`~services.clipboard.PastePlan`: it holds no clipboard
state and makes no decisions of its own, so what it shows and what
:meth:`services.changes.ChangeService.stage_paste` does cannot drift apart.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ...services.clipboard import PasteMode, PastePlan, RowOutcome

__all__ = ["PastePreviewScreen"]

#: Rows rendered before the list is elided — a 5000-row paste must not build 5000 widgets.
MAX_PREVIEW_ROWS = 40

#: Glyphs per outcome, so UPDATE/INSERT is readable without colour (FR-3.7).
_OUTCOME_GLYPH = {RowOutcome.UPDATE: "~", RowOutcome.INSERT: "+"}


class PastePreviewScreen(ModalScreen[bool]):
    """Show the plan; dismisses with ``True`` to stage it, ``False`` to cancel."""

    DEFAULT_CSS = """
    PastePreviewScreen {
        align: center middle;
    }
    #paste-box {
        width: 92;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #paste-title {
        text-style: bold;
    }
    #paste-summary {
        color: $text-muted;
    }
    #paste-errors {
        height: auto;
        max-height: 8;
        overflow-y: auto;
        color: $error;
    }
    #paste-errors.-ok {
        color: $success;
    }
    #paste-body {
        height: auto;
        max-height: 26;
        overflow-y: auto;
    }
    #paste-footer {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "confirm", "Stage paste", show=True),
        Binding("escape,q", "cancel", "Cancel", show=True),
    ]

    def __init__(self, plan: PastePlan, *, title: str = "Paste preview") -> None:
        super().__init__()
        self._plan = plan
        self._title = title

    @property
    def plan(self) -> PastePlan:
        """The plan being reviewed (the caller stages exactly this one)."""
        return self._plan

    def compose(self) -> ComposeResult:
        plan = self._plan
        with Vertical(id="paste-box"):
            yield Static(self._title, id="paste-title")
            yield Static(self._summary_line(), id="paste-summary")
            yield Static(self._errors_line(), id="paste-errors", classes="-ok" if plan.ok else "")
            yield VerticalScroll(Static(self._body(), id="paste-text"), id="paste-body")
            yield Static(
                "enter stage the paste · esc cancel — nothing has been staged yet",
                id="paste-footer",
            )

    # -- rendering ----------------------------------------------------------

    def _summary_line(self) -> str:
        plan = self._plan
        mode = {
            PasteMode.CELL: "one cell",
            PasteMode.FILL: "fill cells",
            PasteMode.ROWS: "rows",
        }[plan.mode]
        header = "by header name" if plan.used_header else "positionally"
        return (
            f"{plan.summary()} · {mode} · mapped {header}\n"
            f"columns: {plan.describe_mapping()}"
            + (f" · untouched: {', '.join(plan.untouched)}" if plan.untouched else "")
        )

    def _errors_line(self) -> str:
        plan = self._plan
        if plan.ok:
            return "✓ every cell converts cleanly — nothing is refused"
        errors = plan.errors
        shown = "\n".join(f"⛔ {message}" for message in errors[:10])
        extra = len(errors) - 10
        return f"{shown}\n… and {extra} more" if extra > 0 else shown

    def _body(self) -> str:
        """The per-row detail: outcome, target column, text, converted value."""
        plan = self._plan
        lines: list[str] = []
        for note in plan.notes:
            lines.append(f"• {note}")
        for row in plan.rows[:MAX_PREVIEW_ROWS]:
            glyph = _OUTCOME_GLYPH[row.outcome]
            where = f"row {row.target_row + 1}" if row.target_row is not None else "new row"
            lines.append(f"{glyph} {row.outcome.value.upper()} {where}")
            for cell in row.values:
                converted = cell.converted
                text = cell.text.replace("\n", "\\n").replace("\t", "\\t")
                shown = text if text else "NULL"
                if cell.error:
                    lines.append(f"    ⛔ {cell.column}: {shown} — {cell.error}")
                elif converted != shown:
                    lines.append(f"    ✓ {cell.column}: {shown} → {converted}")
                else:
                    lines.append(f"    • {cell.column}: {shown}")
        if len(plan.rows) > MAX_PREVIEW_ROWS:
            lines.append(f"… and {len(plan.rows) - MAX_PREVIEW_ROWS} more row(s)")
        return "\n".join(lines) if lines else "nothing to paste"

    # -- actions ------------------------------------------------------------

    def action_confirm(self) -> None:
        """Return ``True`` to stage — refused when any cell failed validation."""
        if not self._plan.ok:
            self.app.bell()  # the errors are on screen; staging them is not offered
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
