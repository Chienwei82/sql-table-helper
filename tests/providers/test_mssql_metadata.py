"""Unit tests for the ``sys.*`` row → domain mapping (no server needed).

These feed the exact row shapes SQL Server returns (documented values, including the
Unicode ``max_length`` in bytes and ``temporal_type``/``trigger_events.type`` codes) into
the mappers, so the introspection logic is verifiable without a live database.
"""

from typing import Any

import pytest

from sql_table_swiss_knife.domain import ReferentialAction, Table, TableKind
from sql_table_swiss_knife.providers.mssql import metadata as md
from sql_table_swiss_knife.services.inspector import format_data_type
from sql_table_swiss_knife.services.validation import _length_limit, validate_input

# -- helpers ------------------------------------------------------------------


def _column(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "column_name": "Col",
        "column_id": 1,
        "data_type": "int",
        "max_length": 4,
        "precision": 10,
        "scale": 0,
        "is_nullable": 1,
        "is_identity": 0,
        "is_computed": 0,
        "is_rowversion": 0,
        "collation_name": None,
        "default_definition": None,
        "seed_value": None,
        "increment_value": None,
        "computed_definition": None,
        "computed_persisted": None,
    }
    row.update(overrides)
    return row


# -- columns ------------------------------------------------------------------


def test_plain_int_column() -> None:
    column = md.column_from_row(_column(column_name="Population", is_nullable=0))
    assert column.name == "Population"
    assert column.data_type == "int"
    assert column.max_length is None  # 4 bytes is not a length
    assert column.precision is None
    assert column.scale is None
    assert column.nullable is False


def test_nvarchar_length_is_halved_from_bytes() -> None:
    column = md.column_from_row(
        _column(
            column_name="Name",
            data_type="nvarchar",
            max_length=200,
            collation_name="SQL_Latin1_General_CP1_CI_AS",
        )
    )
    assert column.max_length == 100
    assert column.collation == "SQL_Latin1_General_CP1_CI_AS"


def test_nvarchar_width_survives_the_whole_pipeline() -> None:
    """sys reports bytes; the declared character width must come out the far end.

    Regression: the mapper halves ``max_length`` once (bytes → characters), and the
    type formatter halved it a *second* time, so a declared ``nvarchar(100)`` was
    advertised as ``nvarchar(50)`` and the editor refused the last 50 characters.
    This exercises the real seam — raw sys row → mapper → formatter and edit limit.
    """
    raw_bytes = 800  # what sys.columns reports for nvarchar(400)
    column = md.column_from_row(
        _column(column_name="Detail", data_type="nvarchar", max_length=raw_bytes)
    )

    assert column.max_length == 400
    assert format_data_type(column) == "nvarchar(400)"
    assert _length_limit(column) == 400

    owning = Table(
        columns=(column,),
        schema="dbo",
        name="T",
        kind=TableKind.BASE_TABLE,
    )
    assert validate_input(owning, column, "x" * 400).ok is True
    assert validate_input(owning, column, "x" * 401).ok is False


def test_max_length() -> None:
    column = md.column_from_row(_column(data_type="varchar", max_length=-1))
    assert column.max_length is None


def test_char_and_varbinary_keep_length() -> None:
    assert md.column_from_row(_column(data_type="char", max_length=2)).max_length == 2
    assert md.column_from_row(_column(data_type="varbinary", max_length=16)).max_length == 16


def test_decimal_precision_and_scale() -> None:
    column = md.column_from_row(_column(data_type="decimal", max_length=9, precision=18, scale=2))
    assert (column.precision, column.scale) == (18, 2)


def test_datetime2_precision() -> None:
    column = md.column_from_row(_column(data_type="datetime2", max_length=8, precision=3, scale=3))
    assert (column.precision, column.scale) == (3, 3)


def test_timestamp_is_reported_as_rowversion() -> None:
    column = md.column_from_row(_column(data_type="timestamp", max_length=8, is_nullable=0))
    assert column.data_type == "rowversion"
    assert column.is_rowversion is True


def test_identity_seed_and_increment() -> None:
    column = md.column_from_row(
        _column(is_identity=1, seed_value="10", increment_value="5", is_nullable=0)
    )
    assert column.is_identity is True
    assert (column.identity_seed, column.identity_increment) == (10, 5)
    assert column.is_server_managed


