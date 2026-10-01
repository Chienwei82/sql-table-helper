"""Clipboard parsing, encoding, conversion and paste planning (FR-4, M7).

The whole point of these tests is that "what will this paste do?" is decided *before*
anything is staged, and that the awkward inputs are handled deliberately: quoted fields with
tabs and newlines, CRLF, BOMs, ragged rows, header rows that only look like headers, German
number/date conventions and the ``NULL`` token.

Everything here runs without a terminal and without a database — the module is pure by
design, so these are the cheapest possible regression net for the riskiest code in M7.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from sql_table_swiss_knife.domain import (
    CheckConstraint,
    Column,
    PrimaryKey,
    Table,
    TableKind,
    UniqueConstraint,
)
from sql_table_swiss_knife.services.clipboard import (
    CellPlan,
    ClipboardBlock,
    ClipboardOptions,
    ClipboardParseError,
    PasteBlockFormat,
    PasteMode,
    PasteRowPlan,
    PasteTarget,
    RowOutcome,
    choose_mode,
    detect_format,
    encode_block,
    normalize_text,
    parse_block,
    plan_paste,
    value_to_text,
)

#: A catalog-style table: char PK, a name, a population, a nullable note, plus a computed
#: column and a rowversion — the two that must never be written by a paste (S-3).
COUNTRY = Table(
    schema="dbo",
    name="Country",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Code", 1, "char", 2, None, None, False, None, False, is_primary_key=True),
        Column("Name", 2, "nvarchar", 200, None, None, False, None, False),
        Column("Population", 3, "int", None, 10, 0, True, None, False),
        Column("Note", 4, "nvarchar", 4000, None, None, True, None, False),
        Column(
            "NameUpper",
            5,
            "nvarchar",
            200,
            None,
            None,
            True,
            None,
            False,
            is_computed=True,
            computed_definition="UPPER([Name])",
        ),
        Column("RowVer", 6, "rowversion", 8, None, None, False, None, False, is_rowversion=True),
    ),
    primary_key=PrimaryKey("PK_Country", ("Code",)),
    unique_constraints=(UniqueConstraint("UQ_Country_Name", ("Name",)),),
    check_constraints=(CheckConstraint("CK_Country_Population", "([Population]>=(0))"),),
)

#: Decimal + date columns, for the locale battery.
METRICS = Table(
    schema="dbo",
    name="Metrics",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Id", 1, "int", None, 10, 0, False, None, True, is_primary_key=True),
        Column("Rate", 2, "decimal", None, 9, 2, False, None, False),
        Column("TakenOn", 3, "date", None, None, None, False, None, False),
        Column("SeenAt", 4, "datetime2", None, None, None, True, None, False),
    ),
    primary_key=PrimaryKey("PK_Metrics", ("Id",)),
)

EDITABLE = tuple(c for c in COUNTRY.columns if not c.is_server_managed)

#: Column names the screen passes to :func:`parse_block` for header detection.
NAMES = tuple(column.name for column in COUNTRY.columns)


def target(
    *,
    table: Table = COUNTRY,
    columns: tuple[Column, ...] = EDITABLE,
    rows: tuple[dict[str, object], ...] = (
        {"Code": "DE", "Name": "Germany", "Population": 83_000_000, "Note": None},
        {"Code": "FR", "Name": "France", "Population": 67_000_000, "Note": None},
        {"Code": "JP", "Name": "Japan", "Population": 125_000_000, "Note": None},
    ),
    anchor_row: int = 0,
    anchor_column: int = 0,
    selection: tuple[int, int, int, int] | None = None,
) -> PasteTarget:
    """A grid of the usual shape: loaded rows, their keys, and a cursor."""
    identity = table.identity_columns
    keys = tuple(tuple((name, row.get(name)) for name in identity) for row in rows)
    return PasteTarget(
        table=table,
        columns=columns,
        rows=rows,
        keys=keys,
        anchor_row=anchor_row,
        anchor_column=anchor_column,
        selection=selection,
    )


# -- format detection ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\tb\n1\t2", PasteBlockFormat.TSV),
        ("a,b\n1,2", PasteBlockFormat.CSV),
        ('a,b\n"x,y",2', PasteBlockFormat.CSV),
        ('[{"a": 1}]', PasteBlockFormat.JSON),
        ('{"a": 1}', PasteBlockFormat.JSON),
        ("hello", PasteBlockFormat.SINGLE),
        ("one\ntwo", PasteBlockFormat.TSV),
    ],
)
def test_the_delimiter_family_is_detected_from_the_payload(
    text: str, expected: PasteBlockFormat
) -> None:
    assert detect_format(text) is expected


def test_a_block_with_no_delimiter_is_a_single_cell() -> None:
    """FR-4.4: one value pasted into one cell, not a row of one-character rows."""
    block = parse_block("Bavaria")
    assert block.is_single_cell
    assert block.rows == (("Bavaria",),)


# -- parsing ---------------------------------------------------------------


def test_crlf_and_a_bom_are_normalized_away() -> None:
    """Windows Excel writes CRLF and a UTF-8 BOM; neither may reach a column name."""
    block = parse_block("\ufeffCode\tName\r\nDE\tGermany\r\n", known_columns=["Code", "Name"])
    assert block.rows == (("Code", "Name"), ("DE", "Germany"))
    assert block.header == ("Code", "Name")


def test_quoted_tsv_fields_keep_their_tab_and_newline() -> None:
    """RFC 4180 quoting: a cell with a tab or a newline stays ONE cell."""
    text = 'Code\tNote\n"DE"\t"a\tb"\n"FR"\t"line1\nline2"'
    block = parse_block(text, known_columns=["Code", "Note"])
    assert block.rows == (("Code", "Note"), ("DE", "a\tb"), ("FR", "line1\nline2"))


def test_doubled_quotes_inside_a_quoted_field_round_trip() -> None:
    block = parse_block('Code,Note\nDE,"say ""hi"""', known_columns=["Code", "Note"])
    assert block.rows[1] == ("DE", 'say "hi"')


