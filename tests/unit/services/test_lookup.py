"""LookupService: the foreign-key picker's data (FR-4.4).

The point of these tests is that the picker *searches server-side* and shows a key plus a
best-guess description column — because a raw identifier in a cell tells a human nothing.
"""

from pathlib import Path

from tests.fakes import FakeProvider

from sql_table_swiss_knife.domain import (
    AuthMode,
    Column,
    ConnectionProfile,
    ForeignKey,
    PrimaryKey,
    ReferentialAction,
    Table,
    TableKind,
)
from sql_table_swiss_knife.services import CatalogService, ConnectionService, LookupService
from sql_table_swiss_knife.services.lookup import description_column, lookup_predicates
from sql_table_swiss_knife.storage import EphemeralSecretStore, ProfileStore

COUNTRY = Table(
    schema="dbo",
    name="Country",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Code", 1, "char", 2, None, None, False, None, False, is_primary_key=True),
        Column("Name", 2, "nvarchar", 100, None, None, False, None, False),
    ),
    primary_key=PrimaryKey("PK_Country", ("Code",)),
)

#: A reference table whose only text column is named ``FullName``.
PERSON = Table(
    schema="dbo",
    name="Person",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("PersonId", 1, "int", None, 10, 0, False, None, True, is_primary_key=True),
        Column("FullName", 2, "nvarchar", 100, None, None, False, None, False),
        Column("Active", 3, "bit", None, 1, 0, False, None, False),
    ),
    primary_key=PrimaryKey("PK_Person", ("PersonId",)),
)

#: No non-key text column at all: the picker can only show keys.
NUMERIC_ONLY = Table(
    schema="dbo",
    name="Metric",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("MetricId", 1, "int", None, 10, 0, False, None, False, is_primary_key=True),
        Column("Value", 2, "int", None, 10, 0, False, None, False),
    ),
    primary_key=PrimaryKey("PK_Metric", ("MetricId",)),
)

KEYLESS = Table(
    schema="dbo",
    name="Audit",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Event", 1, "nvarchar", 100, None, None, True, None, False),
        Column("Detail", 2, "nvarchar", 100, None, None, True, None, False),
    ),
)


def _fk(name: str, column: str, table: str, referenced: str) -> ForeignKey:
    return ForeignKey(
        name,
        (column,),
        "dbo",
        table,
        (referenced,),
        ReferentialAction.NO_ACTION,
        ReferentialAction.NO_ACTION,
    )


FK = _fk("FK_Order_Country", "CountryCode", "Country", "Code")


def _profile() -> ConnectionProfile:
    return ConnectionProfile(
        name="catalog",
        provider="fake",
        host="localhost",
        port=1433,
        database="test",
        username="sa",
        auth=AuthMode.SQL,
    )


async def _lookup(tmp_path: Path, table: Table, rows: list[dict[str, object]]) -> LookupService:
    provider = FakeProvider([table], {table.name: rows})
    connection = ConnectionService(
        profiles=ProfileStore(tmp_path / "profiles.toml"),
        secrets=EphemeralSecretStore(),
        provider_factory=lambda name: provider,
    )
    await connection.connect(_profile())
    return LookupService(connection, CatalogService(connection))


# -- the description-column guess ----------------------------------------------


def test_a_column_called_name_wins_over_other_text_columns() -> None:
    assert _named(description_column(COUNTRY, ("Code",))) == "Name"


def test_a_conventional_name_is_recognised_case_and_separator_insensitively() -> None:
    assert _named(description_column(PERSON, ("PersonId",))) == "FullName"


def _named(column: Column | None) -> str | None:
    """The column's name, so a test asserts on the choice without a None check."""
    return column.name if column is not None else None


def test_a_table_with_only_keys_and_numbers_has_no_description() -> None:
    assert description_column(NUMERIC_ONLY, ("MetricId",)) is None


def test_a_computed_column_is_not_offered_as_a_description() -> None:
    table = Table(
        schema="dbo",
        name="Computed",
        kind=TableKind.BASE_TABLE,
        columns=(
            Column("Id", 1, "int", None, 10, 0, False, None, False, is_primary_key=True),
            Column(
                "Name",
                2,
                "nvarchar",
                50,
                None,
                None,
                True,
                None,
                False,
                is_computed=True,
                computed_definition="UPPER([Id])",
            ),
        ),
        primary_key=PrimaryKey("PK", ("Id",)),
    )
    assert description_column(table, ("Id",)) is None


# -- the search predicates -----------------------------------------------------


def test_searching_by_name_uses_a_like_over_the_description() -> None:
    description = description_column(COUNTRY, ("Code",))

    predicates = lookup_predicates(COUNTRY, ["Code"], description, "ger")

    assert len(predicates) == 1
    assert predicates[0].column == "Name"
    assert predicates[0].value == "%ger%"


def test_a_blank_search_asks_for_nothing_in_particular() -> None:
    assert lookup_predicates(COUNTRY, ["Code"], None, "   ") == ()


def test_searching_a_numeric_reference_falls_back_to_equality() -> None:
    predicates = lookup_predicates(NUMERIC_ONLY, ["MetricId"], None, "7")

    assert len(predicates) == 1
    assert predicates[0].operator.value == "="


# -- the fetched choices -------------------------------------------------------


async def test_the_picker_offers_keys_with_a_readable_label(tmp_path: Path) -> None:
    lookup = await _lookup(
        tmp_path,
        COUNTRY,
        [{"Code": "DE", "Name": "Germany"}, {"Code": "FR", "Name": "France"}],
    )

    result = await lookup.search(FK)

    assert len(result) == 2
    assert result.key_columns == ("Code",)
    assert result.description_name == "Name"
    assert result.header() == "Name"
    assert result.choices[0].key == (("Code", "DE"),)
    assert result.choices[0].label == "Germany"
    assert result.key_label(result.choices[0]) == "DE"


async def test_the_search_narrows_the_choices_server_side(tmp_path: Path) -> None:
    lookup = await _lookup(
        tmp_path,
        COUNTRY,
        [{"Code": "DE", "Name": "Germany"}, {"Code": "FR", "Name": "France"}],
    )

    result = await lookup.search(FK, "fran")

    assert [choice.label for choice in result.choices] == ["France"]
    assert result.search == "fran"


async def test_a_table_with_no_description_column_falls_back_to_the_key(
    tmp_path: Path,
) -> None:
    lookup = await _lookup(tmp_path, NUMERIC_ONLY, [{"MetricId": 7, "Value": 42}])

    result = await lookup.search(_fk("FK_X", "MetricId", "Metric", "MetricId"))

    assert result.description_name is None
    assert result.header() == "MetricId"
    assert result.choices[0].label == "7"  # the key is the label when nothing else is


async def test_a_reference_without_a_key_still_produces_choices(tmp_path: Path) -> None:
    lookup = await _lookup(tmp_path, KEYLESS, [{"Event": "login", "Detail": "user clicked"}])

    result = await lookup.search(_fk("FK_Y", "Event", "Audit", "Event"))

    assert result.choices[0].key == (("Event", "login"),)
    assert result.choices[0].label == "user clicked"  # Detail is the better label


async def test_no_matches_is_an_empty_result_not_an_error(tmp_path: Path) -> None:
    lookup = await _lookup(tmp_path, COUNTRY, [{"Code": "DE", "Name": "Germany"}])

    result = await lookup.search(FK, "zzzz")

    assert result.is_empty is True
    assert len(result) == 0
