"""Live cell validation (FR-3.5): type checks, counters, NULL rules, unverifiable risks.

Pure functions over a column's metadata — no database, no provider (NFR-5). The point of
these tests is that an invalid value is *blocked* (FR-3.5) while a risk the app cannot
decide is *reported and deferred to the database* rather than guessed.
"""

from datetime import date, datetime, time
from decimal import Decimal

import pytest

from sql_table_swiss_knife.domain import (
    CheckConstraint,
    Column,
    ForeignKey,
    PrimaryKey,
    ReferentialAction,
    Table,
    TableKind,
    UniqueConstraint,
)
from sql_table_swiss_knife.services.validation import (
    VERIFIED_BY_DATABASE,
    HintLevel,
    parse_value,
    validate_input,
)

#: A well-formed GUID, used for the uniqueidentifier round trip.
GUID = "6f9619ff-8b86-d011-b42d-00c04fc964ff"


def column(data_type: str, **kwargs: object) -> Column:
    """A column with the given type; keyword arguments override any metadata field."""
    defaults: dict[str, object] = {
        "max_length": None,
        "precision": None,
        "scale": None,
        "nullable": False,
        "default_definition": None,
        "is_identity": False,
    }
    defaults.update(kwargs)
    return Column(name="X", ordinal=1, data_type=data_type, **defaults)  # type: ignore[arg-type]


def table(*columns: Column, **kwargs: object) -> Table:
    """A table owning ``columns``; keyword arguments override any metadata field."""
    defaults: dict[str, object] = {
        "schema": "dbo",
        "name": "T",
        "kind": TableKind.BASE_TABLE,
        "primary_key": PrimaryKey("PK_X", (columns[0].name,)),
        "foreign_keys": (),
        "unique_constraints": (),
        "check_constraints": (),
        "triggers": (),
    }
    defaults.update(kwargs)
    return Table(columns=columns, **defaults)  # type: ignore[arg-type]


# -- type parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "text", "expected"),
    [
        ("int", "42", 42),
        ("bigint", "-7", -7),
        ("smallint", "0", 0),
        ("tinyint", "255", 255),
        ("decimal", "10.25", Decimal("10.25")),
        ("money", "3", Decimal("3")),
        ("float", "1.5", 1.5),
        ("bit", "1", True),
        ("bit", "true", True),
        ("bit", "false", False),
        ("date", "2026-09-30", date(2026, 9, 30)),
        ("datetime2", "2026-09-30 12:30:00", datetime(2026, 9, 30, 12, 30)),
        ("time", "08:15", time(8, 15)),
        (
            "uniqueidentifier",
            "6f9619ff-8b86-d011-b42d-00c04fc964ff",
            "6f9619ff-8b86-d011-b42d-00c04fc964ff",
        ),
        ("nvarchar", "hello", "hello"),
        ("varbinary", "0x01FF", b"\x01\xff"),
    ],
)
def test_supported_types_parse_to_python_values(
    data_type: str, text: str, expected: object
) -> None:
    """The milestone's named types (int/decimal/date/uniqueidentifier/bit) all parse."""
    value, hints = parse_value(column(data_type), text)

    assert value == expected
    assert hints == ()


@pytest.mark.parametrize(
    ("data_type", "text", "expected_text"),
    [
        ("int", "abc", "is not a whole number"),
        ("int", "1.5", "is not a whole number"),
        ("tinyint", "300", "outside"),
        ("decimal", "twelve", "is not a number"),
        ("date", "30/09/2026", "use YYYY-MM-DD"),
        ("time", "25 o'clock", "use HH:MM"),
        ("uniqueidentifier", "not-a-guid", "not a GUID"),
        ("bit", "maybe", "use 0/1, true/false"),
        ("varbinary", "zz", "not hex digits"),
    ],
)
def test_invalid_text_is_rejected_with_a_helpful_message(
    data_type: str, text: str, expected_text: str
) -> None:
    with pytest.raises(ValueError, match=expected_text):
        parse_value(column(data_type), text)


def test_unknown_types_are_passed_through_as_text() -> None:
    """The app must never refuse a type it does not know: the server decides."""
    assert parse_value(column("hierarchyid"), "/a/b/")[0] == "/a/b/"


def test_empty_text_and_the_literal_null_mean_sql_null() -> None:
    """Both spellings map to NULL; the *allowedness* is decided by the column rule."""
    assert parse_value(column("int", nullable=True), "")[0] is None
    assert parse_value(column("int", nullable=True), "  null ")[0] is None


# -- NULL rules --------------------------------------------------------------


def test_null_in_a_not_null_column_blocks_staging() -> None:
    result = validate_input(table(column("int", nullable=False)), column("int"), "NULL")

    assert result.ok is False
    assert result.hints[-1].level is HintLevel.ERROR
    assert "NOT NULL" in result.hints[-1].text


def test_null_in_a_nullable_column_is_allowed_and_announced() -> None:
    result = validate_input(table(column("int", nullable=True)), column("int", nullable=True), "")

    assert result.ok is True
    assert result.value is None
    assert any("accepts NULL" in hint.text for hint in result.hints)


