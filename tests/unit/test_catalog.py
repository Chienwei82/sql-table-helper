"""Tests for catalog domain models (Column, keys, constraints, Trigger, Table)."""

import dataclasses

import pytest

from sql_table_swiss_knife.domain import (
    CheckConstraint,
    Column,
    ForeignKey,
    PrimaryKey,
    ReferentialAction,
    Table,
    TableKind,
    TableMetadata,
    TableSummary,
    Trigger,
    UniqueConstraint,
)


def _column(**overrides: object) -> Column:
    base: dict[str, object] = {
        "name": "Col",
        "ordinal": 1,
        "data_type": "int",
        "max_length": None,
        "precision": None,
        "scale": None,
        "nullable": False,
        "default_definition": None,
        "is_identity": False,
    }
    base.update(overrides)
    return Column(**base)  # type: ignore[arg-type]


def test_column_server_managed_flags() -> None:
    assert not _column().is_server_managed
    assert _column(is_identity=True).is_server_managed
    assert _column(is_computed=True).is_server_managed
    assert _column(is_rowversion=True).is_server_managed


def test_column_validation() -> None:
    with pytest.raises(ValueError, match="column name"):
        _column(name="")
    with pytest.raises(ValueError, match="data_type"):
        _column(data_type="")
    with pytest.raises(ValueError, match="ordinal"):
        _column(ordinal=-1)
    with pytest.raises(ValueError, match="max_length"):
        _column(max_length=-1)


def test_primary_key_requires_columns() -> None:
    with pytest.raises(ValueError, match="at least one column"):
        PrimaryKey("PK_X", ())
    assert PrimaryKey(None, ("A", "B")).columns == ("A", "B")


def test_foreign_key_column_counts_must_match() -> None:
    fk = ForeignKey(
        "FK_X",
        ("A",),
        "dbo",
        "Other",
        ("B",),
        ReferentialAction.NO_ACTION,
        ReferentialAction.CASCADE,
    )
    assert fk.on_delete is ReferentialAction.NO_ACTION
    with pytest.raises(ValueError, match="column counts must match"):
        ForeignKey(
            "FK_Y",
            ("A", "B"),
            "dbo",
            "Other",
            ("C",),
            ReferentialAction.NO_ACTION,
            ReferentialAction.NO_ACTION,
        )


def test_referential_action_values() -> None:
    assert {action.value for action in ReferentialAction} == {
        "NO ACTION",
        "CASCADE",
        "SET NULL",
        "SET DEFAULT",
    }


def test_check_constraint_requires_definition() -> None:
    with pytest.raises(ValueError, match="definition"):
        CheckConstraint("CK_X", "")


def test_unique_constraint_requires_columns() -> None:
    with pytest.raises(ValueError, match="at least one column"):
        UniqueConstraint("UQ_X", ())


def test_trigger_normalizes_events_and_firing() -> None:
    trigger = Trigger("trg_x", (" insert ", "update"), "instead of")
    assert trigger.events == ("INSERT", "UPDATE")
    assert trigger.firing == "INSTEAD OF"
    assert trigger.enabled


def test_trigger_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="at least one event"):
        Trigger("trg_x", (), "AFTER")
    with pytest.raises(ValueError, match="trigger event"):
        Trigger("trg_x", ("MERGE",), "AFTER")
    with pytest.raises(ValueError, match="firing"):
        Trigger("trg_x", ("INSERT",), "BEFORE")


def test_table_requires_columns_and_unique_names() -> None:
    with pytest.raises(ValueError, match="at least one column"):
        Table(schema="dbo", name="X", kind=TableKind.BASE_TABLE, columns=())
    with pytest.raises(ValueError, match="duplicate column names"):
        Table(
            schema="dbo",
            name="X",
            kind=TableKind.BASE_TABLE,
            columns=(_column(name="A", ordinal=1), _column(name="A", ordinal=2)),
        )


def test_table_column_lookup(country_table: Table) -> None:
    assert country_table.column("Name").data_type == "nvarchar"
    with pytest.raises(KeyError, match="no column"):
        country_table.column("Missing")


def test_table_identity_columns_from_primary_key(country_table: Table) -> None:
    assert country_table.identity_columns == ("Code",)
    assert country_table.updatable


def test_table_identity_columns_unique_fallback() -> None:
    table = Table(
        schema="dbo",
        name="NoPk",
        kind=TableKind.BASE_TABLE,
        columns=(
            _column(name="Email", ordinal=1, nullable=False),
            _column(name="Nick", ordinal=2, nullable=True),
        ),
        unique_constraints=(
            UniqueConstraint("UQ_Email", ("Email",)),
            UniqueConstraint("UQ_Nick", ("Nick",)),
        ),
    )
    assert table.identity_columns == ("Email",)  # first single-col non-null unique
    assert table.updatable


def test_table_without_key_is_read_only() -> None:
    table = Table(
        schema="dbo",
        name="Heap",
        kind=TableKind.BASE_TABLE,
        columns=(_column(name="Payload", ordinal=1, nullable=True),),
    )
    assert table.identity_columns == ()
    assert not table.updatable


def test_view_is_never_updatable(country_table: Table) -> None:
    view = dataclasses.replace(country_table, name="v_Country", kind=TableKind.VIEW)
    assert view.is_view
    assert not view.updatable


def test_table_rowversion_and_summary(country_table: Table) -> None:
    assert country_table.rowversion_column is not None
    assert country_table.rowversion_column.name == "RowVer"
    # The summary carries everything the table picker badges need (FR-2.3).
    assert country_table.summary == TableSummary(
        schema="dbo",
        name="Country",
        kind=TableKind.BASE_TABLE,
        has_primary_key=True,
        approximate_row_count=3,
        has_triggers=True,
        has_foreign_keys=False,
    )
    assert str(country_table.ref) == "dbo.Country"
    assert country_table.approximate_row_count == 3


def test_table_metadata_alias() -> None:
    assert TableMetadata is Table


def test_table_is_frozen(country_table: Table) -> None:
    with pytest.raises((AttributeError, ValueError)):
        country_table.name = "Other"  # type: ignore[misc]
