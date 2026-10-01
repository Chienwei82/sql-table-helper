"""Paste *planning*: what a pasted block will do to a table (FR-4, DESIGN §9.5).

This module is **pure**: it has no I/O, no terminal and no database. Everything the
clipboard feature decides lives here as plain functions over plain values, which is what
makes "what will this paste do?" answerable *before* anything is staged.

The text half — encoding, parsing and per-column value conversion — lives in
:mod:`~sql_table_swiss_knife.services.clipboard_convert`. This half answers the remaining
question: **given** a parsed :class:`~clipboard_convert.ClipboardBlock` and the target
columns, which rows will be UPDATEd and which INSERTed?

* :func:`choose_mode` picks cell / fill / rows from the shape of the block against the
  selection.
* :func:`plan_paste` maps the block onto the target columns, converts each value against
  the column's declared type *and the user's locale settings*, and classifies every row as
  UPDATE (a key that exists) or INSERT (a new row).

The grid enters as *values plus keys* (:class:`PasteTarget`), never as a widget: that is
what keeps "what will this paste do?" a pure function of data, testable without a
terminal, and impossible to get out of step with what the Paste Preview dialog shows.

Two decisions are worth calling out, because each is a documented choice rather than an
accident:

* **All-or-nothing.** One bad cell in a paste stages *nothing*; a half-pasted block is
  worse than a rejected one.
* **Row key from cells.** A positional (headerless) paste supplies no key, so it is read
  from the target's own identity columns. A row whose key does not match an existing row
  is an INSERT — never an UPDATE "match everything", which is fragile with duplicates and
  NULLs.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ..domain.catalog import Column, Table
from ..domain.rows import RowKey
from .clipboard_convert import (
    ClipboardBlock,
    ClipboardOptions,
    ClipboardParseError,
    PasteBlockFormat,
    detect_format,
    encode_block,
    is_text_column,
    normalize_text,
    parse_block,
    value_to_text,
)
from .validation import Hint, validate_input

__all__ = [
    "CellPlan",
    # Re-exported so the clipboard feature still has one import path for a caller; the
    # implementations moved to clipboard_convert with the rest of the text handling.
    "ClipboardBlock",
    "ClipboardOptions",
    "ClipboardParseError",
    "PasteBlockFormat",
    "PasteMode",
    "PastePlan",
    "PasteRowPlan",
    "PasteTarget",
    "RowOutcome",
    "choose_mode",
    "detect_format",
    "encode_block",
    "normalize_text",
    "parse_block",
    "plan_paste",
    "value_to_text",
]


class PasteMode(Enum):
    """How a block should land in the grid (FR-4.4 to FR-4.6)."""

    #: One value into the focused cell (FR-4.4).
    CELL = "cell"
    #: A block over existing cells (FR-4.5).
    FILL = "fill"
    #: Whole rows: UPDATE where the key matches, INSERT otherwise (FR-4.6).
    ROWS = "rows"


class RowOutcome(Enum):
    """What will happen to one pasted row — the UPDATE/INSERT split FR-4.6 asks for."""

    UPDATE = "update"
    INSERT = "insert"


@dataclass(frozen=True, slots=True)
class CellPlan:
    """One target cell: the column, the pasted text, the converted value and any error."""

    column: str
    text: str
    value: object
    ok: bool
    hints: tuple[Hint, ...] = ()

    @property
    def error(self) -> str:
        """The first blocking message, or ``""``."""
        for hint in self.hints:
            if hint.is_error:
                return hint.text
        return ""

    @property
    def converted(self) -> str:
        """The value as it will be stored — the preview's "type conversion" column."""
        return value_to_text(self.value)