# -- length, precision, scale ------------------------------------------------


def test_max_length_counter_is_shown_while_typing() -> None:
    # Column.max_length is already a character count (the metadata mapper halves the
    # byte count sys.columns reports), so nvarchar(100) arrives as max_length=100.
    subject = column("nvarchar", max_length=100, nullable=True)
    result = validate_input(table(subject), subject, "abc")

    assert result.ok is True
    assert "3/100 characters" in result.message


def test_a_value_longer_than_the_column_blocks_staging() -> None:
    subject = column("nvarchar", max_length=20, nullable=True)
    result = validate_input(table(subject), subject, "x" * 21)

    assert result.ok is False
    assert "at most 20 characters (now 21)" in result.message


def test_unicode_length_is_not_halved_twice() -> None:
    """A nvarchar(400) column accepts 400 characters, not 200.

    Regression: ``sys.columns`` reports *bytes* for nvarchar and the metadata mapper
    halves them once. A second halving in the length limit silently capped every
    Unicode column at half its real width, rejecting valid edits.
    """
    subject = column("nvarchar", max_length=400, nullable=True)
    result = validate_input(table(subject), subject, "x" * 400)

    assert result.ok is True
    assert "400/400 characters" in result.message

    over = validate_input(table(subject), subject, "x" * 401)

    assert over.ok is False
    assert "at most 400 characters (now 401)" in over.message


def test_byte_sized_char_types_keep_their_length() -> None:
    """Only nvarchar/nchar needed the byte-to-character conversion upstream."""
    subject = column("varchar", max_length=20, nullable=True)
    result = validate_input(table(subject), subject, "x" * 21)

    assert result.ok is False
    assert "at most 20 characters" in result.message


def test_precision_and_scale_are_counted() -> None:
    subject = column("decimal", precision=10, scale=2, nullable=True)
    result = validate_input(table(subject), subject, "1234.56")

    assert result.ok is True
    assert "6/10 digits, 2/2 scale" in result.message


def test_too_many_decimal_places_blocks_staging() -> None:
    subject = column("decimal", precision=10, scale=2, nullable=True)
    result = validate_input(table(subject), subject, "1.234")

    assert result.ok is False
    assert "scale 2" in result.message


def test_too_many_total_digits_blocks_staging() -> None:
    subject = column("decimal", precision=4, scale=2, nullable=True)
    result = validate_input(table(subject), subject, "123.45")

    assert result.ok is False
    assert "holds 4 digits" in result.message


# -- read-only columns -------------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "kwargs"),
    [
        ("int", {"is_identity": True}),
        ("int", {"is_computed": True, "computed_definition": "1+1"}),
        ("timestamp", {"is_rowversion": True}),
    ],
)
def test_server_managed_columns_refuse_edits(data_type: str, kwargs: dict[str, object]) -> None:
    """S-3: identity, computed and rowversion cells are never editable."""
    subject = column(data_type, **kwargs)
    result = validate_input(table(subject), subject, "1")

    assert result.ok is False
    assert "read-only" in result.message


# -- risks the database must verify ------------------------------------------


def test_unique_constraint_is_a_warning_deferred_to_the_database() -> None:
    subject = column("nvarchar", max_length=200, nullable=True)
    owning = table(subject, unique_constraints=(UniqueConstraint("UQ_X", ("X",)),))
    result = validate_input(owning, subject, "Germany")

    assert result.ok is True  # uniqueness is not knowable without reading the rows
    warning = next(hint for hint in result.hints if hint.level is HintLevel.WARNING)
    assert "UQ_X" in warning.text
    assert warning.text.endswith(VERIFIED_BY_DATABASE)


def test_check_constraint_is_a_warning_deferred_to_the_database() -> None:
    subject = column("int", nullable=True)
    owning = table(subject, check_constraints=(CheckConstraint("CK_X", "([X]>(0))"),))
    result = validate_input(owning, subject, "-1")

    assert result.ok is True  # the app does not evaluate CHECK expressions
    warning = next(hint for hint in result.hints if hint.level is HintLevel.WARNING)
    assert "([X]>(0))" in warning.text
    assert VERIFIED_BY_DATABASE in warning.text


def test_foreign_key_column_notes_the_referenced_table() -> None:
    subject = column("char", max_length=2, nullable=True)
    owning = table(
        subject,
        foreign_keys=(
            ForeignKey(
                "FK_Region_Country",
                ("X",),
                "dbo",
                "Country",
                ("Code",),
                ReferentialAction.CASCADE,
                ReferentialAction.NO_ACTION,
            ),
        ),
    )
    result = validate_input(owning, subject, "DE")

    note = next(hint.text for hint in result.hints if "dbo.Country.Code" in hint.text)
    assert VERIFIED_BY_DATABASE in note


def test_error_beats_every_other_hint_in_the_summary() -> None:
    """The one-line summary shows the blocking problem first (FR-3.5 inline error)."""
    subject = column("int", nullable=True)
    owning = table(subject, check_constraints=(CheckConstraint("CK_X", "([X]>(0))"),))
    result = validate_input(owning, subject, "abc")

    assert result.ok is False
    assert result.message == result.errors[0].text
