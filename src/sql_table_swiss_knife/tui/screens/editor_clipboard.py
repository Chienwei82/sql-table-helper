"""The editor screen's clipboard half: copy, paste, import and export (FR-4).

Extracted from ``table_editor.py`` as a mixin. The screen keeps its ``BINDINGS`` — Textual
only collects those from the concrete screen class, so bindings declared on a mixin are
silently ignored, which a probe confirmed — while the actions they name come from here.

The mixin reaches back into the screen for the staging buffer, the selection and the
``_after_stage`` redraw, so it is only meaningful on ``TableEditorScreen``. What it owns is
the question "what happens when the user copies or pastes?", which was a coherent concern
inside a 1672-line screen: it builds the :class:`PasteTarget` from plain grid values, asks
``services.clipboard`` what the block will do, shows the preview, and stages the answer.
"""

import asyncio
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from textual.coordinate import Coordinate
from textual.events import Paste

from ...domain.catalog import Column, Table
from ...services.changes import ChangeService
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
from ...services.transfer import (
    TransferError,
    export_rows,
    read_block,
    suffix_format,
    write_text,
)
from ..widgets import DataGrid
from .base import AppScreen, owning_app
from .paste_preview import PastePreviewScreen
from .transfer_path import PathPromptScreen

if TYPE_CHECKING:
    # Type-check against the real screen so ``query_one``/``push``/``run_worker``/
    # ``status`` resolve to their real signatures. At runtime the mixin bases on
    # ``object``: it is mixed into AppScreen, which already supplies all of them, and
    # re-declaring them here would shadow the real methods and lose their signatures.
    _EditorBase = AppScreen
else:
    _EditorBase = object

__all__ = ["ClipboardMixin"]

_T = TypeVar("_T")


class ClipboardMixin(_EditorBase):
    """Copy, paste, import and export for the editor screen (FR-4).

    Only the *private* helpers the mixin calls on its host are declared, and only under
    ``TYPE_CHECKING``. Two things are deliberately not re-declared:

    * ``query_one`` / ``push`` / ``run_worker`` / ``status`` are real ``Widget`` methods.
      A stub here would shadow the screen's own — MRO puts the mixin first — and raise
      ``NotImplementedError`` at runtime, which is exactly what the first attempt did.
    * ``_EditorBase`` is the screen while type-checking and ``object`` at runtime, so the
      inherited methods keep their real signatures without the mixin inheriting a second
      copy of Textual's machinery.
    """

    # -- what the host screen supplies (for the type checker only) -----------
    if TYPE_CHECKING:
        _table: Table | None
        _changes: ChangeService | None
        _copy_scope: str
        _copy_format: str
        _selection_anchor: Coordinate | None
        _export_columns: tuple[Column, ...]

        def _render_rows(self) -> None: ...
        def _original_values(self, row_index: int) -> dict[str, object]: ...
        def _display_values(self, row_index: int) -> dict[str, object]: ...
        def _require_writable(self) -> bool: ...
        def _after_stage(
            self, ok: bool, message: str, rows: Collection[int] | None = None
        ) -> None: ...

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