@dataclass(frozen=True, slots=True)
class PasteRowPlan:
    """One pasted row: which row it lands on and whether it updates or inserts."""

    index: int
    outcome: RowOutcome
    #: Row index in the grid; ``None`` for an INSERT, which gets a brand new row.
    target_row: int | None
    key: RowKey | None
    values: tuple[CellPlan, ...]

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(cell.error for cell in self.values if cell.error)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class PastePlan:
    """The complete, reviewable answer to "what will this paste do?".

    Nothing is staged when this exists — the Paste Preview dialog renders it, and only a
    confirmed plan reaches :meth:`services.changes.ChangeService.stage_paste`. A plan with
    errors is not staged at all, which is FR-4.6's "nothing is staged half-parsed".
    """

    mode: PasteMode
    rows: tuple[PasteRowPlan, ...]
    #: ``source name → target column``; empty when the mapping is purely positional.
    mapping: tuple[tuple[str, str], ...] = ()
    #: Target columns the block does not touch (left as they are).
    untouched: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    #: True when a header row was mapped by name rather than by position.
    used_header: bool = False

    @property
    def updates(self) -> tuple[PasteRowPlan, ...]:
        return tuple(row for row in self.rows if row.outcome is RowOutcome.UPDATE)

    @property
    def inserts(self) -> tuple[PasteRowPlan, ...]:
        return tuple(row for row in self.rows if row.outcome is RowOutcome.INSERT)

    @property
    def cell_count(self) -> int:
        return sum(len(row.values) for row in self.rows)

    @property
    def errors(self) -> tuple[str, ...]:
        """Every blocking message, row-prefixed so the user can find the cell."""
        return tuple(f"row {row.index + 1}: {error}" for row in self.rows for error in row.errors)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        """``2 updates, 3 inserts · 15 cells`` for the preview's title line."""
        parts: list[str] = []
        for rows, word in ((self.updates, "update"), (self.inserts, "insert")):
            if rows:
                parts.append(f"{len(rows)} {word}{'s' if len(rows) != 1 else ''}")
        head = ", ".join(parts) if parts else "nothing to paste"
        return f"{head} · {self.cell_count} cell(s)"

    def describe_mapping(self) -> str:
        """``Name → Name, Population → Population`` / ``positional``."""
        if self.mapping:
            return ", ".join(f"{source} → {target}" for source, target in self.mapping)
        return "positional"


@dataclass(frozen=True, slots=True)
class PasteTarget:
    """Where a paste lands: the table, the anchor cell and the rows already loaded.

    The grid arrives as *values plus keys* rather than as a widget, so planning stays a
    pure function of data. ``keys[i]`` is the row's identity, or ``None`` for a row with no
    usable key — such a row is read-only (S-4) and can never be the target of an UPDATE.
    """

    table: Table
    #: Visible columns in display order — what positional mapping walks.
    columns: tuple[Column, ...]
    #: The loaded rows (staged values already overlaid by the caller).
    rows: tuple[Mapping[str, object], ...]
    #: Row identity per loaded row; ``None`` where the row has none.
    keys: tuple[RowKey | None, ...] = ()
    #: The grid cursor as ``(row index, column index)``.
    anchor_row: int = 0
    anchor_column: int = 0
    #: An explicit selection as ``(top, left, bottom, right)``, inclusive, or ``None``.
    selection: tuple[int, int, int, int] | None = None

    def row_key(self, index: int) -> RowKey | None:
        if 0 <= index < len(self.keys):
            return self.keys[index]
        return None

    def key_index(self) -> dict[RowKey, int]:
        """Existing identities mapped to their grid row (for UPDATE detection)."""
        return {key: index for index, key in enumerate(self.keys) if key is not None}

    def column_index(self, name: str) -> int | None:
        for index, column in enumerate(self.columns):
            if column.name == name:
                return index
        return None


def choose_mode(target: PasteTarget, block: ClipboardBlock) -> PasteMode:
    """Decide whether a paste fills cells or lands as rows (FR-4.5/4.6).

    The rule follows what the user is looking at:

    * one cell of text → that cell (FR-4.4);
    * a rectangular block matching an explicit selection → that selection (FR-4.5);
    * anything with several rows → rows, so a multi-line paste never silently overwrites
      the cells below the cursor.

    A single value pasted over a multi-cell selection fills the whole selection, which is
    what spreadsheets do and what people expect when they copy one cell and paste it across.
    """
    selection = target.selection
    if selection is not None and _fills_selection(block, selection):
        # A selection always wins, including for a single value: pasting one cell across a
        # range is what every spreadsheet does, and it is the only way a paste can *not*
        # mean "insert rows" when the user has visibly marked a range.
        return PasteMode.FILL
    if block.is_single_cell:
        return PasteMode.CELL
    if block.column_count == 1 and _is_text_column(target, target.anchor_column):
        # Newlines down one column are a *value* with line breaks, not rows to insert —
        # pasting a description into an nvarchar cell must not create N rows.
        return PasteMode.CELL
    return PasteMode.ROWS


def _is_text_column(target: PasteTarget, index: int) -> bool:
    """True when the column at ``index`` stores text (so newlines are data, not rows)."""
    return is_text_column(_column_at(target, index))


def _fills_selection(block: ClipboardBlock, selection: tuple[int, int, int, int]) -> bool:
    """True when the block's shape matches the selection — or is one cell filling it."""
    top, left, bottom, right = selection
    span = (bottom - top + 1, right - left + 1)
    return (block.row_count, block.column_count) in {span, (1, 1)}


