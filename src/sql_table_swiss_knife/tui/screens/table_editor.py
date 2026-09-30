"""Table editor placeholder (M3) — the editing surface arrives in M4.

The screen exists now so the navigation path is complete and testable end to end:
connections → tables → editor. It shows what it *would* show (the object identity, its
badges and the read-only verdict) and loads the table's metadata in a worker, which is
the same call the real grid will make — so the catalog path is proven before the grid
lands.

Known gap: no data grid, no inspector, no SQL preview yet (SPEC §6 M4/M5).
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widgets import Static

from ...domain.catalog import Table, TableKind, TableSummary
from ..widgets import KeyHint, badges_for
from .base import AppScreen

__all__ = ["TableEditorScreen"]


class TableEditorScreen(AppScreen):
    """Placeholder workspace for one table or view."""

    CSS = """
    #editor-body {
        padding: 1 2;
    }
    #editor-title {
        text-style: bold;
        height: auto;
    }
    #editor-facts {
        height: auto;
        color: $text-muted;
    }
    #editor-placeholder {
        height: auto;
        margin-top: 1;
        padding: 1 2;
        border: round $primary;
        color: $warning;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "go_back", "Back to tables", show=True),
        Binding("r", "reload", "Reload metadata", show=True),
    ]

    def __init__(self, summary: TableSummary, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._summary = summary
        self._table: Table | None = None

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        summary = self._summary
        badges = " ".join(badge.glyph for badge in badges_for(summary))
        yield from self.compose_chrome(
            f"{summary.schema}.{summary.name}",
            VerticalScroll(
                Static(f"{summary.schema}.{summary.name}  {badges}", id="editor-title"),
                Static(self._facts(), id="editor-facts"),
                Static(
                    "The data grid lands in Milestone 4.\n"
                    "Until then this screen proves the navigation path and the metadata read.",
                    id="editor-placeholder",
                ),
                id="editor-body",
            ),
        )

    def on_mount(self) -> None:
        self.refresh_header()
        self.refresh_hints()
        self.load_metadata()

    # -- data ---------------------------------------------------------------

    def load_metadata(self) -> None:
        """Read the table's full metadata in a worker (FR-6 groundwork)."""
        self.bump_generation()
        self.status.show_busy(f"reading {self._summary.ref} metadata…")
        catalog = self.services.catalog
        summary = self._summary

        async def fetch() -> None:
            try:
                table = await catalog.get_table(summary.schema, summary.name, refresh=True)
            except Exception as exc:
                self.status.show_message(str(exc), level="error")
                self.notify(str(exc), title="Metadata", severity="error")
                return
            self._table = table
            self.query_one("#editor-facts", Static).update(self._facts())
            self.status.show_message(
                f"{len(table.columns)} columns · "
                f"{'read/write' if table.updatable else 'read-only'}",
                level="success" if table.updatable else "warning",
            )
            self.refresh_hints()

        self.run_worker(fetch(), name="metadata", group=self.WORKER_GROUP, exit_on_error=False)

    def _facts(self) -> str:
        """One-line summary of the object, from the listing or the full metadata."""
        summary = self._summary
        if self._table is None:
            kind = summary.kind.value
            writable = "read-only (no primary key)" if not summary.has_primary_key else "editable"
            return f"{kind} · {writable}"
        table = self._table
        kind = "view" if table.kind is TableKind.VIEW else "table"
        columns = len(table.columns)
        keys = len(table.foreign_keys)
        verdict = "editable" if table.updatable else "read-only (no usable key — S-4)"
        return f"{kind} · {columns} columns · {keys} foreign keys · {verdict}"

    # -- actions ------------------------------------------------------------

    def action_go_back(self) -> None:
        self.pop()

    def action_reload(self) -> None:
        self.load_metadata()

    # -- hints & chrome -----------------------------------------------------

    def hints_for(self) -> tuple[KeyHint, ...]:
        return (
            KeyHint("esc", "back to tables"),
            KeyHint("r", "reload metadata"),
            KeyHint("ctrl+p", "commands"),
        )

    def _screen_title(self) -> str:
        return f"{self._summary.ref}"
