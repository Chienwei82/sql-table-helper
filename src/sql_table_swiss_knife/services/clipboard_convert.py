"""Clipboard text conversion: encoding, parsing and per-column normalization (FR-4).

Split out of :mod:`~sql_table_swiss_knife.services.clipboard`, which now holds only the
paste *plan*. The seam is the one the feature already had: this half turns text into rows
of cell text and converts each value against the column's type and the user's locale; the
other half decides what those rows will do to the table.

Pure, like its sibling — no I/O, no terminal, no database — so a value's conversion is
testable on its own. Three decisions are the ones a naive implementation gets wrong:

* **Quoted fields.** A cell may legitimately contain a tab or a newline (Excel quotes it).
  The parser is RFC 4180 shaped for both delimiters, so a multi-line cell stays *one* cell
  instead of shattering the block into rows.
* **Locale.** ``1.234,56`` and ``31.01.2026`` are ordinary German text and nonsense as SQL.
  ``number_locale`` / ``date_format`` convert them *before* validation, so the configured
  convention is honoured rather than reported as a type error.
* **NULL.** ``NULL`` as pasted text means SQL NULL by default, but an ``nvarchar`` column
  may legitimately *contain* the word — so it is configurable (``null_token`` /
  ``null_as_literal``).
"""

import csv
import io
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

from ..domain.catalog import Column
from ..infra.errors import AppError
from ..storage.settings import Settings

__all__ = [
    "ClipboardBlock",
    "ClipboardOptions",
    "ClipboardParseError",
    "PasteBlockFormat",
    "detect_format",
    "encode_block",
    "is_text_column",
    "normalize_text",
    "parse_block",
    "value_to_text",
]


class ClipboardParseError(AppError):
    """The pasted payload cannot be parsed at all (e.g. malformed JSON)."""


class PasteBlockFormat(Enum):
    """Delimiter family a pasted block arrived in (detected from the payload)."""

    TSV = "tsv"
    CSV = "csv"
    JSON = "json"
    SINGLE = "single"


@dataclass(frozen=True, slots=True)
class ClipboardOptions:
    """Every knob the user can turn for copy/paste, gathered into one value.

    The defaults are the shipped ``Settings`` defaults, so constructing this with no
    arguments gives the behaviour a fresh install has.
    """

    #: Text written for SQL NULL on copy (FR-4.2); ``""`` by default.
    null_repr: str = ""
    #: Text that means SQL NULL on paste (FR-4.6); ``""`` disables the token.
    null_token: str = "NULL"
    #: When True ``null_token`` is pasted as ordinary text instead of NULL.
    null_as_literal: bool = False
    #: ``en`` (1,234.56) or ``de`` (1.234,56) number conventions.
    number_locale: str = "en"
    #: ``iso`` / ``dmy`` / ``mdy`` date conventions.
    date_format: str = "iso"
    #: Refuse blocks with more rows than this instead of staging them.
    max_rows: int = 5000

    def next_copy_format(self, current: str) -> str:
        """The next format in the TSV → CSV → JSON cycle (DESIGN §9.2 ``p``)."""
        order = ("tsv", "csv", "json")
        try:
            position = order.index(current)
        except ValueError:
            return order[0]
        return order[(position + 1) % len(order)]

    @classmethod
    def from_settings(cls, settings: Settings) -> ClipboardOptions:
        """Build the options from the user's ``settings.toml`` (DESIGN §13.2)."""
        return cls(
            null_repr=settings.copy_null_repr,
            null_token=settings.paste_null_token,
            null_as_literal=settings.paste_null_as_literal,
            number_locale=settings.paste_number_locale,
            date_format=settings.paste_date_format,
            max_rows=settings.paste_max_rows,
        )


