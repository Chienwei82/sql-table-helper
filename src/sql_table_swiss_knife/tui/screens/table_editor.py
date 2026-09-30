import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.coordinate import Coordinate
from textual.events import Paste
from textual.widgets import DataTable, Static

from ...domain.catalog import Column, Table, TableSummary
from ...domain.changes import ChangeKind
from ...domain.rows import Row, RowKey
from ...services.cellview import cell_view
from ...services.changes import ApplyError, CellStatus, ChangeService
from ...services.clipboard import (
    ClipboardBlock,
    ClipboardOptions,
    ClipboardParseError,
    PastePlan,
    PasteTarget,
    encode_block,
    parse_block,
    plan_paste,
)
from ...services.connection import is_connection_lost
from ...services.data import DataService, RowWindow, status_text
from ...services.inspector import InspectorService, Severity, Warning, fks_for, read_only_reason
from ...services.lookup import LookupChoice
from ...services.sqlpreview import (
    RowAction,
    SqlMode,
    SqlPreview,
    entries_for,
    generate_for,
    next_mode,
)
from ...services.transfer import TransferError, export_rows, read_block, suffix_format, write_text
from ...services.validation import ParsedValue
from ...services.view import GridView, QuickFilter
from ...storage import AuditEntry, AuditLogError
from ..widgets import CellState, DataGrid, InspectorPanel, KeyHint, SqlPanel, row_key_for
from ..widgets.sql_panel import ModeChosen
from .apply_confirm import ApplyConfirmScreen
from .base import AppScreen, owning_app
from .cell_editor import CellEditorScreen
from .cell_view import CellViewScreen
from .column_picker import ColumnPickerScreen
from .confirm import ConfirmScreen
from .lookup_picker import LookupPickerScreen
from .paste_preview import PastePreviewScreen
from .quick_filter import QuickFilterScreen
from .sql_action import SqlActionScreen
from .transfer_path import PathPromptScreen

__all__ = ["TableEditorScreen"]


#: Maps a service-level cell status onto the widget's class/glyph vocabulary.
_STATUS_CLASS: dict[CellStatus, str] = {
    CellStatus.UNCHANGED: CellState.UNCHANGED,
    CellStatus.MODIFIED: CellState.MODIFIED,
    CellStatus.NEW: CellState.NEW,
    CellStatus.DELETED: CellState.DELETED,
    CellStatus.READ_ONLY: CellState.READ_ONLY,
}


