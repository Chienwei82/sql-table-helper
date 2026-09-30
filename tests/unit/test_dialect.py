"""Tests for the TSqlDialect statement generation."""

from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

import pytest

from sql_table_swiss_knife.providers.mssql import TSqlDialect


@pytest.fixture
def dialect() -> TSqlDialect:
    return TSqlDialect()


def test_name_and_quoting(dialect: TSqlDialect) -> None:
    assert dialect.name == "tsql"
    assert dialect.quote_ident("Code") == "[Code]"
    assert dialect.quote_ident("na]me") == "[na]]me]"
    assert dialect.quote_qualified("dbo", "Country") == "[dbo].[Country]"
    assert dialect.quote_qualified(None, "Country") == "[Country]"
    with pytest.raises(ValueError, match="empty"):
        dialect.quote_ident("")


def test_placeholders(dialect: TSqlDialect) -> None:
    assert dialect.placeholder(0) == "@p0"
    assert dialect.placeholder(12) == "@p12"
    with pytest.raises(ValueError, match=">= 0"):
        dialect.placeholder(-1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "NULL"),
        (True, "1"),
        (False, "0"),
        (42, "42"),
        (-7, "-7"),
        (3.5, "3.5"),
        ("O'Brien", "N'O''Brien'"),
        ("plain", "N'plain'"),
        (b"\x0a\xff", "0x0AFF"),
        (Decimal("1.50"), "1.50"),
        (date(2024, 1, 2), "'2024-01-02'"),
        (datetime(2024, 1, 2, 3, 4, 5), "'2024-01-02 03:04:05'"),
        (time(3, 4, 5), "'03:04:05'"),
        (UUID("12345678-1234-5678-1234-567812345678"), "'12345678-1234-5678-1234-567812345678'"),
    ],
)
def test_literals(dialect: TSqlDialect, value: object, expected: str) -> None:
    assert dialect.literal(value) == expected


def test_literal_rejects_unsupported_values(dialect: TSqlDialect) -> None:
    with pytest.raises(ValueError, match="NaN"):
        dialect.literal(float("nan"))
    with pytest.raises(TypeError, match="unsupported literal type"):
        dialect.literal(object())