def test_ragged_rows_are_padded_and_flagged() -> None:
    """A block that lost its last columns still maps; the flag says it happened."""
    block = parse_block("Code\tName\tPopulation\nDE\tGermany\nFR\tFrance\t67000000")
    assert block.ragged
    assert block.rows[1] == ("DE", "Germany", "")


def test_a_header_row_is_detected_when_most_cells_name_columns() -> None:
    """An export that carries a column we do not have still maps the ones we do."""
    assert parse_block("Code\tName\nDE\tGermany", known_columns=["Code", "Name"]).header == (
        "Code",
        "Name",
    )
    assert parse_block("Name\tNope\nGermany\tx", known_columns=["Code", "Name"]).header == (
        "Name",
        "Nope",
    )


def test_a_row_where_only_a_minority_match_is_still_data() -> None:
    """A value that happens to equal a column name must not be eaten as a header."""
    block = parse_block(
        "Code\tGermany\tx\nDE\tFrance\ty", known_columns=["Code", "Name", "Population"]
    )
    assert block.header is None
    assert block.data_rows()[0][0] == "Code"


def test_header_matching_is_case_insensitive() -> None:
    block = parse_block("code,name\nDE,Germany", known_columns=["Code", "Name"])
    assert block.header == ("code", "name")
    assert block.data_rows() == (("DE", "Germany"),)


def test_json_array_of_objects_maps_by_key_in_first_seen_order() -> None:
    block = parse_block('[{"Name": "Germany", "Code": "DE"}, {"Code": "FR", "Note": null}]')
    assert block.kind is PasteBlockFormat.JSON
    assert block.header == ("Name", "Code", "Note")  # union, in first-seen order
    assert block.rows[0] == ("Germany", "DE", "")
    assert block.rows[1] == ("", "FR", "")


def test_json_array_of_arrays_is_positional() -> None:
    block = parse_block('[["DE", "Germany"], ["FR", "France"]]')
    assert block.header is None
    assert block.rows == (("DE", "Germany"), ("FR", "France"))


def test_malformed_json_is_reported_rather_than_pasted_as_one_cell() -> None:
    with pytest.raises(ClipboardParseError, match="valid JSON"):
        parse_block("[{not json}]")


def test_empty_text_parses_to_an_empty_block() -> None:
    assert parse_block("   \n  ").is_empty


# -- encoding --------------------------------------------------------------


def test_tsv_is_the_default_and_needs_no_quoting_for_plain_values() -> None:
    text = encode_block(["Code", "Name"], [["DE", "Germany"], ["FR", "France"]])
    assert text == "DE\tGermany\nFR\tFrance"


