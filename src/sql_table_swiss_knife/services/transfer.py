"""CSV/JSON import and export for the current table, through the staged pipeline (M7).

Export and import deliberately share the *same* code path as the clipboard
(:mod:`sql_table_swiss_knife.services.clipboard`): a file is just a paste whose source you
chose, and an export is a copy whose destination you chose. That means the parsing, NULL,
locale and validation rules cannot differ between "pasted from Excel" and "read from
``rows.csv``" — and, more importantly, **import never writes to the database**. A file
becomes a :class:`~services.clipboard.PastePlan` that the Paste Preview dialog shows and
the user confirms, exactly like a clipboard paste (FR-4.6).

Export is the mirror image: the rows on screen (staged values included) are encoded with
the same :func:`~services.clipboard.encode_block` the clipboard uses, so a CSV on disk and
a TSV in the clipboard cannot disagree about how a NULL or a date is written.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..infra.errors import AppError
from .clipboard import ClipboardBlock, ClipboardOptions, encode_block, parse_block

__all__ = [
    "TransferError",
    "export_rows",
    "read_block",
    "render_rows_for_export",
    "suffix_format",
    "write_text",
]

#: File suffix → format. An unknown suffix is decided by sniffing the content.
SUFFIX_FORMATS = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".tab": "tsv",
    ".txt": "tsv",
    ".json": "json",
}


class TransferError(AppError):
    """The file could not be read, written or understood."""


def suffix_format(path: Path) -> str:
    """The format implied by the file's suffix, or ``""`` when the name says nothing."""
    return SUFFIX_FORMATS.get(path.suffix.lower(), "")


def render_rows_for_export(
    rows: Sequence[Mapping[str, object]], columns: Sequence[str]
) -> list[list[object]]:
    """Project row mappings onto the requested column order.

    A missing key becomes ``None`` (→ NULL) rather than raising: a hidden or absent column
    must not stop someone exporting the rows they can see.
    """
    return [[row.get(name) for name in columns] for row in rows]


def export_rows(
    columns: Sequence[str],
    rows: Sequence[Mapping[str, object]],
    *,
    fmt: str = "csv",
    include_header: bool = True,
    options: ClipboardOptions | None = None,
) -> str:
    """The text of an export: the same encoding the clipboard uses (FR-4.2)."""
    return encode_block(
        columns,
        render_rows_for_export(rows, columns),
        fmt=fmt,
        include_header=include_header,
        options=options,
    )


def write_text(path: Path, text: str) -> Path:
    """Write ``text`` to ``path``, UTF-8, adding a BOM for CSV.

    Excel only reads UTF-8 CSV as UTF-8 when there is a BOM, so the CSV writer gets one;
    TSV and JSON do not, and a stray BOM there would corrupt the first column's name.
    """
    encoding = "utf-8-sig" if path.suffix.lower() == ".csv" else "utf-8"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
    except OSError as exc:
        raise TransferError(f"cannot write {path}: {exc}") from exc
    return path


def read_block(path: Path, *, known_columns: Sequence[str] = ()) -> ClipboardBlock:
    """Read a CSV/TSV/JSON file into a :class:`ClipboardBlock`, ready to be planned.

    ``known_columns`` is the target table's column names, passed through to the parser so a
    header row is detected and mapped by name exactly as it would be for a paste.

    The result is a *block*, not a plan: mapping, conversion and validation stay in
    :func:`~services.clipboard.plan_paste`, so a file and a clipboard paste are reviewed in
    exactly the same way and cannot diverge.

    The BOM is stripped on read (``utf-8-sig``) so an Excel-written CSV maps its header row
    by name instead of failing to match the first column.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise TransferError(f"{path} does not exist") from exc
    except UnicodeDecodeError as exc:
        raise TransferError(f"{path} is not UTF-8 text (a UTF-8 CSV is expected): {exc}") from exc
    except OSError as exc:
        raise TransferError(f"cannot read {path}: {exc}") from exc
    # The format was sniffed from the content by parse_block itself; the suffix only tells
    # the *writer* what to produce, so nothing here needs to override it.
    return parse_block(text, known_columns=known_columns)


def looks_like_json(path: Path, sample: str = "") -> bool:
    """Whether a file should be read as JSON — used when the suffix is unknown."""
    if suffix_format(path) == "json":
        return True
    return sample.lstrip()[:1] in {"[", "{"}


def describe_export(path: Path, row_count: int, fmt: str) -> str:
    """``wrote 5 rows to /tmp/x.csv as csv`` — the status line after an export."""
    return f"exported {row_count} row(s) to {path} as {fmt}"


def json_preview(text: str) -> str:
    """Validate and pretty-print a JSON payload (used by the import dialog's hint)."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransferError(f"invalid JSON: {exc}") from exc
    return json.dumps(document, indent=2, ensure_ascii=False)