@dataclass(frozen=True, slots=True)
class ClipboardBlock:
    """A parsed paste payload: rows of cell *text*, plus how it was detected.

    The text is kept raw rather than converted: converting is a *decision* (which locale,
    is this NULL, does it fit the column) and it belongs to the planner, where the user
    can see it before anything is staged.
    """

    rows: tuple[tuple[str, ...], ...]
    kind: PasteBlockFormat = PasteBlockFormat.SINGLE
    #: The first row, when it was recognized as column *names* rather than data.
    header: tuple[str, ...] | None = None
    #: True when the source rows were not all the same width.
    ragged: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    @property
    def is_single_cell(self) -> bool:
        """One row of one cell: paste *into* the focused cell (FR-4.4)."""
        return len(self.rows) == 1 and len(self.rows[0]) == 1

    @property
    def is_empty(self) -> bool:
        return not self.rows or all(not cell for row in self.rows for cell in row)

    def data_rows(self) -> tuple[tuple[str, ...], ...]:
        """The rows to map, i.e. without a detected header row."""
        return self.rows[1:] if self.header is not None else self.rows


#: Windows Excel writes a BOM; it is not part of the first column's name.
_BOM = "﻿"
#: Normalize CRLF/CR line endings and drop NUL bytes, which no cell can hold.
_LINE_SPLIT = re.compile(r"\r\n?|\x00")
_THOUSANDS_EN = re.compile(r"(?<=\d),(?=\d{3}\b)")
_THOUSANDS_DE = re.compile(r"(?<=\d)\.(?=\d{3}\b)")


def parse_block(
    text: str,
    *,
    known_columns: Sequence[str] = (),
) -> ClipboardBlock:
    """Parse pasted ``text`` into rows of cell text.

    The delimiter family is detected rather than asked for, because the user pasted
    *something* and expects it to work: a tab makes it TSV (what Excel and Sheets produce),
    a leading ``[``/``{`` makes it JSON, a comma or quote makes it CSV, and anything else
    is one value pasted into one cell (FR-4.4).

    A UTF-8 BOM and CRLF line endings are normalized away, and ragged rows are padded so a
    block that lost its last columns mid-copy still maps cleanly — the missing cells become
    empty strings, which the NULL rules then decide.
    """
    cleaned = _LINE_SPLIT.sub("\n", text).lstrip(_BOM)
    if not cleaned.strip():
        return ClipboardBlock((), PasteBlockFormat.SINGLE)
    kind = detect_format(cleaned)
    if kind is PasteBlockFormat.JSON:
        rows, header = _parse_json(cleaned)
    else:
        delimiter = "\t" if kind is PasteBlockFormat.TSV else ","
        rows, header = _parse_delimited(cleaned, delimiter, known_columns)
    width = max((len(row) for row in rows), default=0)
    ragged = any(len(row) != width for row in rows)
    padded = tuple(tuple(row) + ("",) * (width - len(row)) for row in rows)
    return ClipboardBlock(padded, kind, header, ragged)


def detect_format(text: str) -> PasteBlockFormat:
    """Which delimiter family ``text`` looks like (see :func:`parse_block`)."""
    stripped = text.strip()
    if stripped[:1] in {"[", "{"}:
        return PasteBlockFormat.JSON
    first_line = stripped.split("\n", 1)[0]
    if "\t" in first_line:
        return PasteBlockFormat.TSV
    if "," in first_line or '"' in first_line:
        return PasteBlockFormat.CSV
    if "\n" in stripped:
        # Newlines but no delimiter: several rows of a single column (a vertical paste).
        return PasteBlockFormat.TSV
    return PasteBlockFormat.SINGLE


def _parse_delimited(
    text: str, delimiter: str, known_columns: Sequence[str]
) -> tuple[tuple[tuple[str, ...], ...], tuple[str, ...] | None]:
    """RFC 4180 parsing for both delimiters, plus header detection."""
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, quotechar='"')
    rows = [tuple(row) for row in reader if row]
    if not rows:
        return (), None
    header = rows[0] if _looks_like_header(rows[0], known_columns) else None
    return tuple(rows), header