def test_a_value_containing_the_delimiter_is_quoted_on_the_way_out() -> None:
    """Excel quotes tabs/newlines; we must do the same or the round-trip loses a cell."""
    text = encode_block(["Code", "Note"], [["DE", "a\tb"], ["FR", "line1\nline2"]])
    assert text.splitlines()[0] == 'DE\t"a\tb"'
    parsed = parse_block(text)
    assert parsed.rows[0] == ("DE", "a\tb")


def test_csv_uses_commas_and_a_header_when_asked() -> None:
    text = encode_block(["Code", "Name"], [["DE", "Germany"]], fmt="csv", include_header=True)
    assert text == "Code,Name\nDE,Germany"


def test_json_export_is_objects_with_a_header_and_arrays_without() -> None:
    rows = [["DE", "Germany"]]
    assert encode_block(["Code", "Name"], rows, fmt="json", include_header=True) == (
        '[\n  {\n    "Code": "DE",\n    "Name": "Germany"\n  }\n]'
    )
    assert (
        encode_block(["Code", "Name"], rows, fmt="json")
        == '[\n  [\n    "DE",\n    "Germany"\n  ]\n]'
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (True, "1"),
        (False, "0"),
        (b"\x01\x02", "0x0102"),
        (Decimal("1E+2"), "100"),
        (42, "42"),
    ],
)
def test_values_are_copied_as_the_text_the_cell_editor_accepts(
    value: object, expected: str
) -> None:
    assert value_to_text(value) == expected


def test_null_copies_as_the_configured_representation() -> None:
    """FR-4.2: empty by default, or the literal word so NULL is visible in a spreadsheet."""
    assert value_to_text(None) == ""
    assert value_to_text(None, null_repr="NULL") == "NULL"
    options = ClipboardOptions(null_repr="NULL")
    assert encode_block(["Note"], [[None]], options=options) == "NULL"


def test_tsv_round_trips_through_the_parser_for_every_value_shape() -> None:
    """The copy and the paste half must agree, or a copy/paste pair loses data."""
    rows = [["DE", "a\tb", "line1\nline2", 'say "hi"', None, 5]]
    columns = ["Code", "Tab", "Newline", "Quote", "Note", "Population"]
    assert parse_block(encode_block(columns, rows)).rows == (
        ("DE", "a\tb", "line1\nline2", 'say "hi"', "", "5"),
    )


def test_the_copy_format_cycles_through_the_three_formats() -> None:
    options = ClipboardOptions()
    assert options.next_copy_format("tsv") == "csv"
    assert options.next_copy_format("csv") == "json"
    assert options.next_copy_format("json") == "tsv"
    assert options.next_copy_format("nonsense") == "tsv"


# -- locale / NULL conversion ---------------------------------------------


def test_the_null_token_becomes_sql_null() -> None:
    assert normalize_text("NULL", COUNTRY.column("Note"), ClipboardOptions()) == ""
    assert normalize_text("null", COUNTRY.column("Note"), ClipboardOptions()) == ""


def test_the_null_token_can_be_pasted_as_literal_text() -> None:
    """An nvarchar column may legitimately contain the word NULL (FR-4.6)."""
    options = ClipboardOptions(null_as_literal=True)
    assert normalize_text("NULL", COUNTRY.column("Name"), options) == "NULL"


def test_an_empty_null_token_disables_null_handling_entirely() -> None:
    options = ClipboardOptions(null_token="")
    assert normalize_text("NULL", COUNTRY.column("Note"), options) == "NULL"


def test_german_numbers_and_dates_convert_when_that_locale_is_configured() -> None:
    options = ClipboardOptions(number_locale="de", date_format="dmy")
    assert normalize_text("1.234,56", METRICS.column("Rate"), options) == "1234.56"
    assert normalize_text("31.01.2026", METRICS.column("TakenOn"), options) == "2026-01-31"
    assert normalize_text("31.01.2026 08:30", METRICS.column("SeenAt"), options) == (
        "2026-01-31T08:30"
    )


def test_us_conventions_strip_thousands_separators() -> None:
    options = ClipboardOptions(number_locale="en")
    assert normalize_text("1,234.56", METRICS.column("Rate"), options) == "1234.56"
    assert normalize_text("1,234", COUNTRY.column("Population"), options) == "1234"


