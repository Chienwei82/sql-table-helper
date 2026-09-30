"""CSV/JSON import and export (M7) — a file must behave exactly like a paste.

The rule these tests defend: transfer has no rules of its own. An export is the clipboard
encoder pointed at a file, and an import is the clipboard parser pointed at a file, so a
``NULL``, a German decimal comma and a header row mean the same thing whichever way the data
arrived (FR-4.2, FR-4.6).
"""

from pathlib import Path

import pytest

from sql_table_swiss_knife.domain import Column, PrimaryKey, Table, TableKind
from sql_table_swiss_knife.services.clipboard import ClipboardOptions, PasteTarget, plan_paste
from sql_table_swiss_knife.services.transfer import (
    TransferError,
    export_rows,
    read_block,
    render_rows_for_export,
    suffix_format,
    write_text,
)

COUNTRY = Table(
    schema="dbo",
    name="Country",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Code", 1, "char", 2, None, None, False, None, False, is_primary_key=True),
        Column("Name", 2, "nvarchar", 200, None, None, False, None, False),
        Column("Population", 3, "int", None, 10, 0, True, None, False),
    ),
    primary_key=PrimaryKey("PK_Country", ("Code",)),
)
COLUMNS = tuple(column.name for column in COUNTRY.columns)
ROWS = (
    {"Code": "DE", "Name": "Germany", "Population": 83_000_000},
    {"Code": "FR", "Name": "France", "Population": 67_000_000},
)


# -- export -----------------------------------------------------------------


def test_csv_export_has_a_header_and_the_rows_in_column_order() -> None:
    text = export_rows(COLUMNS, ROWS, fmt="csv")
    assert text.splitlines()[0] == "Code,Name,Population"
    assert text.splitlines()[1] == "DE,Germany,83000000"


def test_a_missing_column_exports_as_null_rather_than_failing() -> None:
    """A hidden or absent column must not stop exporting the rows you can see."""
    assert export_rows(("Code",), ({"Code": "DE", "Name": "Germany"},), fmt="csv") == "Code\nDE"


def test_export_projection_keeps_the_requested_order() -> None:
    projected = render_rows_for_export(ROWS, ("Name", "Code"))
    assert projected == [["Germany", "DE"], ["France", "FR"]]


def test_the_null_representation_is_shared_with_the_clipboard() -> None:
    options = ClipboardOptions(null_repr="NULL")
    text = export_rows(COLUMNS, ({"Code": "DE", "Name": None},), fmt="csv", options=options)
    assert text.splitlines()[1] == "DE,NULL,NULL"


def test_json_export_is_an_array_of_objects() -> None:
    import json

    document = json.loads(export_rows(COLUMNS, ROWS, fmt="json"))
    assert document[0]["Code"] == "DE"
    assert len(document) == 2


# -- suffix handling --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("rows.csv", "csv"),
        ("rows.CSV", "csv"),
        ("rows.tsv", "tsv"),
        ("rows.txt", "tsv"),
        ("rows.json", "json"),
        ("rows.dat", ""),
    ],
)
def test_the_suffix_decides_the_format(name: str, expected: str) -> None:
    assert suffix_format(Path(name)) == expected


# -- files on disk ----------------------------------------------------------


def test_csv_is_written_with_a_bom_so_excel_reads_utf8(tmp_path: Path) -> None:
    """Without the BOM Excel shows "Bavaria" as mojibake; the bytes must be there."""
    path = write_text(tmp_path / "countries.csv", "Name\nBaviera\n")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_json_and_tsv_are_written_without_a_bom(tmp_path: Path) -> None:
    """A BOM in JSON or TSV would corrupt the first column's name."""
    for name in ("countries.json", "countries.tsv"):
        path = write_text(tmp_path / name, "[]\n")
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_missing_parent_directories_are_created(tmp_path: Path) -> None:
    path = write_text(tmp_path / "nested" / "dir" / "out.csv", "Code\n")
    assert path.exists()


def test_an_excel_written_csv_round_trips_through_the_paste_pipeline(tmp_path: Path) -> None:
    """The end-to-end claim: a BOM + CRLF CSV file plans exactly like a pasted block."""
    path = tmp_path / "countries.csv"
    path.write_bytes("﻿Code,Name\r\nDE,Deutschland\r\nES,Spanien\r\n".encode())
    block = read_block(path, known_columns=COLUMNS)
    target = PasteTarget(
        table=COUNTRY,
        columns=COUNTRY.columns,
        rows=({"Code": "DE", "Name": "Germany"},),
        keys=((("Code", "DE"),),),
    )
    plan = plan_paste(target, block)
    assert plan.used_header
    assert [row.outcome.value for row in plan.rows] == ["update", "insert"]
    assert plan.rows[0].values[1].value == "Deutschland"


def test_reading_a_missing_file_says_which_file(tmp_path: Path) -> None:
    with pytest.raises(TransferError, match="does not exist"):
        read_block(tmp_path / "nope.csv")


def test_reading_a_binary_file_says_it_is_not_text(tmp_path: Path) -> None:
    path = tmp_path / "blob.csv"
    path.write_bytes(b"\xff\xfe\x00\x01binary")
    with pytest.raises(TransferError, match="not UTF-8"):
        read_block(path)


def test_a_json_file_parses_into_objects(tmp_path: Path) -> None:
    path = write_text(tmp_path / "rows.json", '[{"Code": "DE", "Name": "Germany"}]')
    block = read_block(path)
    assert block.kind.value == "json"
    assert block.header == ("Code", "Name")
    assert block.rows[0] == ("DE", "Germany")