def _looks_like_header(row: Sequence[str], known_columns: Sequence[str]) -> bool:
    """True when the row looks like column *names* rather than data (FR-4.6).

    The test is "most of the cells name columns of this table", not "all of them": an Excel
    export of five columns of ours plus two of a join usually carries a header the table
    cannot place, and demanding a perfect match would silently paste ``Code`` and ``Name``
    *as data* — writing the words into the first two columns. A row where only a minority of
    the cells match is still treated as data, which is the case that matters (a value that
    happens to equal a column name).
    """
    if not row or not known_columns:
        return False
    lowered = {name.lower() for name in known_columns}
    cells = [cell.strip() for cell in row if cell.strip()]
    if not cells:
        return False
    matches = sum(1 for cell in cells if cell.lower() in lowered)
    return matches >= max(1, (len(cells) + 1) // 2)


def _parse_json(text: str) -> tuple[tuple[tuple[str, ...], ...], tuple[str, ...] | None]:
    """JSON payloads: an array of objects, an array of arrays, or one scalar/object.

    Malformed JSON raises :class:`ClipboardParseError` rather than producing a confusing
    single-cell plan: the user needs to know their JSON was wrong, not that one cell will
    change.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClipboardParseError(f"this does not look like valid JSON: {exc}") from exc
    if isinstance(document, list):
        if not document:
            return (), None
        if all(isinstance(item, dict) for item in document):
            header: list[str] = []
            for item in document:
                for name in item:
                    if name not in header:
                        header.append(str(name))
            rows = tuple(tuple(_json_cell(item.get(name)) for name in header) for item in document)
            return rows, tuple(header)
        # A JSON array of scalars is one cell per row, and a mixed array pads each row out
        # to the block width. Iterating a scalar element directly would raise TypeError,
        # which is not a failure mode the user can act on.
        return _rows_of_cells(document), None
    if isinstance(document, dict):
        single_header = tuple(str(name) for name in document)
        return (tuple(_json_cell(value) for value in document.values()),), single_header
    return ((str(document),),), None


def _rows_of_cells(document: Sequence[object]) -> tuple[tuple[str, ...], ...]:
    """A JSON array rendered as block rows: scalars become one cell, lists become a row.

    Every row is padded to the width of the widest, matching how the delimited parsers
    build a rectangular block, so a ragged document cannot produce a short row.
    """
    rows = [
        (_json_cell(item),)
        if not isinstance(item, list | tuple)
        else tuple(_json_cell(value) for value in item)
        for item in document
    ]
    width = max((len(row) for row in rows), default=0)
    return tuple(row + ("",) * (width - len(row)) for row in rows)


def _json_cell(value: object) -> str:
    """One JSON value as cell text; ``null`` becomes an empty cell (→ SQL NULL)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return json.dumps(value)


def value_to_text(value: object, *, null_repr: str = "") -> str:
    """The text a value is copied as (FR-4.2).

    ``None`` becomes ``null_repr`` — empty by default, so a spreadsheet shows a blank
    cell, or the literal ``NULL`` when the user prefers to see it. Bytes go out as hex
    (the only lossless text form) and booleans as ``1``/``0``, matching what the grid shows
    and what the cell editor accepts back.
    """
    if value is None:
        return null_repr
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, bytes | bytearray | memoryview):
        return "0x" + bytes(value).hex()
    if isinstance(value, Decimal):
        # ``Decimal("1E+2")`` would copy as ``1E+2``, which no spreadsheet reads back.
        return format(value, "f")
    return str(value)


def encode_block(
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    fmt: str = "tsv",
    include_header: bool = False,
    options: ClipboardOptions | None = None,
) -> str:
    """Encode a rectangular block as TSV, CSV or JSON (FR-4.2).

    TSV is the default because it pastes straight into Excel and Sheets. Quoting follows
    RFC 4180 for both text formats — a value containing the delimiter, a quote or a newline
    is quoted and its quotes doubled — which is exactly what Excel writes and therefore what
    :func:`parse_block` reads back. JSON emits an array of objects keyed by column name (or
    a bare array of arrays without a header), because that is the form a person or a script
    can read back.
    """
    opts = options or ClipboardOptions()
    if fmt == "json":
        document: object = (
            [dict(zip(columns, row, strict=False)) for row in rows]
            if include_header
            else [list(row) for row in rows]
        )
        return json.dumps(document, indent=2, ensure_ascii=False, default=str)
    delimiter = "\t" if fmt == "tsv" else ","
    buffer = io.StringIO(newline="")
    writer = csv.writer(
        buffer,
        delimiter=delimiter,
        quotechar='"',
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\n",
    )
    if include_header:
        writer.writerow(list(columns))
    for row in rows:
        writer.writerow([value_to_text(value, null_repr=opts.null_repr) for value in row])
    return buffer.getvalue().rstrip("\n")