def test_accounting_negatives_and_impossible_dates_are_left_to_validation() -> None:
    options = ClipboardOptions(number_locale="de", date_format="dmy")
    assert normalize_text("(1.234,50)", METRICS.column("Rate"), options) == "-1234.50"
    # 31.02.2026 does not exist: leave it alone so validation reports the real problem.
    assert normalize_text("31.02.2026", METRICS.column("TakenOn"), options) == "31.02.2026"


def test_iso_dates_are_untouched_whatever_the_locale() -> None:
    options = ClipboardOptions(date_format="mdy")
    assert normalize_text("2026-01-31", METRICS.column("TakenOn"), options) == "2026-01-31"


# -- mode choice -----------------------------------------------------------


def test_a_single_value_targets_the_focused_cell() -> None:
    assert choose_mode(target(), parse_block("Bavaria")) is PasteMode.CELL


def test_a_multi_row_block_targets_rows() -> None:
    block = parse_block("DE\tGermany\nFR\tFrance")
    assert choose_mode(target(), block) is PasteMode.ROWS


def test_a_block_matching_the_selection_fills_it() -> None:
    block = parse_block("a\tb\nc\td")
    grid = target(selection=(0, 0, 1, 1))
    assert choose_mode(grid, block) is PasteMode.FILL


# -- planning: cells and single values ------------------------------------


def test_one_value_into_one_cell_is_planned_as_an_update() -> None:
    grid = target(anchor_row=1, anchor_column=1)
    plan = plan_paste(grid, parse_block("Bavaria"))
    assert plan.mode is PasteMode.CELL
    assert plan.rows[0].outcome is RowOutcome.UPDATE
    assert plan.rows[0].target_row == 1
    assert plan.rows[0].values[0].column == "Name"
    assert plan.rows[0].values[0].value == "Bavaria"


def test_a_multiline_value_stays_one_cell() -> None:
    """FR-4.4: newlines in a value are data, not extra rows to insert."""
    grid = target(anchor_row=0, anchor_column=3)
    plan = plan_paste(grid, parse_block("first line\nsecond line"))
    assert plan.mode is PasteMode.CELL
    assert len(plan.rows) == 1
    assert plan.rows[0].values[0].value == "first line\nsecond line"


def test_a_cell_past_null_is_staged_as_none() -> None:
    grid = target(anchor_row=0, anchor_column=3)
    plan = plan_paste(grid, parse_block("NULL"))
    assert plan.rows[0].values[0].value is None


# -- planning: fills -------------------------------------------------------


def test_a_block_fills_existing_cells_from_the_anchor() -> None:
    grid = target(anchor_row=0, anchor_column=1)
    plan = plan_paste(grid, parse_block("Germany II\nFrance II\nJapan II"), mode=PasteMode.FILL)
    assert [row.target_row for row in plan.rows] == [0, 1, 2]
    assert [cell.value for cell in plan.rows[0].values] == ["Germany II"]
    assert plan.ok


def test_a_single_value_fills_the_whole_selection() -> None:
    """Copying one cell and pasting it across a range is what everyone expects."""
    grid = target(selection=(0, 1, 2, 2))
    plan = plan_paste(grid, parse_block("X"))
    assert plan.mode is PasteMode.FILL
    assert len(plan.rows) == 3


def test_a_fill_stops_at_the_end_of_the_grid() -> None:
    grid = target(anchor_row=2, anchor_column=1)
    plan = plan_paste(grid, parse_block("a\nb\nc"), mode=PasteMode.FILL)
    assert len(plan.rows) == 1  # only row 2 exists to fill


def test_a_fill_converts_values_against_their_column_type() -> None:
    grid = target(anchor_row=0, anchor_column=2)
    plan = plan_paste(grid, parse_block("500"), mode=PasteMode.FILL)
    assert plan.rows[0].values[0].column == "Population"
    assert plan.rows[0].values[0].value == 500


# -- planning: rows --------------------------------------------------------


