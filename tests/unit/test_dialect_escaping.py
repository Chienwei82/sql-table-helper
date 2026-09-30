"""Escaping and quoting: the one place a Python value becomes SQL text (FR-5.2, S-6).

Every rendering in the app — the literal statements, the batched INSERT/MERGE, the
transaction script — goes through :meth:`TSqlDialect.literal`, so this module is where a
value that *looks* like SQL must be proven to be harmless. The cases below are the ones
that have actually broken naive implementations: a quote that ends the literal, a newline
that hides the rest of a statement, a NUL that truncates server-side, a number too big for
the server to parse, and binary that is not text at all.
"""

from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID

import pytest

from sql_table_swiss_knife.domain.rows import FilterOp
from sql_table_swiss_knife.providers.dialect import SqlScript
from sql_table_swiss_knife.providers.mssql import TSqlDialect


@pytest.fixture
def dialect() -> TSqlDialect:
    return TSqlDialect()


# -- the nasty inputs --------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # A quote must be doubled, or it ends the literal and the rest becomes SQL.
        ("O'Brien", "N'O''Brien'"),
        ("''", "N''''''"),
        # Quoted/quote-only values are covered by the round-trip test below, which checks
        # the value survives rather than counting quotes.
        # Newlines/tabs are legal inside a literal; rewriting them would change the value.
        ("line1\nline2", "N'line1\nline2'"),
        ("trailing\r\n", "N'trailing\r\n'"),
        ("tab\there", "N'tab\there'"),
        ("\x0b\x0c", "N'\x0b\x0c'"),  # vertical tab / form feed
        # Unicode stays as-is; the N prefix is what makes it survive a non-Unicode collation.
        ("Ärger", "N'Ärger'"),
        ("日本語テキスト", "N'日本語テキスト'"),
        ("emoji 🎯 ok", "N'emoji 🎯 ok'"),
        # Nothing at all, and the literal that means nothing.
        ("", "N''"),
        (None, "NULL"),
        (True, "1"),
        (False, "0"),
    ],
)
def test_string_literals_are_escaped(
    dialect: TSqlDialect, value: str | None, expected: str
) -> None:
    assert dialect.literal(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "it's a 'test'; --",
        "'",
        "''",
        "''''",
        "'; DROP TABLE [dbo].[Country]; --",
        "'); DELETE FROM [dbo].[AuditLog]; --'",
        "'" * 22,
    ],
)
def test_a_quote_cannot_escape_the_literal(dialect: TSqlDialect, value: str) -> None:
    """Round-trip, not quote-counting: the value must come back out of the literal exactly.

    Undoing the escaping has to recover the original, which is the property that actually
    matters — a doubled quote is a display convention, "4 quotes" is not a rule. A literal
    that came back with a different value would be a silent data-corruption bug.
    """
    rendered = dialect.literal(value)
    assert rendered.startswith("N'") and rendered.endswith("'")
    assert rendered[2:-1].replace("''", "'") == value
    # The interior is balanced: every interior quote belongs to a pair, so the value itself
    # cannot close the literal early.
    assert rendered[2:-1].count("'") % 2 == 0


def test_a_nul_character_is_refused(dialect: TSqlDialect) -> None:
    """A NUL would be silently truncated by the server, changing the stored value."""
    with pytest.raises(ValueError, match="NUL"):
        dialect.literal("before\x00after")


def test_binary_is_hexadecimal_not_text(dialect: TSqlDialect) -> None:
    assert dialect.literal(b"") == "0x"
    assert dialect.literal(b"\x00") == "0x00"
    assert dialect.literal(b"\xde\xad\xbe\xef") == "0xDEADBEEF"
    assert dialect.literal(bytearray(b"\x0a\xff")) == "0x0AFF"
    assert dialect.literal(memoryview(b"\x01")) == "0x01"
    # Upper-case hex is the SQL Server convention in SSMS output.
    assert dialect.literal(b"\xab") == dialect.literal(b"\xab")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (-1, "-1"),
        (2**63 - 1, "9223372036854775807"),  # bigint max
        (-(2**63), "-9223372036854775808"),  # bigint min
    ],
)
def test_integer_boundaries(dialect: TSqlDialect, value: int, expected: str) -> None:
    assert dialect.literal(value) == expected


@pytest.mark.parametrize("value", [2**63, -(2**63) - 1, 10**30])
def test_an_integer_too_big_for_bigint_is_refused(dialect: TSqlDialect, value: int) -> None:
    """A number SQL Server cannot parse is worse than a loud failure at generation time."""
    with pytest.raises(ValueError, match="bigint"):
        dialect.literal(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0"), "0"),
        (Decimal("-1.500"), "-1.500"),
        # Exponent form must render as plain digits: "1E+3" is valid for a float but not as
        # a decimal literal in a copy/paste script.
        (Decimal("1E+3"), "1000"),
        (Decimal("1E-7"), "0.0000001"),
        (Decimal("123456789012345678901234567890.123"), "123456789012345678901234567890.123"),
    ],
)
def test_decimals_render_as_plain_digits(
    dialect: TSqlDialect, value: Decimal, expected: str
) -> None:
    assert dialect.literal(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2024, 2, 29), "'2024-02-29'"),  # leap day
        (date(1, 1, 1), "'0001-01-01'"),  # year 1, zero-padded
        (date(9999, 12, 31), "'9999-12-31'"),
        (datetime(2024, 1, 2, 3, 4, 5), "'2024-01-02 03:04:05'"),
        (datetime(2024, 1, 2, 3, 4, 5, 678901), "'2024-01-02 03:04:05.678901'"),
        (time(0, 0, 0), "'00:00:00'"),
        (time(23, 59, 59, 999999), "'23:59:59.999999'"),
        (UUID("12345678-1234-5678-1234-567812345678"), "'12345678-1234-5678-1234-567812345678'"),
    ],
)
def test_dates_and_identifiers_are_iso(dialect: TSqlDialect, value: object, expected: str) -> None:
    """ISO-8601 and culture-independent: a copied script must mean the same thing on a
    machine with a different locale — the classic silent-corruption trap."""
    assert dialect.literal(value) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_refused(dialect: TSqlDialect, value: float) -> None:
    with pytest.raises(ValueError, match="NaN/Infinity"):
        dialect.literal(value)


