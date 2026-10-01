"""InspectorService: which table/column the panel shows, and the derived view (FR-6.3).

The service holds only two pieces of state (the table, the focused column); everything it
returns is re-derived from the metadata, so replacing the table can never leave a stale
detail behind.
"""

import pytest

from sql_table_swiss_knife.domain import (
    CheckConstraint,
    Column,
    PrimaryKey,
    Table,
    TableKind,
    Trigger,
)
from sql_table_swiss_knife.services.inspector import InspectorService, Severity

COLUMNS = (
    Column("Code", 1, "char", 2, None, None, False, None, False, is_primary_key=True),
    Column("Name", 2, "nvarchar", 200, None, None, False, None, False),
    Column("Upper", 3, "nvarchar", 200, None, None, True, None, False, is_computed=True),
)
COUNTRY = Table(
    schema="dbo",
    name="Country",
    kind=TableKind.BASE_TABLE,
    columns=COLUMNS,
    primary_key=PrimaryKey("PK_Country", ("Code",)),
)
REGION = Table(
    schema="dbo",
    name="Region",
    kind=TableKind.BASE_TABLE,
    columns=(Column("Id", 1, "int", None, 10, 0, False, None, True, is_primary_key=True),),
    primary_key=PrimaryKey("PK_Region", ("Id",)),
)


def test_a_fresh_service_shows_nothing() -> None:
    """Before metadata loads the panel must render an empty state, not stale content."""
    inspector = InspectorService()

    assert inspector.table is None
    assert inspector.columns() == ()
    assert inspector.warnings() == ()
    assert inspector.headers() == ()
    assert inspector.detail() is None
    assert inspector.summary_rows() == ()


def test_showing_a_table_focuses_its_first_column() -> None:
    inspector = InspectorService()
    inspector.show(COUNTRY)

    assert inspector.table is COUNTRY
    assert inspector.focused_column_name == "Code"
    assert inspector.focused_column is COLUMNS[0]


def test_focus_moves_the_column_detail() -> None:
    """FR-6.2: the detail section follows the focused column."""
    inspector = InspectorService(COUNTRY)
    inspector.focus("Name")

    detail = inspector.detail()
    assert detail is not None
    assert detail.column.name == "Name"
    assert dict(detail.rows)["type"] == "nvarchar(200)"


def test_focus_ignores_a_column_that_no_longer_exists() -> None:
    """A stale name from the grid degrades to "no detail" instead of raising."""
    inspector = InspectorService(COUNTRY)
    inspector.focus("Dropped")

    assert inspector.focused_column is None
    assert inspector.detail() is None


def test_showing_another_table_replaces_everything() -> None:
    """No caching: a new table means new summary, warnings, headers and detail."""
    inspector = InspectorService(COUNTRY, focused_column="Name")
    inspector.show(REGION)

    assert inspector.column_names() == ("Id",)
    assert inspector.headers() == ("Id 🔑#✱ 🔒  int",)  # 🔒: identity is read-only
    detail = inspector.detail()
    assert detail is not None and detail.column.name == "Id"


def test_clear_forgets_the_table() -> None:
    """Leaving the screen (or a failed read) must not leave the previous table on screen."""
    inspector = InspectorService(COUNTRY)
    inspector.clear()

    assert inspector.table is None
    assert inspector.focused_column is None
    assert inspector.warnings() == ()


def test_headers_carry_the_badges_and_types_of_every_column() -> None:
    inspector = InspectorService(COUNTRY)

    assert inspector.headers() == (
        "Code 🔑✱  char(2)",
        "Name ✱  nvarchar(200)",
        "Upper ƒ∅ 🔒  nvarchar(200)",  # 🔒: computed is read-only
    )


def test_columns_pairs_each_column_with_its_badges() -> None:
    inspector = InspectorService(COUNTRY)
    listed = inspector.columns()

    assert [column.name for column, _ in listed] == ["Code", "Name", "Upper"]
    assert listed[0][1][0].glyph == "🔑"


def test_warnings_expose_the_severity_roll_up_the_banner_needs() -> None:
    """The banner counts errors and warnings; both are reachable from the model."""
    subject = Table(
        schema="dbo",
        name="v_X",
        kind=TableKind.VIEW,
        columns=(Column("A", 1, "int", None, 10, 0, True, None, False),),
        check_constraints=(CheckConstraint("CK_A", "([A]>(0))"),),
        triggers=(Trigger("trg_IO", ("INSERT",), "INSTEAD OF"),),
    )
    inspector = InspectorService(subject)
    warnings = inspector.warnings()

    assert any(warning.severity is Severity.ERROR for warning in warnings)
    assert any(warning.severity is Severity.WARNING for warning in warnings)


@pytest.mark.parametrize("column_name", [None, "Nope"])
def test_detail_is_none_without_a_focused_column(column_name: str | None) -> None:
    """No focus, no detail — the panel says so instead of showing the wrong column."""
    inspector = InspectorService(COUNTRY, focused_column=column_name)
    assert inspector.detail() is None
