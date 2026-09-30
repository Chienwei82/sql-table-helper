"""Tests for identifier validation and TableRef."""

import pytest

from sql_table_swiss_knife.domain import TableRef, validate_identifier


def test_validate_identifier_accepts_normal_name() -> None:
    assert validate_identifier("RegionId", kind="column name") == "RegionId"


def test_validate_identifier_rejects_empty() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        validate_identifier("", kind="column name")


def test_validate_identifier_rejects_nul() -> None:
    with pytest.raises(ValueError, match="NUL"):
        validate_identifier("a\x00b", kind="column name")


def test_validate_identifier_rejects_oversized_name() -> None:
    with pytest.raises(ValueError, match="at most 128"):
        validate_identifier("x" * 129, kind="table name")


def test_table_ref_requires_valid_parts() -> None:
    ref = TableRef(schema="dbo", name="Country")
    assert str(ref) == "dbo.Country"


def test_table_ref_rejects_empty_schema() -> None:
    with pytest.raises(ValueError, match="schema name"):
        TableRef(schema="", name="Country")


def test_table_ref_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="table name"):
        TableRef(schema="dbo", name="")


def test_table_ref_is_frozen() -> None:
    ref = TableRef(schema="dbo", name="Country")
    with pytest.raises((AttributeError, ValueError)):
        ref.name = "Other"  # type: ignore[misc]