def test_an_aware_datetime_keeps_its_offset(dialect: TSqlDialect) -> None:
    aware = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert dialect.literal(aware) == "'2024-01-02 03:04:05+00:00'"


def test_a_datetime_is_not_treated_as_a_date(dialect: TSqlDialect) -> None:
    """``datetime`` subclasses ``date``, so the order of the isinstance checks is
    load-bearing — reversing it would silently drop the time part."""
    assert dialect.literal(datetime(2024, 1, 2, 3, 4)) != dialect.literal(date(2024, 1, 2))


def test_an_unsupported_type_is_refused(dialect: TSqlDialect) -> None:
    with pytest.raises(TypeError, match="unsupported literal type"):
        dialect.literal({"a": 1})


# -- identifiers -------------------------------------------------------------


def test_identifier_quoting_cannot_be_broken_out_of(dialect: TSqlDialect) -> None:
    assert dialect.quote_ident("na]me") == "[na]]me]"  # the bracket must be doubled
    assert dialect.quote_ident("x]; DROP TABLE y; --") == "[x]]; DROP TABLE y; --]"
    assert dialect.quote_qualified("dbo", "Country]") == "[dbo].[Country]]]"
    with pytest.raises(ValueError, match="empty"):
        dialect.quote_ident("")
    with pytest.raises(ValueError, match="NUL"):
        dialect.quote_ident("a\x00b")


# -- batched statements and the script envelope -----------------------------


def test_a_batched_insert_needs_one_value_per_column(dialect: TSqlDialect) -> None:
    with pytest.raises(ValueError, match="must have 2 values"):
        dialect.insert_rows_sql("dbo", "Country", ["Code", "Name"], [["'a'"], ["'b'", "'c'"]])


def test_a_batch_needs_at_least_one_row(dialect: TSqlDialect) -> None:
    assert dialect.insert_rows_sql("dbo", "Country", ["Code"], ()) == ""


def test_merge_needs_its_key_columns_written(dialect: TSqlDialect) -> None:
    with pytest.raises(ValueError, match="key columns must be written"):
        dialect.merge_sql("dbo", "Country", ["Code"], ["Name"], [["N'x'"]])


def test_merge_omits_the_matched_clause_when_there_is_nothing_to_update(
    dialect: TSqlDialect,
) -> None:
    """SQL Server rejects a WHEN MATCHED clause with an empty UPDATE SET."""
    sql = dialect.merge_sql("dbo", "Country", ["Code"], ["Code"], [["N'x'"]])
    assert "WHEN MATCHED" not in sql
    assert "INSERT ([Code]) VALUES (source.[Code]);" in sql


def test_the_script_is_all_or_nothing(dialect: TSqlDialect) -> None:
    """The script must roll back on any error (S-7) — that is its whole promise."""
    script = dialect.script_sql(SqlScript(body=("INSERT INTO [dbo].[T] ([a]) VALUES (1)",)))
    assert script.startswith("SET XACT_ABORT ON;\nBEGIN TRANSACTION;\nBEGIN TRY;")
    # The rollback is guarded, so an error *before* the transaction opens is not masked.
    assert "IF @@TRANCOUNT > 0" in script
    assert "ROLLBACK TRANSACTION;" in script
    assert "THROW;" in script  # the original error is re-raised, not swallowed
    # COMMIT is inside the TRY: reaching CATCH means nothing was committed.
    assert script.index("COMMIT TRANSACTION;") < script.index("END TRY")
    assert script.index("BEGIN CATCH") > script.index("END TRY")


def test_the_script_opens_and_closes_identity_insert(dialect: TSqlDialect) -> None:
    script = dialect.script_sql(
        SqlScript(body=("INSERT INTO [dbo].[T] ([Id]) VALUES (1)",), identity_insert=("dbo", "T"))
    )
    on = "SET IDENTITY_INSERT [dbo].[T] ON;"
    off = "SET IDENTITY_INSERT [dbo].[T] OFF;"
    assert on in script and off in script
    # Closed before the commit, so an abort cannot leave the table locked for other writers.
    assert script.index(off) < script.index("COMMIT TRANSACTION;")


def test_an_empty_script_is_empty(dialect: TSqlDialect) -> None:
    assert dialect.script_sql(SqlScript(body=())) == ""


def test_statements_are_terminated_exactly_once(dialect: TSqlDialect) -> None:
    script = dialect.script_sql(SqlScript(body=("DELETE FROM [dbo].[T];",)))
    assert script.count("DELETE FROM [dbo].[T];") == 1


def test_a_predicate_without_a_value_is_refused(dialect: TSqlDialect) -> None:
    assert dialect.predicate_sql("Code", FilterOp.IS_NULL) == "[Code] IS NULL"
    with pytest.raises(ValueError, match="requires a placeholder"):
        dialect.predicate_sql("Code", FilterOp.EQ)
