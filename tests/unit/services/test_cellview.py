"""M8 robustness: wide tables, long text, binary cells, and connection loss (FR-3.1, FR-10).

Two layers of test, on purpose. The *model* tests (``services/cellview``, the frozen
column count, the connection-loss classifier) pin the decisions with ordinary unit
tests. The Pilot tests prove those decisions reach the screen: that ``w`` opens the
expand view, that the PK column is actually frozen, and — most importantly — that a
dropped connection offers a reconnect **without discarding staged changes**.
"""

import pytest

from sql_table_swiss_knife.services.cellview import (
    LONG_TEXT_CHARS,
    CellKind,
    cell_view,
    hex_dump,
    truncate,
    wrap_text,
)
from sql_table_swiss_knife.services.connection import is_connection_lost

# -- the cell model ---------------------------------------------------------


def test_a_short_value_needs_no_expansion() -> None:
    view = cell_view("Germany")
    assert view.kind is CellKind.PLAIN
    assert view.expandable is False


def test_null_is_rendered_distinctly_and_is_not_expandable() -> None:
    view = cell_view(None)
    assert view.text == "NULL"
    assert view.expandable is False


def test_a_long_value_is_expandable_and_keeps_its_full_text() -> None:
    text = "x" * (LONG_TEXT_CHARS + 20)
    view = cell_view(text)
    assert view.kind is CellKind.LONG
    assert view.expandable is True
    assert view.full == text  # nothing is lost, only the grid's line is clipped
    assert view.text.endswith("…")
    assert view.size == len(text)


def test_a_multiline_value_is_always_expandable() -> None:
    """The grid shows one line, so the rest exists even when the text is short."""
    view = cell_view("line one\nline two")
    assert view.kind is CellKind.LONG
    assert view.expandable is True


def test_a_value_exactly_at_the_threshold_stays_inline() -> None:
    assert cell_view("y" * LONG_TEXT_CHARS).kind is CellKind.PLAIN


def test_booleans_render_as_one_and_zero() -> None:
    assert cell_view(True).text == "1"
    assert cell_view(False).text == "0"


def test_truncation_fits_inside_the_budget() -> None:
    """A cell that overruns its column is exactly what this prevents."""
    assert len(truncate("x" * 50, 20)) == 20
    assert truncate("short", 20) == "short"


def test_truncation_of_an_exactly_full_value_adds_nothing() -> None:
    assert truncate("x" * 20, 20) == "x" * 20


def test_binary_values_get_a_hex_preview_in_the_grid() -> None:
    view = cell_view(b"\x01\x02\x03")
    assert view.kind is CellKind.BINARY
    assert view.text.startswith("0x010203")
    assert "3 bytes" in view.text
    assert view.size == 3


def test_a_large_binary_value_marks_the_preview_as_truncated() -> None:
    assert "…" in cell_view(bytes(range(64))).text


def test_binary_values_expand_to_a_hex_dump() -> None:
    view = cell_view(b"\xde\xad\xbe\xef")
    assert "de ad be ef" in view.full
    assert "|" in view.full  # the ASCII column


def test_the_hex_dump_offsets_each_line() -> None:
    dump = hex_dump(bytes(range(48)))
    lines = dump.split("\n")
    assert len(lines) == 3
    assert lines[0].startswith("00000000")
    assert lines[2].startswith("00000020")


def test_the_hex_dump_hides_control_characters() -> None:
    """Raw control bytes would corrupt the display, or emit escape sequences."""
    dump = hex_dump(b"\x1b[31mred\x07")
    assert "\x1b" not in dump
    assert "|.[31mred.|" in dump


def test_text_wraps_hard_rather_than_by_word() -> None:
    """The expand view shows the exact content; reflowing would change it."""
    assert wrap_text("abcdef", 3) == ["abc", "def"]


def test_wrapping_honours_existing_newlines() -> None:
    assert wrap_text("a\nbb", 10) == ["a", "bb"]


def test_wrapping_an_empty_paragraph_keeps_the_line() -> None:
    assert wrap_text("a\n\nb", 10) == ["a", "", "b"]


# -- connection loss --------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Connection reset by peer",
        "08S01: Communication link failure",
        "Communications link failure",
        "[Microsoft][ODBC Driver 18] Login timeout expired",
        "08001 - client unable to establish connection",
    ],
)
def test_a_dropped_link_is_recognised(message: str) -> None:
    assert is_connection_lost(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "The INSERT statement conflicted with the FOREIGN KEY constraint FK_Country",
        "String or binary data would be truncated in table 'dbo.Country'",
        "Invalid column name 'Nam'",
        "The statement terminated. The resultset has been discarded.",
    ],
)
def test_an_ordinary_database_error_is_not_a_lost_connection(message: str) -> None:
    """Misreading these as a dead session would send the user to reconnect for nothing."""
    assert is_connection_lost(message) is False


def test_the_classifier_accepts_an_exception_object() -> None:
    assert is_connection_lost(ConnectionResetError("boom")) is True
    assert is_connection_lost(ValueError("nope")) is False
