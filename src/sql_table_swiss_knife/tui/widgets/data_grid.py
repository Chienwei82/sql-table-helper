"""Data grid: frozen headers, badges, cell states and the staged-value overlay (FR-3).

The grid wraps Textual's virtualized ``DataTable`` so scrolling stays cheap with the
1000-row default page (NFR-2). It owns presentation only:

* headers carry the column badges and the compact type (:func:`inspector.header_label`);
* identity/computed/rowversion columns are marked ``🔒`` and never enter the edit path (S-3);
* rows without a usable key render dimmed — they are read-only (S-4);
* every cell state is expressed as **glyph + colour**, never colour alone (FR-3.7).

The staged overlay is the M5 part: a row's displayed value is ``staged ?? original``, and
the cell's class/glyph comes from :class:`CellState`. The *rules* live in
:class:`services.changes.CellStatus`; this widget only maps them onto CSS classes, so the
decisions stay testable without a terminal.
"""

from collections.abc import Mapping, Sequence
from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import DataTable

from ...domain.catalog import Column, Table
from ...domain.changes import is_new_row
from ...domain.rows import Row, RowKey
from ...services.changes import CellStatus
from ...services.inspector import format_cell_value, header_label

__all__ = ["STATE_GLYPHS", "CellState", "DataGrid", "row_key_for"]


def row_key_for(table: Table, row: Mapping[str, object]) -> RowKey | None:
    """The identity of a fetched row, or ``None`` when the table has no usable key (S-4)."""
    identity = table.identity_columns
    if not identity or any(name not in row for name in identity):
        return None
    return tuple((name, row[name]) for name in identity)


class CellState:
    """The visual state of a cell/row (FR-3.7).

    Named constants rather than an enum because they double as Textual CSS class names;
    the glyph in :data:`STATE_GLYPHS` travels with the state so the meaning survives
    every theme and any colour-vision difference.
    """

    UNCHANGED = "unchanged"
    MODIFIED = "modified"
    NEW = "new"
    DELETED = "deleted"
    INVALID = "invalid"
    READ_ONLY = "readonly"


#: Glyph per state, appended to a changed/decorated cell (FR-3.7).
STATE_GLYPHS: dict[str, str] = {
    CellState.UNCHANGED: "",
    CellState.MODIFIED: "~",
    CellState.NEW: "+",
    CellState.DELETED: "×",  # noqa: RUF001 - DESIGN §9.4 specifies this glyph
    CellState.INVALID: "!",
    CellState.READ_ONLY: "🔒",
}

#: Service status → widget state. The service owns the decision, the widget owns the look.
_STATUS_TO_STATE: dict[CellStatus, str] = {
    CellStatus.UNCHANGED: CellState.UNCHANGED,
    CellStatus.MODIFIED: CellState.MODIFIED,
    CellStatus.NEW: CellState.NEW,
    CellStatus.DELETED: CellState.DELETED,
    CellStatus.READ_ONLY: CellState.READ_ONLY,
}


#: Sentinel for "no staged value for this cell".
_MISSING = object()


