"""Cell inspection: the model behind the expand view (FR-3.1, M8).

A grid cell is one character wide in practice — it has to fit a column, and a column
has to fit many of them. That is fine for a code or a flag and useless for a
description, a JSON blob or a binary column. This module decides **what kind of cell
the user is looking at** and **how it should be expanded**, so the decision is testable
without a terminal and the same rule drives the grid's own truncation marker.

Three kinds, chosen from the *value* rather than the column type, because that is what
the user is actually looking at:

``LONG``
    text whose full form would be cut off — expands to a wrapping, scrollable view.
``BINARY``
    bytes — expands to a hex dump, because a binary column has no readable form and
    "0x01 (4 bytes)" is not a value anybody can check.
``PLAIN``
    everything else; no expand binding is offered, so ``enter`` on a short cell never
    opens an empty dialog.

The length threshold matters on wide catalog tables: a column holding a paragraph per
row is common, and it is exactly the case where the grid must say "there is more here"
instead of silently hiding it.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TypeGuard

__all__ = [
    "BINARY_PREVIEW_BYTES",
    "DEFAULT_TRUNCATE_WIDTH",
    "LONG_TEXT_CHARS",
    "CellKind",
    "CellView",
    "cell_view",
    "hex_dump",
    "is_binary",
    "truncate",
    "wrap_text",
]


class CellKind(Enum):
    """What the user is looking at, and therefore what expanding it should do."""

    PLAIN = "plain"
    LONG = "long"
    BINARY = "binary"


#: A text cell longer than this is worth expanding (a paragraph, not a sentence).
LONG_TEXT_CHARS = 80

#: Bytes shown in the grid's inline preview before it says "…".
BINARY_PREVIEW_BYTES = 8

#: Grid column width at which a single-line cell is considered truncated.
DEFAULT_TRUNCATE_WIDTH = 20


def is_binary(value: object) -> TypeGuard[bytes | bytearray | memoryview]:
    """Whether ``value`` is a byte-ish value needing a hex view."""
    return isinstance(value, bytes | bytearray | memoryview)


def truncate(text: str, width: int = DEFAULT_TRUNCATE_WIDTH) -> str:
    """Clip ``text`` to ``width`` characters, marking that it was cut.

    The ellipsis is inside the budget so the result is exactly ``width`` wide — a cell
    that overruns its column is what this exists to prevent.
    """
    if width <= 0 or len(text) <= width:
        return text
    return text[: width - 1] + "…"


def wrap_text(text: str, width: int = DEFAULT_TRUNCATE_WIDTH * 2) -> list[str]:
    """Hard-wrap ``text`` into lines of at most ``width`` characters.

    A hard wrap rather than a word wrap on purpose: the expand view is for reading
    exact content (a SQL fragment, a JSON document), and reflowing words would silently
    change what the user believes they are looking at. Existing newlines are honoured.
    """
    if width <= 0:
        return [text]
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        lines.extend(paragraph[index : index + width] for index in range(0, len(paragraph), width))
    return lines


def hex_dump(data: bytes, *, width: int = 16) -> str:
    """A classic offset / hex / ASCII dump of ``data``.

    The ASCII column matters: a hex dump of a UUID or a rowversion is unreadable
    without it, and those are the bytes people actually come here to check.
    Non-printable bytes render as ``.`` rather than as raw control characters, which
    would otherwise corrupt the display — or emit terminal escape sequences.
    """
    lines: list[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        hex_part = " ".join(f"{byte:02x}" for byte in chunk).ljust(width * 3 - 1)
        ascii_part = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        lines.append(f"{offset:08x}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CellView:
    """How one cell should be presented, and what expanding it shows.

    ``text`` is what the grid shows and ``full`` is what the expand view shows. Both
    are pre-rendered here so neither the grid nor the dialog makes a formatting
    decision at paint time.
    """

    kind: CellKind
    #: The one-line form for the grid (already truncated, with its marker).
    text: str
    #: The complete value for the expand view (the hex dump, for binary).
    full: str
    #: Size of the underlying value, when meaningful (bytes, or characters).
    size: int | None = None

    @property
    def expandable(self) -> bool:
        """Whether expanding this cell would show anything the grid does not."""
        return self.kind is not CellKind.PLAIN

    @property
    def summary(self) -> str:
        """A short type/size line for the expand view's header."""
        if self.kind is CellKind.BINARY:
            return f"binary · {self.size} byte(s)"
        if self.kind is CellKind.LONG:
            return f"text · {self.size} character(s)"
        return "value"


def cell_view(value: object, *, width: int = DEFAULT_TRUNCATE_WIDTH) -> CellView:
    """Build the :class:`CellView` for one fetched value.

    Args:
        value: The raw value from the provider.
        width: The grid's column width, which decides what counts as truncated.

    Returns:
        A :class:`CellView`. ``None`` renders as the distinct ``NULL`` the grid already
        uses elsewhere, and is never expandable: there is nothing more to show.
    """
    if value is None:
        return CellView(CellKind.PLAIN, "NULL", "NULL")
    if is_binary(value):
        raw = bytes(value)
        preview = raw[:BINARY_PREVIEW_BYTES].hex()
        text = f"0x{preview}{'…' if len(raw) > BINARY_PREVIEW_BYTES else ''} ({len(raw)} bytes)"
        return CellView(CellKind.BINARY, truncate(text, width * 2), hex_dump(raw), len(raw))
    if isinstance(value, bool):
        text = "1" if value else "0"
        return CellView(CellKind.PLAIN, text, text)
    text = str(value)
    # A multi-line value is always expandable: the grid shows one line of it, so the
    # rest exists whether or not it exceeds the character threshold.
    kind = CellKind.LONG if len(text) > LONG_TEXT_CHARS or "\n" in text else CellKind.PLAIN
    return CellView(kind, truncate(text, width), text, len(text))