def normalize_text(text: str, column: Column, options: ClipboardOptions) -> str:
    """Rewrite pasted text into the column's canonical form before validation.

    Only the *shape* of the text changes — whether the result is acceptable is still
    decided by :func:`~services.validation.validate_input`, which owns the column rules:

    * the NULL token (case-insensitively) becomes the empty string, which validation reads
      as SQL NULL — unless ``null_as_literal`` is set, in which case the word stays text;
    * numbers lose their thousands separators and, for ``de``, trade their comma for a
      decimal point (``1.234,56`` → ``1234.56``);
    * dates are re-ordered into ISO for ``dmy``/``mdy`` (``31.01.2026`` → ``2026-01-31``),
      accepting ``.``, ``-`` and ``/`` as separators because all three occur in the wild.
    """
    stripped = text.strip()
    if (
        options.null_token
        and not options.null_as_literal
        and stripped.upper() == options.null_token.upper()
    ):
        return ""
    kind = column.data_type.lower()
    if kind in _NUMERIC_TYPES:
        return _normalize_number(stripped, options.number_locale)
    if kind in _DATE_TYPES:
        return _normalize_date(stripped, options.date_format)
    if kind in _DATETIME_TYPES:
        day, rest = _split_date_time(stripped)
        converted = _normalize_date(day, options.date_format)
        return f"{converted}T{rest}" if rest else converted
    return stripped


#: Column types whose text is a number, and so gets locale treatment.
_NUMERIC_TYPES = {
    "tinyint",
    "smallint",
    "int",
    "bigint",
    "decimal",
    "numeric",
    "money",
    "smallmoney",
    "float",
    "real",
}
_TEXT_TYPES = {
    "char",
    "varchar",
    "nchar",
    "nvarchar",
    "text",
    "ntext",
    "xml",
    "uniqueidentifier",
}
_DATE_TYPES = {"date"}
_DATETIME_TYPES = {"datetime", "datetime2", "smalldatetime", "datetimeoffset"}


def _normalize_number(text: str, locale: str) -> str:
    """Strip thousands separators and localize the decimal point."""
    if not text:
        return text
    body = text
    negative = False
    if body.startswith("(") and body.endswith(")"):
        # Accounting notation for negatives: (1.234,56) → -1234.56
        body, negative = body[1:-1].strip(), True
    if locale == "de":
        body = _THOUSANDS_DE.sub("", body).replace(",", ".")
    else:
        body = _THOUSANDS_EN.sub("", body)
    return f"-{body}" if negative and not body.startswith("-") else body


def _split_date_time(text: str) -> tuple[str, str]:
    """Split ``2026-01-31 12:30:00`` into its date and time halves."""
    for separator in (" ", "T"):
        if separator in text:
            head, _, tail = text.partition(separator)
            return head.strip(), tail.strip()
    return text.strip(), ""


def _normalize_date(text: str, date_format: str) -> str:
    """Re-order a day-first / month-first date into ISO ``YYYY-MM-DD``."""
    if date_format == "iso" or not text:
        return text
    parts = re.split(r"[./-]", text)
    if len(parts) != 3:
        return text  # a time, or something unrecognizable: leave it to validation
    first, second, third = (part.strip() for part in parts)
    if not (first.isdigit() and second.isdigit() and third.isdigit()):
        return text
    if len(first) == 4:  # already year-first: only the separators differ
        return text
    day, month = (first, second) if date_format == "dmy" else (second, first)
    try:
        parsed = date(int(third), int(month), int(day))
    except ValueError:
        return text  # 31.02.2026 is not a date; validation will say so properly
    return parsed.isoformat()


def is_text_column(column: Column | None) -> bool:
    """True when the column stores text, so a newline inside it is data rather than a row.

    The planning half needs this to decide whether a multi-line cell is one value or
    several, which is why it is a function over the column rather than a bare set the
    caller has to reach into.
    """
    return column is not None and column.data_type.lower() in _TEXT_TYPES