class DataGrid(DataTable[str]):
    """The row grid of the table editor screen."""

    DEFAULT_CSS = """
    DataGrid {
        height: 1fr;
        width: 1fr;
    }
    DataGrid > .datatable--header {
        background: $panel;
        text-style: bold;
    }
    DataGrid > .datatable--cursor {
        background: $primary 30%;
    }
    DataGrid.-readonly > .datatable--body {
        color: $nullable;
    }
    /* Cell-state component classes: colour reinforces the glyph, never replaces it
       (FR-3.7). They are component classes rather than CSS classes on cells because
       Textual's DataTable renders cells itself — a cell is a value, not a widget. */
    DataGrid > .datatable--cell--modified {
        color: $warning;
        text-style: italic;
    }
    DataGrid > .datatable--cell--new {
        color: $success;
    }
    DataGrid > .datatable--cell--deleted {
        color: $error;
        text-style: strike;
    }
    DataGrid > .datatable--cell--invalid {
        background: $error 30%;
    }
    DataGrid > .datatable--cell--readonly {
        color: $nullable;
    }
    """

    #: Component classes for the staged cell states, so their colours follow the theme.
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        *DataTable.COMPONENT_CLASSES,
        "datatable--cell--modified",
        "datatable--cell--new",
        "datatable--cell--deleted",
        "datatable--cell--invalid",
        "datatable--cell--readonly",
    }

    def __init__(self, **kwargs: object) -> None:
        super().__init__(zebra_stripes=True, cursor_type="cell", **kwargs)  # type: ignore[arg-type]
        self._table: Table | None = None
        self._columns: tuple[Column, ...] = ()
        self._rows: list[Row] = []
        self._keys: list[RowKey | None] = []
        self._hidden: frozenset[str] = frozenset()
        self._states: dict[Coordinate, str] = {}

    # -- data ---------------------------------------------------------------

    @property
    def frozen_count(self) -> int:
        """How many leading columns are frozen (the PK, or nothing)."""
        return self.fixed_columns

    def set_frozen(self, count: int) -> None:
        """Freeze the first ``count`` columns so they stay put on a wide table.

        Freezing the identity columns is what makes a 60-column table usable: without
        it, scrolling right to read a description loses the very column that says which
        row you are looking at. Textual's own ``fixed_columns`` does the work, so the
        freeze is real (it survives sorting and cursor movement) rather than a repaint.
        """
        # Textual asserts 0 <= count <= total columns; clamping here keeps the widget
        # total when the column set changes under it (a reload with a hidden column).
        self.fixed_columns = max(0, min(count, len(self._columns)))

    def load(
        self,
        table: Table,
        rows: Sequence[Row],
        *,
        hidden: frozenset[str] = frozenset(),
        keys: Sequence[RowKey | None] | None = None,
    ) -> None:
        """Replace the contents with ``table``'s headers and ``rows``.

        The table is remembered so :meth:`column_at` can answer "what is the metadata of
        the focused column?" — the question the inspector panel asks on every cursor move.
        ``hidden`` drops columns from the *view* without changing the fetched rows, so a
        hidden column's staged value is still tracked and still applied. ``keys`` supplies
        the row identities explicitly, which a staged INSERT needs: it has no fetched values
        to derive one from, so the caller passes the synthetic key the staging area gave it.
        """
        self.clear(columns=True)
        self._table = table
        self._hidden = hidden
        self._columns = tuple(column for column in table.columns if column.name not in hidden)
        for column in self._columns:
            self.add_column(header_label(table, column), key=column.name)
        self.set_frozen(self._frozen_count(table, hidden))
        self.set_rows(rows, keys=keys)

    def _frozen_count(self, table: Table, hidden: frozenset[str]) -> int:
        """How many leading *visible* columns are the row identity.

        Counted in visible columns, not metadata: hiding the PK column has to move the
        freeze with it, otherwise the grid would freeze an arbitrary data column and
        quietly break the guarantee the freeze exists for.
        """
        identity = [name for name in table.identity_columns if name not in hidden]
        # Only a *leading* run can be frozen; an identity column that sits in the
        # middle of a wide table cannot be, so freezing nothing is the honest answer.
        leading = 0
        for column in self._columns:
            if column.name in identity:
                leading += 1
            else:
                break
        return min(leading, len(self._columns))

    def set_rows(self, rows: Sequence[Row], *, keys: Sequence[RowKey | None] | None = None) -> None:
        """Replace the row set, keeping the headers (reload, filter, apply, undo).

        ``keys`` overrides the derived row identities where the caller knows better.
        """
        self.clear()
        self.clear_states()
        self._rows = list(rows)
        derived = [
            row_key_for(self._table, row.values) if self._table is not None else None
            for row in self._rows
        ]
        self._keys = list(keys) if keys is not None else derived
        for row in self._rows:
            self.add_row(*self.render_row(row), key=None)
        if self._table is not None:
            self.set_class(not self._table.updatable, "-readonly")

    def append_rows(self, rows: Sequence[Row]) -> None:
        """Add more rows without rebuilding the headers ("fetch more", FR-3.8)."""
        for row in rows:
            self._rows.append(row)
            self._keys.append(
                row_key_for(self._table, row.values) if self._table is not None else None
            )
            self.add_row(*self.render_row(row), key=None)

    def render_row(self, row: Row) -> tuple[str, ...]:
        """The display text of one row, in visible-column order.

        ``NULL`` is upper-case and distinct (FR-3.1). The state glyph is appended by
        :meth:`apply_overlay`, so a cell with no staged change looks exactly as it did
        before M5.
        """
        return tuple(format_cell_value(row.get(column.name)) for column in self._columns)

    def rerender_row(self, index: int) -> None:
        """Re-render one row in place (a stage, revert or undo changed it)."""
        if not 0 <= index < len(self._rows):
            return
        for column_index, text in enumerate(self.render_row(self._rows[index])):
            self.update_cell_at(Coordinate(index, column_index), text, update_width=False)

    # -- the staged overlay -------------------------------------------------

    def apply_overlay(
        self,
        statuses: Mapping[tuple[RowKey | None, str], str],
        values: Mapping[tuple[RowKey | None, str], object] | None = None,
    ) -> None:
        """Render staged values and cell states over the fetched rows (FR-3.7).

        Args:
            statuses: ``(row key, column) → CellState`` for the cells that are not plain.
            values: ``(row key, column) → staged value`` for the cells whose display
                differs from what was fetched.
        """
        overlays = values or {}
        for row_index, key in enumerate(self._keys):
            row = self._rows[row_index]
            for column_index, column in enumerate(self._columns):
                state = statuses.get((key, column.name), CellState.UNCHANGED)
                staged = overlays.get((key, column.name), _MISSING)
                text = format_cell_value(row.get(column.name) if staged is _MISSING else staged)
                glyph = STATE_GLYPHS.get(state, "")
                rendered = f"{text} {glyph}".strip() if glyph else text
                self._write_cell(row_index, column_index, rendered, state)

    def _write_cell(self, row_index: int, column_index: int, text: str, state: str) -> None:
        """Write one cell's value, styled by its state, and remember the state.

        A ``DataTable`` cell is a value rather than a widget, so a cell's colour travels
        with the value: a plain string for an unchanged cell, a Rich ``Text`` carrying the
        state's style otherwise. The style is resolved from this widget's own component
        classes, so the colours follow the active theme (``$warning``, ``$success``…)
        instead of being hard-coded here. The glyph always accompanies the colour (FR-3.7).
        """
        coordinate = Coordinate(row_index, column_index)
        # ``DataTable``'s stub types cells as ``str``, but its renderer passes any
        # renderable through ``default_cell_formatter``, which keeps a styled ``Text``
        # intact — that is how a cell's colour can differ from its row's.
        cell: str | Text = (
            text if state == CellState.UNCHANGED else Text(text, style=self._state_style(state))
        )
        self.update_cell_at(coordinate, cell, update_width=False)  # type: ignore[arg-type]
        self._states[coordinate] = state

    def _state_style(self, state: str) -> Style:
        """The Rich style for a cell state, resolved from its component class."""
        if state == CellState.UNCHANGED:
            return Style.null()
        return self.get_component_rich_style(f"datatable--cell--{state}", default=Style.null())

    def cell_state(self, row_index: int, column_index: int) -> str:
        """The state last rendered for one cell (``CellState.UNCHANGED`` before any overlay)."""
        return self._states.get(Coordinate(row_index, column_index), CellState.UNCHANGED)

    def clear_states(self) -> None:
        """Forget every rendered state (a reload replaces the rows wholesale)."""
        self._states.clear()

    # -- metadata access ----------------------------------------------------

    @property
    def table(self) -> Table | None:
        """The table whose rows are shown, or ``None`` before the first load."""
        return self._table

    @property
    def visible_columns(self) -> tuple[Column, ...]:
        """The columns currently rendered, in display order."""
        return self._columns

    @property
    def fetched_rows(self) -> tuple[Row, ...]:
        """The fetched rows the grid is rendering (the domain rows, not Textual's map)."""
        return tuple(self._rows)

    def key_at(self, row_index: int) -> RowKey | None:
        """Identity of the row at ``row_index``, or ``None`` for a read-only row (S-4)."""
        if 0 <= row_index < len(self._keys):
            return self._keys[row_index]
        return None

    def column_at(self, column_index: int) -> Column | None:
        """Metadata of the column under ``column_index``, or ``None`` when out of range."""
        if not 0 <= column_index < len(self._columns):
            return None
        return self._columns[column_index]

    def is_new_row(self, row_index: int) -> bool:
        """True when the row at ``row_index`` is a staged INSERT."""
        return is_new_row(self.key_at(row_index))

    def state_for(self, column_index: int) -> str:
        """Cell state of the focused cell: read-only wins over "unchanged" (S-3/S-4)."""
        column = self.column_at(column_index)
        if column is None or self._table is None:
            return CellState.UNCHANGED
        if column.is_server_managed or not self._table.updatable:
            return CellState.READ_ONLY
        return CellState.UNCHANGED