class TableEditorScreen(AppScreen):
    """Split view over one table or view: rows on the left, schema on the right."""

    CSS = """
    #editor-body {
        height: 1fr;
    }
    #editor-warnings {
        height: auto;
        max-height: 6;
        padding: 0 2;
        background: $panel;
        display: none;
    }
    #editor-warnings.-shown {
        display: block;
    }
    #editor-pending {
        height: 1;
        padding: 0 2;
        background: $panel;
        color: $text-muted;
        display: none;
    }
    #editor-pending.-shown {
        display: block;
    }
    #editor-pending.-dirty {
        color: $warning;
    }
    InspectorPanel {
        width: 46;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "go_back", "Back to tables", show=True),
        Binding("f2", "toggle_inspector", "Toggle inspector", show=True),
        Binding("f3", "toggle_sql_panel", "SQL panel", show=True),
        Binding("v", "sql_mode", "SQL mode", show=True),
        Binding("y", "copy_sql", "Copy SQL", show=False),
        # -- clipboard (FR-4) --
        Binding("ctrl+c", "copy_data", "Copy", show=True),
        Binding("b", "copy_scope", "Copy scope", show=False),
        Binding("p", "copy_format", "Copy format", show=False),
        Binding("ctrl+v", "paste", "Paste", show=True),
        Binding("i", "import_file", "Import file", show=False),
        Binding("o", "export_file", "Export file", show=False),
        # -- selection (rectangular, for copy and for fill) --
        Binding("shift+up", "select_up", "Extend", show=False, priority=True),
        Binding("shift+down", "select_down", "Extend", show=False, priority=True),
        Binding("shift+left", "select_left", "Extend", show=False, priority=True),
        Binding("shift+right", "select_right", "Extend", show=False, priority=True),
        Binding("ctrl+escape", "clear_selection", "Clear selection", show=False, priority=True),
        Binding("g", "generate_sql", "Generate SQL for", show=True),
        Binding("r", "reload", "Reload", show=True),
        Binding("m", "fetch_more", "Fetch more", show=False),
        # -- editing (M5) --
        Binding("enter,f4", "edit_cell", "Edit cell", show=True),
        Binding("ctrl+n", "insert_row", "New row", show=True),
        Binding("ctrl+d", "duplicate_row", "Duplicate", show=True),
        Binding("delete", "delete_row", "Delete", show=True),
        Binding("ctrl+z", "undo", "Undo", show=True),
        Binding("ctrl+shift+z", "redo", "Redo", show=False),
        Binding("ctrl+u", "revert_row", "Revert row", show=True),
        Binding("ctrl+shift+u", "discard_all", "Discard all", show=True),
        Binding("ctrl+s", "apply", "Apply", show=True),
        # -- view (FR-3.2/3.3) --
        # M8: the expand view for long text and binary cells (FR-3.1).
        Binding("w", "expand_cell", "Expand cell", show=True),
        Binding("c", "toggle_columns", "Columns", show=True),
        Binding("f", "quick_filter", "Filter", show=True),
        Binding("s", "sort_column", "Sort", show=True),
    ]

    def __init__(self, summary: TableSummary, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._summary = summary
        self._table: Table | None = None
        self._window: RowWindow | None = None
        self._inspector = InspectorService()
        self._inspector_collapsed = False
        self._view = GridView()
        self._changes: ChangeService | None = None
        #: Rows staged as new, in staging order: the grid shows them after the fetched ones.
        self._new_rows: dict[RowKey, dict[str, object]] = {}
        #: Server-generated keys of the most recent Apply, so the grid can re-key rows.
        self._applied_keys: tuple[RowKey, ...] = ()
        # -- clipboard (FR-4) --
        #: What the data-copy action copies: the focused cell, its whole row, its whole
        #: column, or the selected rectangle. Kept as a screen-level state (and cycled with
        #: ``b``) because "what did I just copy?" is a question the user must be able to
        #: answer *before* pressing the key — an implicit rule would be guesswork.
        self._copy_scope = "cell"
        #: The format the copy encodes as (FR-4.2), cycled with ``p``; the initial value
        #: comes from ``settings.toml`` so the preference survives a restart.
        self._copy_format = "tsv"
        #: Anchor of a shift-arrow selection, in grid coordinates.
        self._selection_anchor: Coordinate | None = None
        #: Columns the export dialog will write (captured when the dialog is pushed).
        self._export_columns: tuple[Column, ...] = ()
        #: SQL panel state (FR-5). The panel starts hidden and defaults to the *script*
        #: rendering: it is the one that is safe to run by hand, which is what a user who
        #: just found the panel is most likely to want.
        self._sql_mode = SqlMode.SCRIPT
        self._sql_open = False
        self._sql_generated: tuple[str, str] | None = None
        #: Set while a dropped connection is awaiting the user's reconnect decision.
        self._connection_lost: bool = False

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        summary = self._summary
        yield from self.compose_chrome(
            f"{summary.schema}.{summary.name}",
            Static(id="editor-warnings"),
            Static(id="editor-pending"),
            Horizontal(
                DataGrid(id="editor-grid"),
                InspectorPanel(inspector=self._inspector, id="editor-inspector"),
                id="editor-body",
            ),
            # Starts hidden: the panel is an opt-in view (F3), so it must not steal any
            # height from the grid until the user asks for it.
            SqlPanel(id="editor-sql", classes="-hidden"),
        )

    def on_mount(self) -> None:
        # The copy format is a preference (``copy_format`` in settings.toml), so the app
        # opens with the format the user last chose rather than a hard-coded default.
        self._copy_format = owning_app(self).settings.copy_format
        self.refresh_header()
        self.refresh_hints()
        self.query_one("#editor-grid", DataGrid).focus()
        if not self.connection.is_connected:
            self._show_disconnected()
            return
        self.load_metadata()

    @property
    def changes(self) -> ChangeService | None:
        """The staging service for this table, or ``None`` before metadata loads."""
        return self._changes

    @property
    def view(self) -> GridView:
        """The current sort/filter/visibility state."""
        return self._view

    # -- data ---------------------------------------------------------------

    def load_metadata(self, *, rows: bool = True) -> None:
        """Load (or reload) the metadata, and optionally the first page of rows.

        Metadata comes first because the inspector, the grid headers and the staging rules
        all need it; the row fetch follows in the same worker chain, and a stale generation
        result is dropped so switching tables never flashes the wrong schema (FR-3.9).
        """
        generation = self.bump_generation()
        self.status.show_busy(f"reading {self._summary.ref}…")
        catalog = self.services.catalog
        data: DataService = self.services.data
        summary = self._summary
        view = self._view
        fetch_rows = rows

        async def work() -> None:
            try:
                table = await catalog.get_table(summary.schema, summary.name, refresh=True)
                window = (
                    await data.fetch(
                        table,
                        sort=view.sort_for(table),
                        filters=view.predicates(),
                    )
                    if fetch_rows
                    else None
                )
            except Exception as exc:
                if generation != self.generation:
                    return
                self._inspector.clear()
                # A dropped connection is not a query failure: it is a dead session, and
                # the only useful response is to offer a reconnect (FR-10).
                if is_connection_lost(exc):
                    self._handle_connection_lost(exc)
                    return
                self.report_error(str(exc))
                return
            if generation != self.generation:
                return
            self._apply_metadata(table, window)

        self.run_worker(work(), name="metadata", group=self.WORKER_GROUP, exit_on_error=False)

    def fetch_more_rows(self) -> None:
        """Append the next page of rows (``m``, FR-3.8)."""
        if self._window is None or not self._window.has_more:
            self.status.show_message("no more rows to fetch")
            return
        generation = self.generation
        data: DataService = self.services.data
        window = self._window
        table = self._table

        async def work() -> None:
            try:
                if table is None:
                    return
                grown = await data.fetch(
                    table,
                    limit=max(window.limit, 1000),
                    offset=window.count,
                    sort=window.sort,
                    filters=window.filters,
                )
            except Exception as exc:
                if generation == self.generation:
                    if is_connection_lost(exc):
                        self._handle_connection_lost(exc)
                    else:
                        self.report_error(str(exc))
                return
            if generation != self.generation:
                return
            self._apply_rows(grown)

    def _apply_metadata(self, table: Table, window: RowWindow | None) -> None:
        """Install freshly loaded metadata (and rows) into every widget.

        A reload must not cost the user their staged work (FR-10), so an existing
        staging buffer is re-pointed at the new metadata instead of being replaced. Only
        a table that genuinely changed identity gets a fresh buffer.
        """
        if self._changes is None or not self._changes.rebind(table):
            self._changes = ChangeService(self.connection, table)
        self._table = table
        self._inspector.show(table)
        panel = self.query_one("#editor-inspector", InspectorPanel)
        panel.bind_inspector(self._inspector)
        if window is not None:
            self._window = window
            self._render_rows()
        self._sync_focus()
        self._show_warning_summary(self._inspector.warnings())
        self._refresh_pending()
        self.refresh_hints()
        self.refresh_header()

    def _apply_rows(self, window: RowWindow) -> None:
        """Replace the loaded rows after a "fetch more", a filter or a reload."""
        self._window = window
        self._render_rows()
        self._sync_focus()
        self._refresh_pending()
        self.refresh_hints()

    def _render_rows(self) -> None:
        """Rebuild the grid from the fetched window plus any staged new rows."""
        grid = self.query_one("#editor-grid", DataGrid)
        if self._table is None or self._window is None:
            return
        rows = [*self._window.rows, *self._staged_rows()]
        keys = [*self._window_keys(), *self._new_rows.keys()]
        grid.load(self._table, rows, hidden=self._view.hidden, keys=keys)
        self._refresh_overlay()

    def _staged_rows(self) -> list[Row]:
        """The staged INSERT rows, as grid rows carrying their staged values."""
        return [Row(values) for values in self._new_rows.values()]

    def _window_keys(self) -> list[RowKey | None]:
        """The identities of the fetched rows, in the grid's order."""
        if self._table is None or self._window is None:
            return []
        return [row_key_for(self._table, row.values) for row in self._window.rows]

    def _refresh_overlay(self) -> None:
        """Redraw the staged values and states over the fetched rows (FR-3.7)."""
        grid = self.query_one("#editor-grid", DataGrid)
        changes = self._changes
        if changes is None:
            return
        statuses: dict[tuple[RowKey | None, str], str] = {}
        values: dict[tuple[RowKey | None, str], object] = {}
        for row_index in range(len(grid.fetched_rows)):
            key = grid.key_at(row_index)
            row = grid.fetched_rows[row_index]
            for column in grid.visible_columns:
                status = (
                    changes.status_for(key, column.name)
                    if key is not None
                    else (CellStatus.UNCHANGED)
                )
                statuses[(key, column.name)] = _STATUS_CLASS[status]
                if status is CellStatus.MODIFIED and key is not None:
                    values[(key, column.name)] = changes.display_values(key, row.values).get(
                        column.name
                    )
        grid.apply_overlay(statuses, values)

    def _show_warning_summary(self, warnings: tuple[Warning, ...]) -> None:
        """Show the one-line severity roll-up above the grid, when there is anything to say.

        The full text lives in the inspector panel; this strip exists so a collapsed panel
        still tells the user the table is dangerous.
        """
        errors = sum(1 for warning in warnings if warning.severity is Severity.ERROR)
        cautions = sum(1 for warning in warnings if warning.severity is Severity.WARNING)
        banner = self.query_one("#editor-warnings", Static)
        if not errors and not cautions:
            banner.remove_class("-shown")
            return
        parts: list[str] = []
        if errors:
            parts.append(f"⛔ {errors} critical")
        if cautions:
            parts.append(f"⚠ {cautions} caution" + ("" if cautions == 1 else "s"))
        banner.update(f"{'  ·  '.join(parts)} — see the inspector (F2)")
        banner.add_class("-shown")

    def _refresh_pending(self) -> None:
        """The pending-changes strip: ``N inserts, M updates, K deletes`` (FR-7.2).

        Also refreshes the SQL panel, so the preview and the strip can never disagree about
        how much is staged (FR-7.2 + FR-5.1).
        """
        strip = self.query_one("#editor-pending", Static)
        changes = self._changes
        if changes is None or changes.is_empty:
            strip.remove_class("-shown")
            strip.remove_class("-dirty")
            strip.update("")
            self.refresh_sql_panel()
            return
        counts = changes.counts
        strip.update(
            f"pending: {changes.summary}"
            f"  ·  {self._view.sort_label()}  ·  {self._view.filter_label()}"
            "  ·  ctrl+s to apply"
        )
        strip.add_class("-shown")
        strip.set_class(counts[ChangeKind.DELETE] > 0, "-dirty")
        self.refresh_sql_panel()

    def _sync_focus(self) -> None:
        """Point the inspector at the grid's focused column and re-render the panel."""
        grid = self.query_one("#editor-grid", DataGrid)
        column = grid.column_at(grid.cursor_column)
        self._inspector.focus(column.name if column is not None else None)
        self.query_one("#editor-inspector", InspectorPanel).refresh_content()

    # -- cursor helpers -----------------------------------------------------

    def _cursor(self) -> tuple[RowKey | None, Column | None, int, int]:
        """``(row key, column, row index, column index)`` under the grid cursor."""
        grid = self.query_one("#editor-grid", DataGrid)
        row_index, column_index = grid.cursor_row, grid.cursor_column
        return grid.key_at(row_index), grid.column_at(column_index), row_index, column_index

    def _original_values(self, row_index: int) -> dict[str, object]:
        """The *fetched* values of a row, ignoring anything staged (for WHERE clauses)."""
        grid = self.query_one("#editor-grid", DataGrid)
        if not 0 <= row_index < len(grid.fetched_rows):
            return {}
        if grid.is_new_row(row_index):
            return {}
        row = grid.fetched_rows[row_index]
        key = grid.key_at(row_index)
        staged = self._changes.find(key) if self._changes and key is not None else None
        if staged is None or staged.change.before is None:
            return dict(row.values)
        return dict(staged.change.before)

    def _display_values(self, row_index: int) -> dict[str, object]:
        """What the grid shows for a row: staged values over the fetched ones."""
        grid = self.query_one("#editor-grid", DataGrid)
        if not 0 <= row_index < len(grid.fetched_rows):
            return {}
        values = dict(grid.fetched_rows[row_index].values)
        key = grid.key_at(row_index)
        if self._changes is not None and key is not None:
            staged = self._changes.find(key)
            if staged is not None and staged.change.after is not None:
                values.update(staged.change.after)
        return values

    # -- events -------------------------------------------------------------

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        """Cursor moved: the inspector follows it (FR-6.3)."""
        event.stop()
        grid = self.query_one("#editor-grid", DataGrid)
        column = grid.column_at(event.coordinate.column)
        self._inspector.focus(column.name if column is not None else None)
        self.query_one("#editor-inspector", InspectorPanel).refresh_content()

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Enter/F4 on a cell: open the editor, or explain why the cell is read-only."""
        event.stop()
        self.action_edit_cell()

    # -- navigation ---------------------------------------------------------

    def action_go_back(self) -> None:
        """Leave the table, warning first when work is staged and unsaved."""
        changes = self._changes
        if changes is None or changes.is_empty:
            self.pop()
            return
        self.push(
            ConfirmScreen(
                f"{changes.summary} are staged and have not been applied.\n"
                "Leave anyway? They will be lost.",
                title="Unsaved changes",
            ),
            self._on_leave_confirmed,
        )

    def _on_leave_confirmed(self, confirmed: bool | None) -> None:
        if confirmed:
            self.pop()

    def action_toggle_inspector(self) -> None:
        """Collapse/expand the inspector panel (``F2``)."""
        self._inspector_collapsed = not self._inspector_collapsed
        self.query_one("#editor-inspector", InspectorPanel).set_collapsed(self._inspector_collapsed)
        self.refresh_hints()

    def action_reload(self) -> None:
        self.load_metadata()

    def action_fetch_more(self) -> None:
        self.fetch_more_rows()

    # -- cell inspection (M8) ------------------------------------------------

    def action_expand_cell(self) -> None:
        """Open the focused cell in full — long text wrapped, binary as a hex dump.

        Bound to ``w`` ("wider view") rather than to ``enter``, which already edits:
        expanding a cell is a *reading* action, and a key that both reads and writes
        would make the safest action the riskiest to press. A cell with nothing more to
        show says so instead of opening an empty dialog.
        """
        _, column, row_index, _ = self._cursor()
        if column is None or row_index < 0:
            return
        values = self._display_values(row_index)
        if column.name not in values:
            self.status.show_message("no value under the cursor")
            return
        view = cell_view(values[column.name])
        if not view.expandable:
            self.status.show_message(
                f"{column.name}: {view.summary} — nothing to expand ({view.text})"
            )
            return
        table = self._table
        self.push(
            CellViewScreen(
                values[column.name],
                title=f"{table.ref if table else ''} · {column.name}",
                view=view,
            )
        )

    def _focused_cell_is_expandable(self) -> bool:
        """Whether the focused cell has more to show than the grid does (FR-3.1).

        Drives the hint, so the key is advertised only where it does something —
        an always-present "expand" hint on a column of two-letter codes trains the
        user to ignore the footer.
        """
        _, column, row_index, _ = self._cursor()
        if column is None or row_index < 0:
            return False
        return cell_view(self._display_values(row_index).get(column.name)).expandable

    # -- editing ------------------------------------------------------------

    def action_edit_cell(self) -> None:
        """Open the editor for the focused cell, or the picker for a foreign key.

        A foreign-key cell opens the lookup list instead of a text box: asking a human to
        type an identifier helps nobody, and the picker shows the referenced rows (FR-4.4).
        """
        changes = self._changes
        table = self._table
        key, column, row_index, _ = self._cursor()
        if changes is None or table is None or column is None or row_index is None:
            return
        reason = read_only_reason(column)
        if reason is not None:
            self.status.show_message(f"{column.name}: {reason}", level="warning")
            return
        if key is None and not grid_is_new(self, row_index):
            self.status.show_message(
                "this row has no usable key — it cannot be edited (S-4)", level="warning"
            )
            return
        if not self.services.safety.check_table(table).allowed:
            # Read-only, a view, or a keyless table — the policy words it for us (S-4/S-9).
            self.report_warning(self.services.safety.check_table(table).message, title="Read-only")
            return
        foreign_key = fks_for(table, column.name)
        if foreign_key:
            self.push(
                LookupPickerScreen(
                    foreign_key[0],
                    title=f"{table.ref} · {column.name}",
                ),
                lambda choice: self._on_lookup_chosen(choice, key, column, row_index),
            )
            return
        self.push(
            CellEditorScreen(table, column, self._display_values(row_index).get(column.name)),
            lambda parsed: self._on_cell_edited(parsed, key, column, row_index),
        )

    def _on_cell_edited(
        self,
        parsed: ParsedValue | None,
        key: RowKey | None,
        column: Column,
        row_index: int,
    ) -> None:
        """Stage the value the cell editor returned (``None`` means the user cancelled)."""
        if parsed is None or self._changes is None or key is None:
            return
        original = self._original_values(row_index)
        if grid_is_new(self, row_index):
            edit = self._changes.fill_new_row(key, column.name, parsed.value)
        else:
            edit = self._changes.edit_cell(key, column.name, _render(parsed.value), original)
        self._after_stage(edit.ok, f"{column.name} staged" if edit.ok else edit.message)

    def _on_lookup_chosen(
        self,
        choice: LookupChoice | None,
        key: RowKey | None,
        column: Column,
        row_index: int,
    ) -> None:
        """Stage the picked key value, mapping the reference's columns onto this one."""
        if choice is None or self._changes is None or key is None or not choice.key:
            return
        value = choice.key[0][1]
        original = self._original_values(row_index)
        if grid_is_new(self, row_index):
            edit = self._changes.fill_new_row(key, column.name, value)
        else:
            edit = self._changes.stage_value(key, column.name, value, original)
        self._after_stage(edit.ok, f"{column.name} = {value!r}")

    def action_insert_row(self) -> None:
        """Stage a brand new row at the end of the grid (``ctrl+n``)."""
        changes = self._changes
        if changes is None or not self._require_writable():
            return
        key = changes.insert_row()
        self._new_rows[key] = {}
        self._render_rows()
        # Put the cursor on the new row: staging a row is always followed by filling it in.
        grid = self.query_one("#editor-grid", DataGrid)
        grid.focus()
        grid.cursor_coordinate = Coordinate(grid.row_count - 1, grid.cursor_column)
        self._after_stage(True, "new row staged — fill in its cells")

    def action_duplicate_row(self) -> None:
        """Stage a copy of the focused row (``ctrl+d``)."""
        changes = self._changes
        key, _, row_index, _ = self._cursor()
        if changes is None or key is None or not self._require_writable():
            return
        copy = changes.duplicate_row(key, self._display_values(row_index))
        staged = changes.find(copy)
        self._new_rows[copy] = dict(staged.change.after or {}) if staged is not None else {}
        self._render_rows()
        grid = self.query_one("#editor-grid", DataGrid)
        grid.focus()
        grid.cursor_coordinate = Coordinate(grid.row_count - 1, grid.cursor_column)
        self._after_stage(True, "row duplicated — set the key before applying")

    def action_delete_row(self) -> None:
        """Mark the focused row for deletion (``delete``)."""
        changes = self._changes
        key, _, row_index, _ = self._cursor()
        if changes is None or key is None or not self._require_writable():
            return
        if grid_is_new(self, row_index):
            # Nothing was ever written, so "deleting" just forgets the staged row.
            self._new_rows.pop(key, None)
            changes.revert(key)
        else:
            changes.delete_row(key, self._original_values(row_index))
        self._render_rows()
        self._after_stage(True, "row marked for deletion")

    def action_undo(self) -> None:
        """Undo the last staging action (``ctrl+z``)."""
        if self._changes is not None and self._changes.undo():
            self._sync_new_rows()
            self._after_stage(True, "undone")
        else:
            self.status.show_message("nothing to undo")

    def action_redo(self) -> None:
        """Redo the last undone staging action (``ctrl+shift+z``)."""
        if self._changes is not None and self._changes.redo():
            self._sync_new_rows()
            self._after_stage(True, "redone")
        else:
            self.status.show_message("nothing to redo")

    def action_revert_row(self) -> None:
        """Drop the staged changes of the focused row (``ctrl+u``)."""
        key, column, row_index, _ = self._cursor()
        if self._changes is None or key is None or column is None:
            return
        if grid_is_new(self, row_index):
            self._new_rows.pop(key, None)
        if self._changes.revert(key):
            self._render_rows()
            self._after_stage(True, f"row reverted: {column.name}")
        else:
            self.status.show_message("this row has no staged changes")

    def action_discard_all(self) -> None:
        """Discard every staged change, after a confirmation (S-2)."""
        changes = self._changes
        if changes is None or changes.is_empty:
            self.status.show_message("nothing staged")
            return
        self.push(
            ConfirmScreen(
                f"Discard {changes.summary}?\nNothing has been written to the database yet.",
                title="Discard staged changes",
            ),
            self._on_discard_confirmed,
        )

    def _on_discard_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed or self._changes is None:
            return
        self._changes.revert_all()
        self._new_rows.clear()
        self._render_rows()
        self._after_stage(True, "all staged changes discarded")

    def _require_writable(self) -> bool:
        """Refuse a write the safety policy forbids, with the reason (S-4, S-9).

        Every staging entry point calls this, so read-only, views and keyless tables
        are refused *before* anything reaches the staging area rather than at Apply
        time — a change set that cannot be applied is worse than no change set at all.
        The wording comes from the policy so the screen cannot drift from the rule.
        """
        table = self._table
        if table is None:
            return False
        permission = self.services.safety.check_table(table)
        if not permission.allowed:
            self.report_warning(permission.message, title="Read-only")
            return False
        return True

    @property
    def is_read_only(self) -> bool:
        """Whether this screen currently refuses writes (S-9)."""
        return self.services.safety.read_only

    def _sync_new_rows(self) -> None:
        """Reconcile the staged-insert list after an undo/redo (a row may reappear)."""
        changes = self._changes
        if changes is None:
            self._new_rows.clear()
            return
        self._new_rows = {
            staged.key: dict(staged.change.after or {})
            for staged in changes.staged_rows()
            if staged.is_new
        }

    def _after_stage(self, ok: bool, message: str) -> None:
        """Redraw the overlay and the pending strip after any staging change."""
        # A generated statement is a snapshot of one row at one moment; once the staging
        # area moves, keeping it on screen would be showing SQL for a state that no longer
        # exists, so the panel falls back to the change list.
        self._sql_generated = None
        self._refresh_overlay()
        self._refresh_pending()
        self.refresh_sql_panel()
        self.refresh_hints()
        self.refresh_header()
        self.status.show_message(message, level="" if ok else "warning")

    # -- view ---------------------------------------------------------------

    def action_toggle_columns(self) -> None:
        """Choose which columns the grid shows (``c``, FR-3.3)."""
        table = self._table
        if table is None:
            return
        self.push(ColumnPickerScreen(table, self._view.hidden), self._on_columns_chosen)

    def _on_columns_chosen(self, hidden: frozenset[str] | None) -> None:
        if hidden is None:
            return
        self._view = GridView(hidden=hidden, sort=self._view.sort, filters=self._view.filters)
        self._render_rows()
        self._after_stage(True, "columns updated")

    def action_quick_filter(self) -> None:
        """Filter the focused column on the server (``f``, FR-3.3)."""
        table = self._table
        _, column, _, _ = self._cursor()
        if table is None or column is None:
            return
        current = next((quick for quick in self._view.filters if quick.column == column.name), None)
        self.push(
            QuickFilterScreen(
                table,
                column.name,
                current.text if current else "",
                current.mode if current else None,
            ),
            self._on_filter_chosen,
        )

    def _on_filter_chosen(self, quick: QuickFilter | None) -> None:
        if quick is None:
            return
        self._view = self._view.with_filter(quick)
        self.load_metadata()

    def clear_filters(self) -> None:
        """Drop every quick filter and re-fetch (``escape`` in the filter box)."""
        if not self._view.is_filtered:
            return
        self._view = self._view.with_filter_cleared(next(iter(self._view.filters)).column)
        self.load_metadata()

    def action_sort_column(self) -> None:
        """Cycle ascending → descending → unsorted on the focused column (``s``)."""
        _, column, _, _ = self._cursor()
        if column is None or self._table is None:
            return
        self._view = self._view.with_sort(column.name)
        self.load_metadata()

    # -- clipboard (FR-4) ---------------------------------------------------

    @property
    def clipboard_options(self) -> ClipboardOptions:
        """The user's copy/paste settings, read fresh (they can change mid-session)."""
        return ClipboardOptions.from_settings(owning_app(self).settings)

    def _paste_target(self) -> PasteTarget | None:
        """The grid as the clipboard service sees it: values, keys and the anchor cell."""
        table = self._table
        if table is None:
            return None
        grid = self.query_one("#editor-grid", DataGrid)
        count = len(grid.fetched_rows)
        return PasteTarget(
            table=table,
            columns=grid.visible_columns,
            rows=tuple(self._display_values(index) for index in range(count)),
            keys=tuple(grid.key_at(index) for index in range(count)),
            anchor_row=grid.cursor_row,
            anchor_column=grid.cursor_column,
            selection=self.selection(),
        )

    def selection(self) -> tuple[int, int, int, int] | None:
        """The selected rectangle as ``(top, left, bottom, right)``, or ``None``.

        The anchor is set by the first ``shift+arrow``; the live end is the grid cursor, so
        the selection follows the cursor the way every text widget's does.
        """
        anchor = self._selection_anchor
        if anchor is None:
            return None
        grid = self.query_one("#editor-grid", DataGrid)
        end = Coordinate(grid.cursor_row, grid.cursor_column)
        top, bottom = sorted((anchor.row, end.row))
        left, right = sorted((anchor.column, end.column))
        return (top, left, bottom, right)

    def action_select(self, row_delta: int = 0, column_delta: int = 0) -> None:
        """Extend the selection with ``shift+arrow`` (FR-4.1: copy a range)."""
        grid = self.query_one("#editor-grid", DataGrid)
        if self._selection_anchor is None:
            self._selection_anchor = Coordinate(grid.cursor_row, grid.cursor_column)
        grid.cursor_coordinate = Coordinate(
            max(0, grid.cursor_row + row_delta), max(0, grid.cursor_column + column_delta)
        )

    def action_select_up(self) -> None:
        self.action_select(row_delta=-1)

    def action_select_down(self) -> None:
        self.action_select(row_delta=1)

    def action_select_left(self) -> None:
        self.action_select(column_delta=-1)

    def action_select_right(self) -> None:
        self.action_select(column_delta=1)

    def action_clear_selection(self) -> None:
        """Drop the selection (``ctrl+escape``)."""
        self._selection_anchor = None
        self.refresh_hints()

    # -- copy ----------------------------------------------------------------

    def action_copy_data(self) -> None:
        """Copy the cell / row / column / selection as the current format (``ctrl+c``).

        Deliberately a different key from ``y`` (copy SQL): "copy this row" and "copy the
        INSERT for this row" are different requests, and a user who reaches for one must
        never get the other.
        """
        table = self._table
        if table is None:
            return
        columns, rows = self._copy_block()
        if not rows:
            self.status.show_message("nothing to copy — no rows are loaded", level="warning")
            return
        text = encode_block(
            [column.name for column in columns],
            rows,
            fmt=self._copy_format,
            options=self.clipboard_options,
        )
        outcome = owning_app(self).clipboard_service.copy(text)
        scope = self._selection_label()
        if outcome.ok:
            self.status.show_message(
                f"copied {scope} as {self._copy_format} ({len(text)} chars) — {outcome.describe()}"
            )
        else:
            self.report_warning(outcome.detail or "the clipboard could not be written")

    def _selection_label(self) -> str:
        """What the copy action will take, named the way the user thinks about it."""
        if self._copy_scope == "selection":
            rectangle = self.selection()
            return "the selection" if rectangle else "the focused cell (no selection)"
        if self._copy_scope == "row":
            return "the focused row"
        if self._copy_scope == "column":
            return "the focused column"
        return "the focused cell"

    def _copy_block(self) -> tuple[tuple[Column, ...], tuple[tuple[object, ...], ...]]:
        """The cell/row/column/selection rectangle the copy action should encode (FR-4.1)."""
        grid = self.query_one("#editor-grid", DataGrid)
        visible = grid.visible_columns
        if not visible or not grid.fetched_rows:
            return (), ()
        row_index, column_index = grid.cursor_row, grid.cursor_column
        scope = self._copy_scope
        indices: Sequence[int]
        columns: tuple[Column, ...]
        rectangle = self.selection()
        if scope == "selection" and rectangle is not None:
            top, left, bottom, right = rectangle
            columns = visible[left : right + 1]
            indices = range(top, min(bottom, len(grid.fetched_rows) - 1) + 1)
        elif scope == "row":
            columns, indices = visible, [row_index]
        elif scope == "column":
            columns, indices = (visible[column_index],), range(len(grid.fetched_rows))
        else:
            columns, indices = (visible[column_index],), [row_index]
        rows = tuple(
            tuple(self._display_values(index).get(column.name) for column in columns)
            for index in indices
        )
        return columns, rows

    def action_copy_scope(self) -> None:
        """Cycle what the copy takes: cell → row → column → selection (``b``)."""
        scopes = ("cell", "row", "column", "selection")
        position = scopes.index(self._copy_scope) if self._copy_scope in scopes else 0
        self._copy_scope = scopes[(position + 1) % len(scopes)]
        self.refresh_hints()
        self.status.show_message(f"ctrl+c copies {self._selection_label()}")

    def action_copy_format(self) -> None:
        """Cycle the copy format TSV → CSV → JSON (``p``, FR-4.2) and remember it."""
        self._copy_format = self.clipboard_options.next_copy_format(self._copy_format)
        owning_app(self).persist_settings(copy_format=self._copy_format)
        self.refresh_hints()
        self.status.show_message(f"copy format: {self._copy_format.upper()}")

    # -- paste ---------------------------------------------------------------

    def on_paste(self, event: Paste) -> None:
        """Terminal bracketed paste (FR-4.7) — the primary paste path.

        Textual delivers the whole payload as one event, which is what makes a multi-line
        Excel block arrive intact instead of as a stream of keystrokes.
        """
        event.stop()
        self.handle_paste_text(event.text)

    def action_paste(self) -> None:
        """``ctrl+v`` without bracketed paste: read the system clipboard instead.

        Reading a clipboard is opt-in (``clipboard_read_fallback``); when it is off, say so
        rather than appearing to paste nothing.
        """
        clipboard = owning_app(self).clipboard_service
        if not clipboard.read_fallback:
            self.report_warning(
                "this terminal did not send a bracketed paste; enable "
                "clipboard_read_fallback in settings.toml to paste with ctrl+v"
            )
            return
        text = clipboard.read()
        if text is None:
            self.report_warning("the system clipboard could not be read")
            return
        self.handle_paste_text(text)

    def handle_paste_text(self, text: str) -> None:
        """Parse, plan and preview a paste payload — nothing is staged yet (FR-4.6)."""
        target = self._paste_target()
        table = self._table
        if target is None or table is None:
            self.status.show_message("no table is open — nothing to paste into")
            return
        options = self.clipboard_options
        if not self._require_writable():
            return
        names = [column.name for column in table.columns]
        try:
            block = parse_block(text, known_columns=names)
        except ClipboardParseError as exc:
            self.report_error(str(exc))
            return
        if block.is_empty:
            self.status.show_message("nothing to paste — the clipboard text is empty")
            return
        if block.ragged:
            self.status.show_message(
                f"the block is ragged (rows of different widths, {block.column_count} "
                "columns wide) — missing cells are treated as empty",
            )
        self._plan_in_background(target, block, options)

    def _plan_in_background(
        self, target: PasteTarget, block: ClipboardBlock, options: ClipboardOptions
    ) -> None:
        """Plan off the event loop so a huge paste cannot freeze the UI (NFR-2).

        Parsing and converting a 5000-row block with per-cell validation is real work; it
        runs in a worker thread while the status line shows progress, and the result comes
        back through the event loop to be previewed.
        """
        self.status.show_busy("planning the paste…")

        async def work() -> None:
            # The worker resumes on the event loop after the thread finishes, so the
            # review dialog is pushed from the UI thread without ``call_from_thread``.
            plan = await asyncio.to_thread(plan_paste, target, block, options=options)
            self._review_paste(plan)

        self.run_worker(work(), name="paste-plan", group="paste")

    def _review_paste(self, plan: PastePlan) -> None:
        """Show the Paste Preview dialog for a planned paste (FR-4.6).

        A plan that converts nothing is never previewed: either a cell failed (and the user
        needs the *list*, not a dialog to confirm) or the whole block was refused up front
        (empty, over ``paste_max_rows``, nothing to map). Both are reported as one message.
        """
        if plan.rows and plan.errors:
            self.report_error(
                f"paste refused: {len(plan.errors)} cell(s) do not convert "
                f"(first: {plan.errors[0]})"
            )
            return
        if not plan.rows:
            self.report_warning(f"paste refused: {'; '.join(plan.notes) or 'nothing to paste'}")
            return
        self.push(
            PastePreviewScreen(plan, title=f"Paste into {self._table.ref if self._table else ''}"),
            lambda confirmed: self._on_paste_confirmed(confirmed, plan),
        )

    def _on_paste_confirmed(self, confirmed: bool | None, plan: PastePlan) -> None:
        """Stage a confirmed plan — through the staging area, never the database (S-1)."""
        changes = self._changes
        if not confirmed or changes is None:
            return
        grid = self.query_one("#editor-grid", DataGrid)
        originals = tuple(self._original_values(index) for index in range(len(grid.fetched_rows)))
        edit = changes.stage_paste(plan, originals)
        if not edit.ok:
            self.report_warning(edit.message or "the paste was not staged")
            return
        self._sync_new_rows()
        self._render_rows()
        self._after_stage(True, edit.message or plan.summary())

    # -- import / export (file transfer, same pipeline) ----------------------

    def action_import_file(self) -> None:
        """Import a CSV/JSON file through the same preview a clipboard paste gets (``i``)."""
        if self._table is None or not self._require_writable():
            return
        self.push(
            PathPromptScreen(
                f"Import into {self._table.ref}",
                hint=(
                    "the file is parsed exactly like a pasted block: header names map by "
                    "name, everything else positionally. Nothing is written until you "
                    "confirm the preview and apply."
                ),
                must_exist=True,
            ),
            self._on_import_path,
        )

    def _on_import_path(self, path: Path | None) -> None:
        """Read the file and hand it to the same planning path as a paste."""
        target = self._paste_target()
        if path is None or target is None:
            return
        names = [column.name for column in target.table.columns]
        try:
            block = read_block(path, known_columns=names)
        except TransferError as exc:
            self.report_error(str(exc))
            return
        self.status.show_message(f"read {path.name}: {block.row_count} row(s)")
        self._plan_in_background(target, block, self.clipboard_options)

    def action_export_file(self) -> None:
        """Export the rows on screen (staged edits included) to a CSV/JSON file (``o``)."""
        table = self._table
        if table is None:
            return
        grid = self.query_one("#editor-grid", DataGrid)
        columns = grid.visible_columns
        if not grid.fetched_rows:
            self.status.show_message("nothing to export — no rows are loaded", level="warning")
            return
        default = Path.cwd() / f"{table.name}.csv"
        self.push(
            PathPromptScreen(
                f"Export {table.ref} as CSV",
                hint=(
                    "the file holds the rows as shown, staged edits included; the .csv or "
                    ".json suffix picks the format."
                ),
                initial=str(default),
            ),
            self._on_export_path,
        )
        self._export_columns = columns

    def _on_export_path(self, path: Path | None) -> None:
        """Write the export; a write error is reported rather than swallowed."""
        columns = self._export_columns
        if path is None or not columns:
            return
        grid = self.query_one("#editor-grid", DataGrid)
        rows = [self._display_values(index) for index in range(len(grid.fetched_rows))]
        fmt = suffix_format(path) or "csv"
        try:
            text = export_rows(
                [column.name for column in columns],
                rows,
                fmt=fmt,
                options=self.clipboard_options,
            )
            write_text(path, text)
        except TransferError as exc:
            self.report_error(str(exc))
            return
        self.report_info(f"exported {len(rows)} row(s) to {path} as {fmt}", title="Export")

    # -- SQL panel (FR-5) ---------------------------------------------------

    @property
    def sql_panel(self) -> SqlPanel:
        """The SQL preview panel."""
        return self.query_one("#editor-sql", SqlPanel)

    def build_preview(self) -> SqlPreview:
        """The panel's content for the current staging state, in the current mode.

        Built on demand rather than cached, so the panel can never show SQL for changes
        that are no longer staged: every re-render derives it from the ``ChangeSet``.
        """
        table = self._table
        changes = self._changes
        dialect = self.connection.provider().dialect
        if table is None:
            return SqlPreview(table=None, dialect=dialect, mode=self._sql_mode)
        entries = (
            entries_for(
                dialect,
                table,
                [staged.change for staged in changes.staged_rows()],
                identity_insert=changes.requires_identity_insert(),
            )
            if changes is not None
            else ()
        )
        preview = SqlPreview(
            table=table,
            dialect=dialect,
            entries=entries,
            mode=self._sql_mode,
            identity_insert=changes is not None and changes.requires_identity_insert(),
        )
        if self._sql_generated is not None:
            sql, title = self._sql_generated
            return preview.with_generated(sql, title)
        return preview

    def refresh_sql_panel(self) -> None:
        """Re-render the panel from the current staging area (no-op while it is closed)."""
        if not self._sql_open:
            return
        self.sql_panel.preview = self.build_preview()

    def _show_sql_panel(self) -> None:
        """Open the panel (without stealing focus from the grid) and fill it in."""
        self._sql_open = True
        self.sql_panel.set_class(False, "-hidden")
        self.refresh_sql_panel()

    def action_toggle_sql_panel(self) -> None:
        """Show/hide the SQL preview panel (``F3``)."""
        if self._sql_open:
            self._sql_open = False
            self.sql_panel.set_class(True, "-hidden")
            self.query_one("#editor-grid", DataGrid).focus()
        else:
            self._show_sql_panel()
        self.refresh_hints()

    def action_sql_mode(self) -> None:
        """Cycle the rendering: parameterized → literal → script (``v``, FR-5.2/5.3)."""
        self._sql_mode = next_mode(self._sql_mode)
        self._show_sql_panel()
        self.status.show_message(f"SQL panel: {self._sql_mode.label} — {self._sql_mode.hint}")
        self.refresh_hints()

    def on_sql_panel_mode_chosen(self, event: ModeChosen) -> None:
        """A click on the mode tabs: adopt the chosen rendering (same value as ``v``)."""
        try:
            self._sql_mode = SqlMode[event.mode.upper()]
        except KeyError:  # pragma: no cover - a tab id we did not create
            return
        self._show_sql_panel()
        self.status.show_message(f"SQL panel: {self._sql_mode.label} — {self._sql_mode.hint}")
        self.refresh_hints()

    def action_copy_sql(self) -> None:
        """Copy to the clipboard: the whole script, or the selected statement (``y``).

        All three copy targets FR-5.5 asks for are reachable from one key because they
        differ only by what is *selected* in the panel: no selection means the whole script,
        a selected statement means that statement (with its parameter legend, so the text
        is usable on its own), and a generated statement means what "generate" produced.
        """
        preview = self.build_preview()
        panel = self.sql_panel
        if panel.selected >= 0 and preview.entry(panel.selected + 1) is not None:
            text = preview.copy_entry(panel.selected + 1)
            what = f"statement {panel.selected + 1}"
        else:
            text = preview.copy_all()
            what = "the generated statement" if preview.generated else "the whole script"
        if not text:
            self.status.show_message("nothing to copy — nothing is staged", level="warning")
            return
        self.app.copy_to_clipboard(text)
        self.status.show_message(f"copied {what} ({len(text)} chars) to the clipboard")

    def action_generate_sql(self) -> None:
        """Pick a statement to generate for the focused row or the filter (``g``, FR-5.6)."""
        table = self._table
        if table is None:
            return
        key, _, row_index, _ = self._cursor()
        actions, unavailable = self._available_actions(table, key)
        if not actions:
            self.status.show_message(
                unavailable or "no statement can be generated here", level="warning"
            )
            return
        subject = (
            f"row {row_index + 1}"
            if key is not None
            else f"all {self._row_count()} loaded row(s) of {table.ref}"
        )
        self._show_sql_panel()
        self.push(
            SqlActionScreen(actions, subject=subject, unavailable=unavailable),
            self._on_sql_action,
        )

    def _available_actions(
        self, table: Table, key: RowKey | None
    ) -> tuple[tuple[RowAction, ...], str]:
        """Which actions this row/table supports, and why the others do not.

        Conservative by design: an action is offered only when it can produce a *correct*
        statement, and the reason for every omission is shown so an absent entry is
        explained rather than merely noticed.
        """
        reasons: list[str] = []
        actions: list[RowAction] = []
        if key is not None:
            actions.extend((RowAction.SELECT, RowAction.INSERT))
            if self.services.safety.check_table(table).allowed:
                actions.extend((RowAction.UPDATE, RowAction.DELETE))
            elif self.is_read_only:
                reasons.append("this session is read-only — no UPDATE/DELETE SQL")
            else:
                reasons.append("UPDATE/DELETE need a usable row key — this table has none")
        else:
            reasons.append("this row has no key, so single-row statements are not offered")
        if self._row_count() == 0:
            reasons.append("no rows are loaded, so there is nothing to generate SQL for")
        elif not table.identity_columns:
            reasons.append("MERGE and the insert script need a primary key on the table")
        else:
            actions.extend((RowAction.MERGE, RowAction.INSERT_SCRIPT))
        return tuple(actions), " · ".join(reasons)

    def _on_sql_action(self, action: RowAction | None) -> None:
        """Render the chosen action into the panel — it is never executed (S-1)."""
        if action is None:
            return
        table = self._table
        if table is None:
            return
        key, _, row_index, _ = self._cursor()
        changes = self._changes
        try:
            sql = generate_for(
                self.connection.provider().dialect,
                table,
                action,
                key=key if action.needs_key else None,
                values=self._row_values(row_index) if action.needs_values else None,
                rows=self._all_row_values() if action.covers_table else (),
                identity_insert=changes is not None and changes.requires_identity_insert(),
            )
        except (ValueError, TypeError) as exc:
            self.report_error(f"cannot generate {action.value}: {exc}")
            return
        self._sql_generated = (sql, action.label)
        # A generated statement is literal text by nature, so literal is the mode the user
        # wants to see and copy; the script mode would wrap a statement belonging to no
        # change set, implying a transaction that does not exist.
        self._sql_mode = SqlMode.LITERAL
        self._show_sql_panel()
        self.status.show_message(f"generated {action.label} — nothing was executed")

    def _row_values(self, row_index: int) -> dict[str, object] | None:
        """The values shown for the grid row at ``row_index``, staged edits included."""
        rows = self.query_one("#editor-grid", DataGrid).fetched_rows
        if not 0 <= row_index < len(rows):
            return None
        return dict(rows[row_index].values)

    def _all_row_values(self) -> list[dict[str, object]]:
        """Every loaded row's values — the source for "insert script for all rows"."""
        return [dict(row.values) for row in self.query_one("#editor-grid", DataGrid).fetched_rows]

    def _row_count(self) -> int:
        """How many rows the grid is showing (fetched plus staged new ones)."""
        return len(self.query_one("#editor-grid", DataGrid).fetched_rows)

    def action_apply(self) -> None:
        """Review and apply every staged change in one transaction (``ctrl+s``, FR-7.6).

        The safety policy decides *whether* this may happen and *how much* the user has
        to confirm; this method only routes the verdict to the right dialog and reports
        a refusal. Keeping the decision in the policy is what makes it testable and
        keeps the same rules in force for the paste and import paths, which stage
        through the very same change set.
        """
        changes = self._changes
        table = self._table
        if changes is None or changes.is_empty:
            self.status.show_message("nothing staged — nothing to apply")
            return
        verdict = self.services.safety.review(
            [table] if table is not None else [],
            changes.counts,
        )
        if not verdict.allowed:
            self.report_warning(verdict.message, title="Apply blocked")
            return
        self.push(
            ApplyConfirmScreen(verdict, title=f"Apply to {table.ref if table else ''}"),
            self._on_apply_confirmed,
        )

    def _on_apply_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self._apply_now()

    def _apply_now(self) -> None:
        """Run the Apply in a worker, then refresh what the database now holds.

        The re-fetch is not optional: identity values are generated by the server, and a
        rowversion is bumped by the write, so the grid cannot know the new values without
        asking again.
        """
        changes = self._changes
        if changes is None:
            return
        generation = self.bump_generation()
        self.status.show_busy("applying staged changes…")
        # Captured before the Apply: a successful Apply clears the staging area, so the
        # counts and the statements have to be read while they still exist. This is also
        # the record of what was *attempted*, which matters when the Apply fails.
        counts = dict(changes.counts)
        statements = self._audit_statements(changes)

        async def work() -> None:
            try:
                result = await changes.apply()
            except ApplyError as exc:
                if generation == self.generation:
                    self._write_audit(counts, statements, "rolled_back", 0, exc.message)
                    self._report_apply_error(exc)
                return
            except Exception as exc:
                if generation == self.generation:
                    self._write_audit(counts, statements, "rolled_back", 0, str(exc))
                    if is_connection_lost(exc):
                        self._handle_connection_lost(exc)
                    else:
                        self.report_error(str(exc))
                return
            if generation != self.generation:
                return
            self._new_rows.clear()
            self._applied_keys = result.inserted_keys
            self._write_audit(counts, statements, "committed", result.duration_ms, None)
            self._after_apply(result.statement_count, result.duration_ms)

        self.run_worker(work(), name="apply", group=self.WORKER_GROUP, exit_on_error=False)

    def _after_apply(self, statements: int, duration_ms: int) -> None:
        self.load_metadata()
        self.notify(
            f"applied {statements} statement(s) in {duration_ms} ms",
            title="Applied",
            severity="information",
            timeout=4,
        )

    # -- audit (M8) ----------------------------------------------------------

    def _audit_statements(self, changes: ChangeService) -> tuple[str, ...]:
        """The literal SQL of the pending Apply, for the audit record.

        Read before the Apply runs, from the same builder the provider executes, so the
        log states what was *attempted*. A failure to build them (no provider, a
        dialect error) yields an empty tuple rather than blocking the Apply: the write
        must not be stopped by the record of the write.
        """
        try:
            return tuple(statement.sql_literal for statement in changes.statements())
        except Exception:
            return ()

    def _write_audit(
        self,
        counts: Mapping[ChangeKind, int],
        statements: tuple[str, ...],
        outcome: str,
        duration_ms: int,
        error: str | None,
    ) -> None:
        """Append this Apply to the audit log; never let logging break the Apply.

        The log is a record, not a gate: a read-only config directory must not stop a
        database write the user confirmed, and must not lose their staged work either.
        A failure is surfaced once, as a warning, so it is never silently ignored.
        """
        session = self.connection.session
        table = self._table
        if session is None or table is None:
            return
        entry = AuditEntry.now(
            profile=session.profile_name,
            environment=session.environment,
            server=session.server,
            database=session.database,
            table=str(table.ref),
            counts={kind.value: count for kind, count in counts.items()},
            statements=statements,
            outcome=outcome,
            duration_ms=duration_ms,
            error=error,
        )
        try:
            owning_app(self).audit_log.record(entry)
        except AuditLogError as exc:
            self.report_warning(f"apply not recorded in the audit log: {exc}", title="Audit")

    def _report_apply_error(self, error: ApplyError) -> None:
        """Report a failed Apply without losing the staged work (FR-7.6, FR-7.8).

        The status line carries the reason *and* the fact that nothing was written, in
        one message: replacing the reason with a generic "nothing was applied" would hide
        the only line that tells the user which row to refresh.
        """
        conflicts = error.conflicts
        if conflicts:
            assert self._changes is not None
            reason = self._changes.conflict_message(conflicts[0])
            if len(conflicts) > 1:
                reason += f" (+{len(conflicts) - 1} more row(s))"
            message = f"{reason} — nothing was applied, your changes are still staged"
            self.report_warning(message, title="Concurrency conflict")
        else:
            message = f"{error.message} — nothing was applied, your changes are still staged"
            self.report_error(message, title="Apply failed")

    # -- hints & chrome -----------------------------------------------------

    def hints_for(self) -> tuple[KeyHint, ...]:
        changes = self._changes
        table = self._table
        writable = table is not None and self.services.safety.check_table(table).allowed
        hints = [
            KeyHint("f2", "show inspector" if self._inspector_collapsed else "hide inspector"),
            KeyHint(
                "f3",
                f"hide sql ({self._sql_mode.label})" if self._sql_open else "show sql",
            ),
        ]
        if self._sql_open:
            hints.append(KeyHint("v", f"sql mode: {self._sql_mode.label}"))
        hints.append(KeyHint("g", "generate sql"))
        if self._focused_cell_is_expandable():
            hints.append(KeyHint("w", "expand cell"))
        hints.append(KeyHint("^c", f"copy {self._selection_label()} ({self._copy_format})"))
        if writable:
            hints.extend(
                (
                    KeyHint("enter", "edit cell"),
                    KeyHint("^v", "paste"),
                    KeyHint("^s", f"apply ({changes.summary if changes else 'nothing'})"),
                )
            )
        if self.is_read_only:
            hints.append(KeyHint("f5", "allow writes"))
        if self._view.is_filtered:
            hints.append(KeyHint("f", "filter"))
        elif self._window is not None and self._window.has_more:
            hints.append(KeyHint("m", "fetch more"))
        else:
            hints.append(KeyHint("r", "reload"))
        hints.append(KeyHint("esc", "back to tables"))
        hints.append(KeyHint("^p", "commands"))
        return tuple(hints)

    def _screen_title(self) -> str:
        summary = self._summary
        if self._window is None:
            return f"{summary.ref} · loading…"
        if self.is_read_only:
            verdict = "read-only (session)"
        elif self._table is not None and not self._table.updatable:
            verdict = "read-only (no key)"
        else:
            verdict = "read/write"
        pending = "" if self._changes is None or self._changes.is_empty else " · pending"
        return f"{summary.ref} · {status_text(self._window)} · {verdict}{pending}"

    # -- connection loss (FR-10) --------------------------------------------

    def _handle_connection_lost(self, error: Exception) -> None:
        """Offer a reconnect, keeping every staged change (FR-10).

        The staging area is **never** cleared here. It is the only copy of the user's
        unsaved work: the rows behind it are gone with the connection, so discarding it
        would lose edits that cannot be reconstructed, and an Apply that fails this way
        has written nothing (the transaction rolled back). Refreshing after the
        reconnect re-reads the rows and re-attaches the staged changes on top.

        What the user is told matters as much as what is preserved: the grid goes
        visibly stale rather than quietly showing yesterday's rows as if they were live.
        """
        changes = self._changes
        staged = changes.summary if changes is not None and not changes.is_empty else ""
        session = self.connection.session
        target = session.label if session else "the server"
        message = f"the connection to {target} was lost"
        detail = (
            f"{message}.\n\nYour staged changes are safe and still here"
            f"{f' ({staged})' if staged else ''} — reconnect to continue."
        )
        self._connection_lost = True
        self.status.show_message(f"{message} — staged changes preserved", level="error")
        self.push(
            ConfirmScreen(
                detail,
                title="Connection lost",
            ),
            self._on_reconnect_answered,
        )

    def _on_reconnect_answered(self, confirmed: bool | None) -> None:
        """Reconnect when the user accepts; otherwise stay put with the work kept."""
        self._connection_lost = False
        if not confirmed:
            self.report_warning(
                "not reconnected — your staged changes are kept; press esc and reconnect "
                "from the connection list when you are ready"
            )
            return
        self.reconnect()

    def reconnect(self) -> None:
        """Re-establish the session with the same profile, then re-read the table.

        Reloading the metadata and rows afterwards is what re-attaches the grid to the
        live data while the staging area keeps the user's pending changes layered on
        top — the two are separate, which is why the work survives the round trip.
        """
        profile_name = self.connection.session.profile_name if self.connection.session else None
        if profile_name is None:
            self.report_error("cannot reconnect: the session is gone", title="Reconnect")
            return
        try:
            profile = self.connection.get_profile(profile_name)
        except Exception as exc:
            self.report_error(f"cannot reconnect: {exc}", title="Reconnect")
            return

        async def reconnect_worker() -> None:
            try:
                await self.connection.connect(profile)
            except Exception as exc:
                self.report_error(f"reconnect failed: {exc}", title="Reconnect")
                return
            # The old cache describes a dead session, so it goes.
            self.services.catalog.invalidate()
            live = self.connection.session
            self.status.show_message(f"reconnected to {live.label if live else profile.name}")
            self.load_metadata()  # re-read rows; staging is preserved by ChangeService

        self.run_worker(
            reconnect_worker(),
            name="reconnect",
            group=self.WORKER_GROUP,
            exit_on_error=False,
        )

    @property
    def connection_lost(self) -> bool:
        """Whether the session dropped while this table was open (FR-10)."""
        return self._connection_lost

    def _show_disconnected(self) -> None:
        """Reconnect path instead of an empty grid when the session is gone (FR-10)."""
        self.status.show_message("no active connection — press [b]esc[/b] to go back")


def grid_is_new(screen: TableEditorScreen, row_index: int) -> bool:
    """True when the grid row at ``row_index`` is a staged INSERT."""
    grid = screen.query_one("#editor-grid", DataGrid)
    return grid.is_new_row(row_index)


def _render(value: object) -> str:
    """The text form of a parsed value, for the service's text-based staging path."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, bytes | bytearray | memoryview):
        return "0x" + bytes(value).hex()
    return str(value)