def test_a_positional_row_paste_writes_the_key_so_a_match_updates() -> None:
    """Without a header the key is positional, and it must still reach the UPDATE.

    The key column used to be filtered out of positional row pastes as if it were the
    SQL Server IDENTITY property. It is not: `identity_columns` is row identity (the PK,
    else a single-column UNIQUE). Dropping it meant a pasted key matched nothing, so
    pasting existing rows silently staged duplicate INSERTs instead of UPDATEs.
    """
    grid = target(anchor_column=0)
    plan = plan_paste(grid, parse_block("DE\tGermany"), mode=PasteMode.ROWS)
    assert plan.rows[0].outcome is RowOutcome.UPDATE
    assert plan.rows[0].target_row == 0
    assert plan.rows[0].key == (("Code", "DE"),)
    assert [cell.column for cell in plan.rows[0].values] == ["Code", "Name"]
    assert not plan.inserts


def test_a_positional_row_paste_with_an_unknown_key_inserts() -> None:
    """Carrying the key is what distinguishes an INSERT from an UPDATE (FR-4.6)."""
    grid = target(anchor_column=0)
    plan = plan_paste(grid, parse_block("IT\tItaly"), mode=PasteMode.ROWS)
    assert plan.rows[0].outcome is RowOutcome.INSERT
    assert plan.rows[0].target_row is None
    assert [cell.column for cell in plan.rows[0].values] == ["Code", "Name"]


def test_a_positional_row_paste_still_never_writes_a_server_managed_column() -> None:
    """S-3 is about IDENTITY/computed/rowversion, which stay unwritable positionally."""
    grid = target(anchor_column=0)
    plan = plan_paste(grid, parse_block("DE\tGermany\tX\t0x01"), mode=PasteMode.ROWS)
    written = {cell.column for cell in plan.rows[0].values}
    assert "NameUpper" not in written
    assert "RowVer" not in written


def test_rows_without_a_key_column_are_all_inserts() -> None:
    """A table with no row identity cannot match anything, so every row is INSERTed."""
    keyless = Table(
        schema="dbo",
        name="Lookup",
        kind=TableKind.BASE_TABLE,
        columns=(Column("Label", 1, "nvarchar", 50, None, None, False, None, False),),
        primary_key=None,
    )
    grid = target(table=keyless, columns=keyless.columns, rows=())
    plan = plan_paste(grid, parse_block("a\nb"), mode=PasteMode.ROWS)
    assert len(plan.inserts) == 2
    assert not plan.updates
    assert any("INSERTed" in note for note in plan.notes)


def test_a_json_array_of_scalars_parses_as_one_cell_per_row() -> None:
    """Iterating a scalar element directly raised TypeError, which is not actionable."""
    block = parse_block("[1, 2, 3]")
    assert block.rows == (("1",), ("2",), ("3",))


def test_a_ragged_json_array_is_padded_to_a_rectangular_block() -> None:
    assert parse_block('[["a", "b"], ["c"]]').rows == (("a", "b"), ("c", ""))


def test_malformed_json_raises_a_clipboard_error() -> None:
    with pytest.raises(ClipboardParseError):
        parse_block("[1, 2,")


def test_a_row_carrying_the_key_updates_the_matching_row() -> None:
    grid = target()
    plan = plan_paste(
        grid, parse_block("Code\tName\nDE\tGermany II\nFR\tFrance II", known_columns=NAMES)
    )
    assert plan.used_header
    assert [row.outcome for row in plan.rows] == [RowOutcome.UPDATE, RowOutcome.UPDATE]
    assert plan.rows[0].target_row == 0
    assert plan.rows[1].target_row == 1
    assert plan.rows[0].key == (("Code", "DE"),)


def test_an_unknown_key_inserts_and_a_known_one_updates() -> None:
    """FR-4.6: the UPDATE/INSERT split is per row, not per paste."""
    grid = target()
    plan = plan_paste(
        grid, parse_block("Code\tName\nDE\tGermany II\nES\tSpain", known_columns=NAMES)
    )
    assert plan.rows[0].outcome is RowOutcome.UPDATE
    assert plan.rows[1].outcome is RowOutcome.INSERT
    assert plan.summary() == "1 update, 1 insert · 4 cell(s)"  # two columns per row


def test_a_header_maps_by_name_and_skips_columns_the_table_lacks() -> None:
    grid = target()
    plan = plan_paste(grid, parse_block("Name\tNope\nGermany\tx", known_columns=NAMES))
    assert plan.mapping == (("Name", "Name"),)
    assert any("Nope" in note for note in plan.notes)


