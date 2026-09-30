"""Shared fixtures: well-formed table metadata for tests."""

from collections.abc import Iterator

import pytest

from sql_table_swiss_knife import providers
from sql_table_swiss_knife.domain import (
    CheckConstraint,
    Column,
    ForeignKey,
    PrimaryKey,
    ReferentialAction,
    Table,
    TableKind,
    Trigger,
    UniqueConstraint,
)

# Country: single-column PK, UNIQUE name, CHECK, default, computed, rowversion.
COUNTRY_COLUMNS = (
    Column("Code", 1, "char", 2, None, None, False, None, False, is_primary_key=True),
    Column("Name", 2, "nvarchar", 100, None, None, False, None, False),
    Column("Population", 3, "int", None, 10, 0, False, "0", False),
    Column(
        "NameUpper",
        4,
        "nvarchar",
        100,
        None,
        None,
        True,
        None,
        False,
        is_computed=True,
        computed_definition="UPPER([Name])",
    ),
    Column("RowVer", 5, "rowversion", 8, None, None, False, None, False, is_rowversion=True),
)


@pytest.fixture
def country_table() -> Table:
    return Table(
        schema="dbo",
        name="Country",
        kind=TableKind.BASE_TABLE,
        columns=COUNTRY_COLUMNS,
        primary_key=PrimaryKey("PK_Country", ("Code",)),
        unique_constraints=(UniqueConstraint("UQ_Country_Name", ("Name",)),),
        check_constraints=(CheckConstraint("CK_Country_Population", "([Population]>=(0))"),),
        triggers=(Trigger("trg_Country_AfterUpdate", ("UPDATE",), "AFTER"),),
        approximate_row_count=3,
    )


@pytest.fixture
def region_table() -> Table:
    return Table(
        schema="dbo",
        name="Region",
        kind=TableKind.BASE_TABLE,
        columns=(
            Column(
                "RegionId",
                1,
                "int",
                None,
                10,
                0,
                False,
                None,
                True,
                identity_seed=1,
                identity_increment=1,
                is_primary_key=True,
            ),
            Column(
                "CountryCode",
                2,
                "char",
                2,
                None,
                None,
                False,
                None,
                False,
                is_foreign_key=True,
            ),
            Column("ParentRegionId", 3, "int", None, 10, 0, True, None, False, is_foreign_key=True),
            Column("Name", 4, "nvarchar", 100, None, None, False, None, False),
            Column(
                "NameUpper",
                5,
                "nvarchar",
                100,
                None,
                None,
                True,
                None,
                False,
                is_computed=True,
                computed_definition="UPPER([Name])",
            ),
            Column(
                "RowVer",
                6,
                "rowversion",
                8,
                None,
                None,
                False,
                None,
                False,
                is_rowversion=True,
            ),
            Column("SortOrder", 7, "int", None, 10, 0, False, "0", False),
        ),
        primary_key=PrimaryKey("PK_Region", ("RegionId",)),
        foreign_keys=(
            ForeignKey(
                "FK_Region_Country",
                ("CountryCode",),
                "dbo",
                "Country",
                ("Code",),
                ReferentialAction.CASCADE,
                ReferentialAction.NO_ACTION,
            ),
            ForeignKey(
                "FK_Region_Parent",
                ("ParentRegionId",),
                "dbo",
                "Region",
                ("RegionId",),
                ReferentialAction.NO_ACTION,
                ReferentialAction.NO_ACTION,
            ),
        ),
        check_constraints=(CheckConstraint("CK_Region_Name", "([Name]<>N'')"),),
    )


@pytest.fixture
def country_rows() -> list[dict[str, object]]:
    return [
        {"Code": "DE", "Name": "Germany", "Population": 83000000, "RowVer": b"\x01"},
        {"Code": "FR", "Name": "France", "Population": 67000000, "RowVer": b"\x02"},
        {"Code": "JP", "Name": "Japan", "Population": 125000000, "RowVer": b"\x03"},
        {"Code": "US", "Name": "United States", "Population": 331000000, "RowVer": b"\x04"},
        {"Code": "CH", "Name": "Switzerland", "Population": 8700000, "RowVer": b"\x05"},
    ]


@pytest.fixture(autouse=True)
def clean_provider_registry() -> Iterator[None]:
    """Isolate the global provider registry between tests."""
    snapshot = dict(providers._PROVIDERS)
    providers._PROVIDERS.clear()
    yield
    providers._PROVIDERS.clear()
    providers._PROVIDERS.update(snapshot)