def test_non_identity_has_no_seed() -> None:
    column = md.column_from_row(_column(seed_value="10", increment_value="5"))
    assert column.identity_seed is None


def test_computed_column_persisted_flag() -> None:
    computed = md.column_from_row(
        _column(is_computed=1, computed_definition="UPPER([Name])", computed_persisted=1)
    )
    assert computed.is_computed is True
    assert computed.computed_definition == "UPPER([Name])"
    assert computed.computed_persisted is True
    assert computed.is_server_managed

    not_persisted = md.column_from_row(
        _column(is_computed=1, computed_definition="UPPER([Name])", computed_persisted=0)
    )
    assert not_persisted.computed_persisted is False


def test_default_definition() -> None:
    column = md.column_from_row(_column(default_definition="((0))"))
    assert column.default_definition == "((0))"


def test_column_flags_from_keys() -> None:
    column = md.column_from_row(
        _column(column_name="Code"), is_primary_key=True, is_foreign_key=True
    )
    assert column.is_primary_key is True
    assert column.is_foreign_key is True


# -- keys ---------------------------------------------------------------------


def test_primary_and_unique_keys() -> None:
    rows = [
        {
            "index_name": "PK_RegionAlias",
            "is_primary_key": 1,
            "key_ordinal": 1,
            "column_name": "RegionId",
        },
        {
            "index_name": "PK_RegionAlias",
            "is_primary_key": 1,
            "key_ordinal": 2,
            "column_name": "Lang",
        },
        {
            "index_name": "UQ_Country_Name",
            "is_primary_key": 0,
            "key_ordinal": 1,
            "column_name": "Name",
        },
    ]
    primary_key, uniques = md.unique_and_primary_keys(rows)
    assert primary_key is not None
    assert primary_key.name == "PK_RegionAlias"
    assert primary_key.columns == ("RegionId", "Lang")
    assert [u.columns for u in uniques] == [("Name",)]
    assert uniques[0].name == "UQ_Country_Name"


def test_no_keys() -> None:
    primary_key, uniques = md.unique_and_primary_keys([])
    assert primary_key is None
    assert uniques == ()


# -- foreign keys -------------------------------------------------------------


def test_foreign_key_with_referential_actions() -> None:
    rows = [
        {
            "constraint_name": "FK_Region_Country",
            "column_name": "CountryCode",
            "referenced_schema": "dbo",
            "referenced_table": "Country",
            "referenced_column": "Code",
            "on_delete": "CASCADE",
            "on_update": "NO_ACTION",
        }
    ]
    (fk,) = md.foreign_keys(rows)
    assert fk.columns == ("CountryCode",)
    assert (fk.referenced_schema, fk.referenced_table) == ("dbo", "Country")
    assert fk.referenced_columns == ("Code",)
    assert fk.on_delete is ReferentialAction.CASCADE
    assert fk.on_update is ReferentialAction.NO_ACTION


def test_composite_foreign_key_column_order() -> None:
    rows = [
        {
            "constraint_name": "FK",
            "column_name": "A",
            "referenced_schema": "dbo",
            "referenced_table": "T",
            "referenced_column": "RA",
            "on_delete": "SET_NULL",
            "on_update": "CASCADE",
            "constraint_column_id": 1,
        },
        {
            "constraint_name": "FK",
            "column_name": "B",
            "referenced_schema": "dbo",
            "referenced_table": "T",
            "referenced_column": "RB",
            "on_delete": "SET_NULL",
            "on_update": "CASCADE",
            "constraint_column_id": 2,
        },
    ]
    (fk,) = md.foreign_keys(rows)
    assert fk.columns == ("A", "B")
    assert fk.referenced_columns == ("RA", "RB")
    assert fk.on_delete is ReferentialAction.SET_NULL


def test_referential_action_codes_and_descriptions() -> None:
    assert md.referential_action(0) is ReferentialAction.NO_ACTION
    assert md.referential_action(1) is ReferentialAction.CASCADE
    assert md.referential_action(2) is ReferentialAction.SET_NULL
    assert md.referential_action(3) is ReferentialAction.SET_DEFAULT
    assert md.referential_action("set default") is ReferentialAction.SET_DEFAULT
    assert md.referential_action(None) is ReferentialAction.NO_ACTION
    assert md.referential_action("something else") is ReferentialAction.NO_ACTION