def test_a_header_naming_a_server_managed_column_does_not_write_it() -> None:
    """S-3: a computed/rowversion column is never writable, by paste or otherwise."""
    grid = target()
    plan = plan_paste(grid, parse_block("Name\tRowVer\nGermany\t0x0102", known_columns=NAMES))
    assert [cell.column for cell in plan.rows[0].values] == ["Name"]
    assert any("server-managed" in note for note in plan.notes)


def test_untouched_columns_are_reported() -> None:
    grid = target()
    plan = plan_paste(grid, parse_block("Code\tName\nDE\tGermany", known_columns=NAMES))
    assert set(plan.untouched) == {"Population", "Note"}


# -- validation of pasted cells -------------------------------------------


def test_a_value_that_does_not_fit_the_column_is_reported_per_cell() -> None:
    """FR-4.6: the errors are collected and shown; nothing is staged."""
    grid = target()
    plan = plan_paste(grid, parse_block("Code\tPopulation\nDE\tlots\nFR\t12", known_columns=NAMES))
    assert not plan.ok
    assert len(plan.errors) == 1
    assert plan.errors[0].startswith("row 1:")
    assert "whole number" in plan.errors[0]


def test_a_value_too_long_for_the_column_is_reported() -> None:
    grid = target()
    plan = plan_paste(grid, parse_block("Code\tName\nDE\t" + "x" * 201, known_columns=NAMES))
    assert not plan.ok
    assert "100 characters" in plan.errors[0]


def test_a_null_into_a_not_null_column_is_refused() -> None:
    grid = target()
    plan = plan_paste(grid, parse_block("Code\tName\nDE\tNULL", known_columns=NAMES))
    assert not plan.ok
    assert "NOT NULL" in plan.errors[0]


def test_the_preview_shows_the_converted_value_next_to_the_text() -> None:
    """The dialog's "type conversion" column is exactly this pair."""
    options = ClipboardOptions(number_locale="de", date_format="dmy")
    grid = PasteTarget(
        table=METRICS,
        columns=METRICS.columns[1:],
        rows=({"Rate": Decimal("0"), "TakenOn": date(2026, 1, 1), "SeenAt": None},),
        keys=((("Id", 1),),),
    )
    block = parse_block("1.234,56\t31.01.2026")
    plan = plan_paste(grid, block, options=options)
    cells = plan.rows[0].values
    assert cells[0].text == "1.234,56" and cells[0].converted == "1234.56"
    assert cells[1].text == "31.01.2026" and cells[1].converted == "2026-01-31"
    assert plan.ok


def test_an_empty_paste_produces_a_note_rather_than_an_empty_plan() -> None:
    plan = plan_paste(target(), parse_block(""))
    assert plan.notes == ("the pasted text is empty",)
    assert plan.rows == ()


def test_a_block_over_the_row_limit_is_refused_before_any_parsing_work() -> None:
    options = ClipboardOptions(max_rows=2)
    plan = plan_paste(target(), parse_block("1\n2\n3"), options=options)
    assert plan.rows == ()
    assert "paste_max_rows" in plan.notes[0]


def test_the_plan_maps_datetime_text_with_a_time_part() -> None:
    cell = METRICS.column("SeenAt")
    parsed = plan_paste(
        PasteTarget(
            table=METRICS,
            columns=(cell,),
            rows=({"SeenAt": datetime(2026, 1, 31, 8, 30)},),
            keys=(),
        ),
        parse_block("2026-01-31 08:30:00"),
    )
    assert parsed.rows[0].values[0].value == datetime(2026, 1, 31, 8, 30)


def test_a_cell_plan_is_built_from_a_cell_plan_object() -> None:
    """CellPlan is the seam the preview renders; keep its text/value/error contract."""
    cell = CellPlan("Name", "x", "x", ok=True)
    assert cell.converted == "x"
    assert cell.error == ""
    row = PasteRowPlan(0, RowOutcome.INSERT, None, None, (cell,))
    assert row.ok
    assert row.errors == ()


def test_the_block_reports_its_shape() -> None:
    block = ClipboardBlock((("a", "b"), ("c", "d")))
    assert (block.row_count, block.column_count) == (2, 2)
    assert not block.is_single_cell