def plan_paste(
    target: PasteTarget,
    block: ClipboardBlock,
    *,
    options: ClipboardOptions | None = None,
    mode: PasteMode | None = None,
) -> PastePlan:
    """Map ``block`` onto ``target`` and convert every value — staging nothing.

    Args:
        target: The table, the visible columns and the loaded rows.
        block: The parsed clipboard payload.
        options: Locale / NULL / row-limit settings; defaults to the shipped ones.
        mode: Force a mode; by default :func:`choose_mode` picks one.

    Returns:
        A :class:`PastePlan` whose ``errors`` are per-cell and blocking: the caller shows
        them and stages nothing.
    """
    opts = options or ClipboardOptions()
    chosen = mode or choose_mode(target, block)
    notes: list[str] = []
    if block.row_count > opts.max_rows:
        notes.append(
            f"the block has {block.row_count} rows, over the paste_max_rows limit "
            f"of {opts.max_rows}"
        )
        return PastePlan(chosen, (), notes=tuple(notes))
    if block.is_empty:
        return PastePlan(chosen, (), notes=("the pasted text is empty",))
    if chosen is PasteMode.CELL:
        return _plan_cell(target, block, opts)
    columns, mapping, untouched, used_header = _resolve_columns(target, block, chosen, notes)
    if chosen is PasteMode.FILL:
        rows = _plan_fill(target, block, columns, opts)
    else:
        rows = _plan_rows(target, block, columns, opts, notes)
    return PastePlan(
        chosen,
        rows,
        mapping=mapping,
        untouched=untouched,
        notes=tuple(notes),
        used_header=used_header,
    )


def _plan_cell(target: PasteTarget, block: ClipboardBlock, opts: ClipboardOptions) -> PastePlan:
    """One value into the focused cell — including a multi-line string (FR-4.4).

    A single-column payload stays *one* cell: a value containing newlines is a legitimate
    ``nvarchar`` value, and splitting it into rows would insert rows nobody asked for.
    """
    column = _column_at(target, target.anchor_column)
    if column is None:
        return PastePlan(PasteMode.CELL, (), notes=("there is no column at the cursor",))
    text = (
        "\n".join(row[0] for row in block.rows)
        if block.column_count == 1
        else "\n".join("\t".join(row) for row in block.rows)
    )
    row = PasteRowPlan(
        0,
        RowOutcome.UPDATE,
        target.anchor_row,
        target.row_key(target.anchor_row),
        _convert_row(target.table, text, (column,), opts),
    )
    return PastePlan(
        PasteMode.CELL,
        (row,),
        mapping=((column.name, column.name),),
        untouched=tuple(c.name for c in target.columns if c.name != column.name),
    )


def _plan_fill(
    target: PasteTarget,
    block: ClipboardBlock,
    columns: tuple[Column | None, ...],
    opts: ClipboardOptions,
) -> tuple[PasteRowPlan, ...]:
    """Fill existing cells from the anchor (or the selection) across the block (FR-4.5)."""
    top, _, bottom, _ = target.selection or (
        target.anchor_row,
        target.anchor_column,
        target.anchor_row + block.row_count - 1,
        target.anchor_column + block.column_count - 1,
    )
    # One pasted value fills the whole selection (across and down); a block fills it
    # cell-for-cell and stops at the end of the range (FR-4.5).
    source_rows = block.data_rows()
    span = list(range(top, bottom + 1)) or [top]
    plans: list[PasteRowPlan] = []
    for offset, row_index in enumerate(span):
        if row_index >= len(target.rows):
            break  # the grid ends here: the rest of the block is not pasted
        if block.is_single_cell:
            text_row = source_rows[0]
            source = _broadcast_row(text_row, columns)
        else:
            if offset >= len(source_rows):
                break
            text_row = source_rows[offset]
            source = text_row
        plans.append(
            PasteRowPlan(
                offset,
                RowOutcome.UPDATE,
                row_index,
                target.row_key(row_index),
                _convert_block_row(target.table, source, columns, opts),
            )
        )
    return tuple(plans)


def _broadcast_row(text_row: Sequence[str], columns: Sequence[Column | None]) -> tuple[str, ...]:
    """Repeat one pasted value across a whole row (the single-cell fill, FR-4.5)."""
    value = text_row[0] if text_row else ""
    return tuple(value for _ in columns)


def _plan_rows(
    target: PasteTarget,
    block: ClipboardBlock,
    columns: tuple[Column | None, ...],
    opts: ClipboardOptions,
    notes: list[str],
) -> tuple[PasteRowPlan, ...]:
    """Land whole rows: UPDATE the ones whose key exists, INSERT the rest (FR-4.6)."""
    existing = target.key_index()
    identity = target.table.identity_columns
    mapped = tuple(column.name for column in columns if column is not None)
    keyed = bool(identity) and all(name in mapped for name in identity)
    notes.append(
        f"rows carrying {', '.join(identity)} will UPDATE a matching row"
        if keyed
        else (
            f"the block does not carry every key column ({', '.join(identity) or 'none'}) "
            "— every row is INSERTed"
        )
    )
    plans: list[PasteRowPlan] = []
    for offset, text_row in enumerate(block.data_rows()):
        cells = _convert_block_row(target.table, text_row, columns, opts)
        key = _row_key_from_cells(identity, cells) if keyed else None
        row_index = existing.get(key) if key is not None else None
        plans.append(
            PasteRowPlan(
                offset,
                RowOutcome.UPDATE if row_index is not None else RowOutcome.INSERT,
                row_index,
                key,
                cells,
            )
        )
    return tuple(plans)