def test_incoming_foreign_keys() -> None:
    rows = [
        {
            "constraint_name": "FK_Region_Country",
            "referencing_schema": "dbo",
            "referencing_table": "Region",
            "column_name": "CountryCode",
            "referenced_column": "Code",
            "on_delete": "CASCADE",
            "on_update": "NO_ACTION",
        },
    ]
    (incoming,) = md.incoming_foreign_keys(rows)
    assert incoming.ref == "dbo.Region"
    assert incoming.columns == ("CountryCode",)
    assert incoming.referenced_columns == ("Code",)
    assert incoming.on_delete is ReferentialAction.CASCADE


# -- checks and triggers ------------------------------------------------------


def test_check_constraints() -> None:
    rows = [{"constraint_name": "CK_Country_Population", "definition": "([Population]>=(0))"}]
    (check,) = md.check_constraints(rows)
    assert check.name == "CK_Country_Population"
    assert check.definition == "([Population]>=(0))"


def test_incomplete_check_rows_are_skipped() -> None:
    assert md.check_constraints([{"constraint_name": "CK", "definition": None}]) == ()


def test_after_trigger_with_multiple_events() -> None:
    rows = [
        {
            "trigger_name": "trg_Audit",
            "is_disabled": 0,
            "is_instead_of_trigger": 0,
            "event_type": 2,
        },
        {
            "trigger_name": "trg_Audit",
            "is_disabled": 0,
            "is_instead_of_trigger": 0,
            "event_type": 3,
        },
    ]
    (trigger,) = md.triggers_from_rows(rows)
    assert trigger.name == "trg_Audit"
    assert trigger.events == ("UPDATE", "DELETE")
    assert trigger.firing == "AFTER"
    assert trigger.enabled is True


def test_instead_of_trigger_and_disabled_state() -> None:
    rows = [
        {"trigger_name": "trg_View", "is_disabled": 1, "is_instead_of_trigger": 1, "event_type": 1}
    ]
    (trigger,) = md.triggers_from_rows(rows)
    assert trigger.firing == "INSTEAD OF"
    assert trigger.enabled is False
    assert trigger.events == ("INSERT",)


# -- temporal -----------------------------------------------------------------


def test_temporal_flags_for_system_versioned_table() -> None:
    rows = [{"temporal_type": 2, "history_schema": "dbo", "history_table": "CountryHistory"}]
    assert md.temporal_flags(rows) == (True, False, "dbo", "CountryHistory")


def test_temporal_flags_for_history_table() -> None:
    rows = [{"temporal_type": 1, "history_schema": "dbo", "history_table": "CountryHistory"}]
    assert md.temporal_flags(rows) == (False, True, "dbo", "CountryHistory")


def test_temporal_flags_for_plain_table() -> None:
    assert md.temporal_flags([{"temporal_type": 0}]) == (False, False, None, None)
    assert md.temporal_flags([]) == (False, False, None, None)


# -- listing ------------------------------------------------------------------


def test_table_summaries() -> None:
    rows = [
        {
            "schema_name": "dbo",
            "table_name": "Country",
            "object_type": "U",
            "has_primary_key": 1,
            "approximate_rows": 3,
            "has_triggers": 0,
            "has_foreign_keys": 1,
        },
        {
            "schema_name": "dbo",
            "table_name": "v_Country",
            "object_type": "V",
            "has_primary_key": 0,
            "approximate_rows": 0,
            "has_triggers": 1,
        },
        {
            "schema_name": "dbo",
            "table_name": "fn_x",
            "object_type": "FN",
            "has_primary_key": 0,
            "approximate_rows": None,
            "has_triggers": 0,
        },
    ]
    summaries = md.table_summaries(rows)
    # only tables and views are exposed; functions/procedures are filtered out
    assert [f"{s.schema}.{s.name}" for s in summaries] == ["dbo.Country", "dbo.v_Country"]
    assert summaries[0].kind is TableKind.BASE_TABLE
    assert summaries[0].has_primary_key is True
    assert summaries[0].approximate_row_count == 3
    assert summaries[0].has_triggers is False
    assert summaries[0].has_foreign_keys is True  # drives the 🔗 badge in the picker
    assert summaries[1].kind is TableKind.VIEW
    assert summaries[1].has_triggers is True
    assert summaries[1].has_foreign_keys is False  # absent column -> False, never None


