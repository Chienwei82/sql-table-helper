"""SQL panel / generation for the table editor (FR-5).

: class:`EditorSqlMixin` owns everything that *shows* SQL: the preview panel,
the mode cycle, copying, and generating a statement for the focused row. It
never executes anything (S-1) — generation renders text into the panel. The
screen keeps ``BINDINGS`` (Textual only collects them from the concrete screen)
and the staging state; this mixin only reads them through the host stubs below.
"""

from typing import TYPE_CHECKING

from ...domain.catalog import Table
from ...domain.rows import RowKey
from ...services.sqlpreview import (
    RowAction,
    SqlMode,
    SqlPreview,
    entries_for,
    generate_for,
    next_mode,
)
from ..widgets import DataGrid, SqlPanel
from ..widgets.sql_panel import ModeChosen
from .base import AppScreen
from .sql_action import SqlActionScreen

if TYPE_CHECKING:
    from ...services.changes import ChangeService
    from ...services.sqlpreview import AppliedRun

    _EditorBase = AppScreen
else:
    _EditorBase = object

__all__ = ["EditorSqlMixin"]


class EditorSqlMixin(_EditorBase):
    """SQL preview, copy and generation for :class:`TableEditorScreen`."""

    if TYPE_CHECKING:
        _table: Table | None
        _changes: ChangeService | None
        _sql_mode: SqlMode
        _sql_open: bool
        _sql_generated: tuple[str, str] | None
        _applied_run: AppliedRun | None
        _applied_keys: tuple[RowKey, ...]

        @property
        def grid(self) -> DataGrid: ...

        def _cursor(self) -> tuple[RowKey | None, object, int, int]: ...
        def _display_values(self, row_index: int) -> dict[str, object]: ...
        def refresh_hints(self) -> None: ...
        @property
        def is_read_only(self) -> bool: ...

    # -- SQL panel (FR-5) ---------------------------------------------------

    @property
    def sql_panel(self) -> SqlPanel:
        """The SQL preview panel."""
        return self.query_one("#editor-sql", SqlPanel)

    def build_preview(self) -> SqlPreview:
        """The panel's content for the current staging state, in the current mode.

        Built on demand rather than cached, so the panel can never show SQL for changes
        that are no longer staged: every re-render derives it from the ``ChangeSet``.
        The last successful Apply rides along so the panel can show what ran once
        nothing is staged anymore (FR-5.5).
        """
        table = self._table
        changes = self._changes
        dialect = self.connection.provider().dialect
        if table is None:
            return SqlPreview(
                table=None, dialect=dialect, mode=self._sql_mode, applied=self._applied_run
            )
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
            applied=self._applied_run,
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
            self.grid.focus()
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
        try:
            sql = self.generate_sql(action)
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

    def generate_sql(self, action: RowAction) -> str:
        """The SQL for ``action`` over the rows as the grid currently shows them.

        Values come from the staged overlay, not the fetched window: a generated
        statement must describe the edits the user has staged, or the panel would show
        SQL for values that Apply never uses.
        """
        table = self._table
        if table is None:
            raise ValueError("no table is loaded")
        key, _, row_index, _ = self._cursor()
        changes = self._changes
        return generate_for(
            self.connection.provider().dialect,
            table,
            action,
            key=key if action.needs_key else None,
            values=self._row_values(row_index) if action.needs_values else None,
            rows=self._all_row_values() if action.covers_table else (),
            identity_insert=changes is not None and changes.requires_identity_insert(),
        )

    def _row_values(self, row_index: int) -> dict[str, object] | None:
        """The values shown for the grid row at ``row_index``, staged edits included."""
        if not 0 <= row_index < self._row_count():
            return None
        return self._display_values(row_index)

    def _all_row_values(self) -> list[dict[str, object]]:
        """Every loaded row's values — the source for "insert script for all rows".

        Staged edits are included, because the SQL panel must show the values the grid
        shows: a script generated from the pre-edit rows would not match what Apply runs.
        """
        return [self._display_values(index) for index in range(self._row_count())]

    def _row_count(self) -> int:
        """How many rows the grid is showing (fetched plus staged new ones)."""
        return len(self.grid.fetched_rows)