def _row_key_from_cells(identity: Sequence[str], cells: Sequence[CellPlan]) -> RowKey | None:
    """Build the row identity from the pasted key cells, or ``None`` if one is missing."""
    by_name = {cell.column: cell for cell in cells}
    pairs: list[tuple[str, object]] = []
    for name in identity:
        cell = by_name.get(name)
        if cell is None or not cell.ok or cell.value is None:
            return None  # an incomplete key cannot match an existing row
        pairs.append((name, cell.value))
    return tuple(pairs) if pairs else None


def _convert_block_row(
    table: Table,
    text_row: Sequence[str],
    columns: Sequence[Column | None],
    opts: ClipboardOptions,
) -> tuple[CellPlan, ...]:
    """Convert one source row against the resolved columns, skipping the skipped ones."""
    return tuple(
        _convert_cell(table, column, text_row[index] if index < len(text_row) else "", opts)
        for index, column in enumerate(columns)
        if column is not None
    )


def _convert_row(
    table: Table, text: str, columns: Sequence[Column], opts: ClipboardOptions
) -> tuple[CellPlan, ...]:
    return tuple(_convert_cell(table, column, text, opts) for column in columns)


def _convert_cell(table: Table, column: Column, text: str, opts: ClipboardOptions) -> CellPlan:
    """Normalize then validate one cell; the plan carries the text, the value and errors.

    The *same* :func:`~services.validation.validate_input` the cell editor uses decides
    acceptability, so a pasted value is refused in exactly the cases typing it would be.
    """
    parsed = validate_input(table, column, normalize_text(text, column, opts))
    return CellPlan(column.name, text, parsed.value, parsed.ok, parsed.hints)


def _resolve_columns(
    target: PasteTarget, block: ClipboardBlock, mode: PasteMode, notes: list[str]
) -> tuple[tuple[Column | None, ...], tuple[tuple[str, str], ...], tuple[str, ...], bool]:
    """Which target column each source cell goes to, and what is left untouched.

    A detected header row maps *by name* (FR-4.6: an Excel header row must not be pasted as
    data), which also lets the block skip columns the table does not have. Otherwise the
    mapping is positional, starting at the anchor column for a fill and at the first column
    for a row insert — FR-4.6's "map positionally to visible/editable columns".
    """
    if block.header is not None:
        columns: list[Column | None] = []
        pairs: list[tuple[str, str]] = []
        matched: set[str] = set()
        for name in block.header:
            column = target.table.column_or_none(name)
            if column is None:
                columns.append(None)
                notes.append(f"there is no column named {name!r} — that column is ignored")
                continue
            if column.is_server_managed:
                columns.append(None)
                notes.append(f"{column.name} is server-managed (S-3) — not written")
                continue
            columns.append(column)
            matched.add(column.name)
            pairs.append((name, column.name))
        untouched = tuple(c.name for c in target.columns if c.name not in matched)
        return tuple(columns), tuple(pairs), untouched, True
    start = target.anchor_column if mode is PasteMode.FILL else 0
    columns = [
        target.columns[start + index] if start + index < len(target.columns) else None
        for index in range(block.column_count)
    ]
    if mode is PasteMode.ROWS:
        # A positional row insert must never write identity/computed columns implicitly:
        # the user has not opted into IDENTITY_INSERT (S-3), and a computed column cannot
        # be written at all.
        #
        # The row *key* is deliberately NOT excluded here. `identity_columns` is row
        # identity (the PK, else a single-column UNIQUE — see domain/catalog.py), not the
        # SQL Server IDENTITY property, so S-3 does not apply to it. Excluding it silently
        # dropped the key from every positional block, which turned "update these rows"
        # into "insert duplicates of them": the pasted key no longer matched anything.
        # `_plan_rows` decides UPDATE vs INSERT from the mapped columns and reports which.
        columns = [
            None if column is not None and column.is_server_managed else column
            for column in columns
        ]
    named = tuple(
        (f"column {index + 1}", column.name)
        for index, column in enumerate(columns)
        if column is not None
    )
    untouched = tuple(
        column.name for column in target.columns if column.name not in {n for _, n in named}
    )
    return tuple(columns), named, untouched, False


def _column_at(target: PasteTarget, index: int) -> Column | None:
    if 0 <= index < len(target.columns):
        return target.columns[index]
    return None