def test_object_kind() -> None:
    assert md.object_kind("U") is TableKind.BASE_TABLE
    assert md.object_kind("V") is TableKind.VIEW
    assert md.object_kind("P") is None


def test_databases_from_rows() -> None:
    rows = [
        {"database_name": "master", "current_database": "SwissKnifeSample", "database_id": 1},
        {
            "database_name": "SwissKnifeSample",
            "current_database": "SwissKnifeSample",
            "database_id": 5,
        },
    ]
    databases = md.databases_from_rows(rows)
    assert [(d.name, d.is_current) for d in databases] == [
        ("master", False),
        ("SwissKnifeSample", True),
    ]


# -- full assembly ------------------------------------------------------------


def test_build_table_full() -> None:
    table = md.build_table(
        schema="dbo",
        name="Region",
        kind=TableKind.BASE_TABLE,
        column_rows=[
            _column(
                column_name="RegionId",
                column_id=1,
                is_identity=1,
                seed_value="1",
                increment_value="1",
                is_nullable=0,
                default_definition=None,
            ),
            _column(
                column_name="CountryCode",
                column_id=2,
                data_type="char",
                max_length=2,
                is_nullable=0,
            ),
            _column(
                column_name="NameUpper",
                column_id=3,
                data_type="nvarchar",
                max_length=200,
                is_computed=1,
                computed_definition="UPPER([Name])",
                computed_persisted=0,
            ),
            _column(
                column_name="RowVer",
                column_id=4,
                data_type="timestamp",
                max_length=8,
                is_nullable=0,
            ),
        ],
        key_rows=[
            {
                "index_name": "PK_Region",
                "is_primary_key": 1,
                "key_ordinal": 1,
                "column_name": "RegionId",
            }
        ],
        fk_rows=[
            {
                "constraint_name": "FK_Region_Country",
                "column_name": "CountryCode",
                "referenced_schema": "dbo",
                "referenced_table": "Country",
                "referenced_column": "Code",
                "on_delete": "CASCADE",
                "on_update": "NO_ACTION",
            }
        ],
        incoming_fk_rows=[
            {
                "constraint_name": "FK_RegionAlias_Region",
                "referencing_schema": "dbo",
                "referencing_table": "RegionAlias",
                "column_name": "RegionId",
                "referenced_column": "RegionId",
                "on_delete": "CASCADE",
                "on_update": "NO_ACTION",
            }
        ],
        check_rows=[{"constraint_name": "CK_Region_Name", "definition": "([Name]<>(N''))"}],
        trigger_rows=[
            {
                "trigger_name": "trg_Region",
                "is_disabled": 0,
                "is_instead_of_trigger": 0,
                "event_type": 2,
            }
        ],
        temporal_rows=[{"temporal_type": 0}],
        approximate_row_count=4,
    )
    assert [c.name for c in table.columns] == ["RegionId", "CountryCode", "NameUpper", "RowVer"]
    assert table.primary_key is not None and table.primary_key.columns == ("RegionId",)
    assert table.column("RegionId").is_primary_key
    assert table.column("CountryCode").is_foreign_key
    assert table.column("NameUpper").computed_persisted is False
    assert table.rowversion_column is not None
    assert table.approximate_row_count == 4
    assert [fk.name for fk in table.foreign_keys] == ["FK_Region_Country"]
    assert [fk.ref for fk in table.incoming_foreign_keys] == ["dbo.RegionAlias"]
    assert [c.name for c in table.check_constraints] == ["CK_Region_Name"]
    assert [t.name for t in table.triggers] == ["trg_Region"]
    assert table.is_system_versioned is False
    assert table.identity_columns == ("RegionId",)
    assert table.updatable is True


def test_build_table_without_primary_key_is_read_only() -> None:
    table = md.build_table(
        schema="dbo",
        name="Log",
        kind=TableKind.BASE_TABLE,
        column_rows=[_column(column_name="Message")],
    )
    assert table.primary_key is None
    assert table.identity_columns == ()
    assert table.updatable is False


def test_build_table_requires_columns() -> None:
    with pytest.raises(Exception, match="no columns found"):
        md.build_table(schema="dbo", name="Ghost", kind=TableKind.BASE_TABLE, column_rows=[])
