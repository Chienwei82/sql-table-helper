"""Table browser (FR-2): search-as-you-type over a schema-grouped, badged tree.

The listing is fetched once per session (the catalog service caches it) and filtering
happens entirely in memory, so typing never blocks on the database (FR-2.1). Selecting
a row pushes the editor screen; that screen is a placeholder in M3 — the data grid
lands in M4.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input, Static, Tree

from ...domain.catalog import Table, TableSummary
from ...services import SchemaGroup, filter_summaries, group_by_schema, needs_filter_prompt
from ...services.view import GridView, QuickFilter
from ..widgets import KeyHint, TableTree
from .base import AppScreen, owning_app
from .open_prompt import LargeTablePromptScreen
from .quick_filter import QuickFilterScreen
from .table_editor import TableEditorScreen

__all__ = ["TABLE_BROWSER_TITLE", "TableBrowserScreen"]

TABLE_BROWSER_TITLE = "Tables"


class TableBrowserScreen(AppScreen):
    """Filterable tree of the current database's tables and views."""

    CSS = """
    #filter-row {
        height: 3;
    }
    #table-filter {
        width: 1fr;
    }
    #table-count {
        width: auto;
        height: 3;
        content-align: right middle;
        color: $text-muted;
    }
    #table-tree {
        height: 1fr;
    }
    #table-empty {
        height: auto;
        padding: 1 2;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("/", "focus_filter", "Search", show=True),
        Binding("escape", "escape", "Back", show=False),
        Binding("enter", "open_table", "Open", show=True),
        Binding("escape", "go_back", "Back", show=True),
        Binding("r", "reload", "Reload", show=True),
        # F6, not F5: DESIGN §9.2 gives F5 to the session-wide read-only toggle (S-9),
        # and a safety switch must never be shadowed by a screen-local binding.
        Binding("f6", "toggle_schema", "Collapse schemas", show=True),
    ]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._summaries: tuple[TableSummary, ...] = ()
        self._filter = ""

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield from self.compose_chrome(TABLE_BROWSER_TITLE)
        with Vertical(id="filter-row"):
            yield Input(placeholder="filter tables…  (schema.table)", id="table-filter")
            yield Static("", id="table-count")
        yield Static(id="table-empty")
        yield TableTree(id="table-tree")

    def on_mount(self) -> None:
        self.refresh_header()
        self.refresh_hints()
        # The tree is the primary surface: start there so arrow keys work immediately;
        # "/" then moves focus to the filter instead of typing a slash into it.
        self.query_one("#table-tree", TableTree).focus()
        if not self.connection.is_connected:
            self._show_disconnected()
            return
        self.load_tables()

    # -- data ---------------------------------------------------------------

    def load_tables(self, *, refresh: bool = False) -> None:
        """Fetch (or re-fetch) the table listing in a worker."""
        self.bump_generation()
        self.status.show_busy("loading tables…")
        catalog = self.services.catalog

        async def fetch() -> None:
            """Worker body: the only place the listing call is awaited."""
            try:
                summaries = await catalog.list_tables(refresh=refresh)
            except Exception as exc:
                self.status.show_message(str(exc), level="error")
                self.notify(str(exc), title="Tables", severity="error")
                return
            self._summaries = tuple(summaries)
            self.status.show_message(f"{len(self._summaries)} object(s)")
            self._apply_filter()

        self.run_worker(fetch(), name="tables", group=self.WORKER_GROUP, exit_on_error=False)

    def _apply_filter(self) -> None:
        """Re-filter and rebuild the tree (in-memory, instant — FR-2.1)."""
        matches = filter_summaries(self._summaries, self._filter)
        groups: tuple[SchemaGroup, ...] = group_by_schema(matches)
        tree = self.query_one("#table-tree", TableTree)
        tree.populate(groups)
        count = sum(len(group.tables) for group in groups)
        self.query_one("#table-count", Static).update(
            f"{count}/{len(self._summaries)} objects · {len(groups)} schema(s)"
        )
        empty = self.query_one("#table-empty", Static)
        if count:
            empty.display = False
            # The tree is the working surface, but stealing focus while the user is
            # typing would eat every second keystroke — so only refocus when the
            # filter is not being edited.
            if not self._filter_active():
                tree.focus()
            # The cursor can only be placed once the tree has laid out its lines.
            self.call_after_refresh(tree.focus_first_table)
        else:
            empty.display = True
            if self._summaries:
                empty.update(f"nothing matches {self._filter!r}")
            else:
                empty.update("this database exposes no tables or views")
        self.refresh_hints()

    # -- actions ------------------------------------------------------------

    def action_focus_filter(self) -> None:
        """Jump to the search input (the picker's most frequent action)."""
        self.query_one("#table-filter", Input).focus()

    def action_focus_tree(self) -> None:
        """Return focus to the tree (escape inside the filter input)."""
        self.query_one("#table-tree", TableTree).focus()

    def action_open_table(self) -> None:
        """Open the table under the cursor in the editor screen (FR-2.4).

        A table the server estimates to be larger than ``filter_prompt_threshold`` asks
        the user, once, whether to add a filter first (FR-2.5). The decision is a pure
        function (:func:`~services.catalog.needs_filter_prompt`), and choosing to filter
        seeds the editor with that filter so the *first* fetch is already narrowed.
        """
        tree = self.query_one("#table-tree", TableTree)
        summary = tree.selected_summary()
        if summary is None:
            self.report_warning("select a table first (schema headers are not tables)")
            return
        threshold = owning_app(self).settings.filter_prompt_threshold
        if not needs_filter_prompt(summary.approximate_row_count, threshold=threshold):
            self.push(TableEditorScreen(summary))
            return
        count = summary.approximate_row_count
        assert count is not None  # needs_filter_prompt returns False for an unknown count

        def answered(choice: bool | None) -> None:
            """Open directly, or ask for one filter and open with it applied."""
            if choice is None:  # cancelled — stay in the browser
                return
            if choice:
                self._open_with_filter(summary)
            else:
                self.push(TableEditorScreen(summary))

        self.push(
            LargeTablePromptScreen(str(summary.ref), count, threshold=threshold),
            answered,
        )

    def _open_with_filter(self, summary: TableSummary) -> None:
        """Read the metadata, collect one filter, then open the editor with it applied.

        The metadata fetch is what gives the filter dialog its column list; it is the same
        cached read the editor would do anyway, so nothing is fetched twice.
        """

        async def work() -> Table:
            return await self.services.catalog.get_table(summary.schema, summary.name)

        def ready(table: Table) -> None:
            """Push the filter dialog once the columns are known."""
            self.push(
                QuickFilterScreen(table, self._default_filter_column(table)),
                lambda quick: self._open_filtered(summary, quick),
            )

        self.run_task(work, busy_message=f"reading {summary.ref}…", on_result=ready)

    @staticmethod
    def _default_filter_column(table: Table) -> str:
        """The column the filter dialog pre-selects: the key, else the first column."""
        identity = table.identity_columns
        return identity[0] if identity else table.columns[0].name

    def _open_filtered(self, summary: TableSummary, quick: QuickFilter | None) -> None:
        """Open the editor, seeded with the chosen filter (or unfiltered, if cleared)."""
        view = (
            GridView(filters=(quick,)) if quick is not None and not quick.is_empty else GridView()
        )
        self.push(TableEditorScreen(summary, view=view))

    def action_escape(self) -> None:
        """Escape leaves the filter first, then the screen (never traps the user)."""
        if self.focused is not self.query_one("#table-tree", TableTree):
            self.action_focus_tree()
            return
        self.action_go_back()

    def action_go_back(self) -> None:
        """Return to the connection list, leaving the session open."""
        self.pop()

    def action_reload(self) -> None:
        """Re-read the listing from the server."""
        self.load_tables(refresh=True)

    def action_toggle_schema(self) -> None:
        """Collapse or expand every schema group (FR-2.2)."""
        self.query_one("#table-tree", TableTree).toggle_schemas()

    # -- events -------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter as you type — the listing is already in memory (FR-2.1)."""
        if event.input.id == "table-filter":
            self._filter = event.value
            self._apply_filter()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter opens the first match instead of submitting nothing."""
        if event.input.id != "table-filter":
            return
        event.stop()
        if self.query_one("#table-tree", TableTree).focus_first_table() is None:
            self.report_warning("nothing matches the filter")
            return
        self.action_open_table()

    def on_tree_node_selected(self, event: Tree.NodeSelected[object]) -> None:
        """Enter or double-click on a table row opens it (mouse + keyboard, FR-2.4)."""
        event.stop()
        self.action_open_table()

    # -- hints & chrome -----------------------------------------------------

    def hints_for(self) -> tuple[KeyHint, ...]:
        """Hints change with the filter state and whether a table is selected."""
        if not self.connection.is_connected:
            return (KeyHint("esc", "back to connections"), KeyHint("ctrl+q", "quit"))
        searching = KeyHint("/", "edit filter") if self._filter else KeyHint("/", "search")
        return (
            KeyHint("enter", "open table"),
            searching,
            KeyHint("f6", "collapse schemas"),
            KeyHint("r", "reload"),
            KeyHint("esc", "back to connections"),
        )

    def _screen_title(self) -> str:
        session = self.connection.session
        return f"Tables · {session.database}" if session else TABLE_BROWSER_TITLE

    def _filter_active(self) -> bool:
        """True while the search input has focus (keystrokes belong to it)."""
        return self.focused is self.query_one("#table-filter", Input)

    def _show_disconnected(self) -> None:
        """Reconnect path instead of a crash when the session is gone (FR-10)."""
        self.query_one("#table-empty", Static).update(
            "Not connected. Press [b]esc[/b] to go back to the connection list."
        )
        self.query_one("#table-empty", Static).display = True
        self.query_one("#table-count", Static).update("offline")
        self.status.show_message("no active connection", level="error")
