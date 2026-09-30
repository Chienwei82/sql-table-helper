"""CatalogService: listing, caching, and the pure filter/group helpers (M3).

The filter and grouping functions are what makes the picker feel instant (FR-2.1), so
they are tested directly and exhaustively — no database, no app (NFR-5).
"""

from pathlib import Path

import pytest
from tests.fakes import FakeProvider

from sql_table_swiss_knife.domain import (
    AuthMode,
    Column,
    ConnectionProfile,
    PrimaryKey,
    Table,
    TableKind,
    TableSummary,
)
from sql_table_swiss_knife.providers import MetadataError
from sql_table_swiss_knife.services import (
    CatalogService,
    ConnectionService,
    filter_summaries,
    group_by_schema,
)
from sql_table_swiss_knife.storage import EphemeralSecretStore, ProfileStore

COUNTRY = TableSummary("dbo", "Country", TableKind.BASE_TABLE, has_primary_key=True)
ORDER = TableSummary(
    "sales", "Order", TableKind.BASE_TABLE, has_primary_key=True, has_triggers=True
)
AUDIT = TableSummary("dbo", "AuditLog", TableKind.BASE_TABLE)  # no PK -> read-only
CUSTOMER_VIEW = TableSummary("reporting", "v_Customer", TableKind.VIEW)

ALL_SUMMARIES = (COUNTRY, ORDER, AUDIT, CUSTOMER_VIEW)


def _profile() -> ConnectionProfile:
    return ConnectionProfile(
        name="catalog",
        provider="fake",
        host="localhost",
        database="test",
        username="sa",
        auth=AuthMode.SQL,
    )


def _table(name: str = "Country") -> Table:
    return Table(
        schema="dbo",
        name=name,
        kind=TableKind.BASE_TABLE,
        columns=(Column("Code", 1, "char", 2, None, None, False, None, False),),
        primary_key=PrimaryKey(f"PK_{name}", ("Code",)),
    )


async def _connected_catalog(
    tmp_path: Path, tables: tuple[Table, ...] = ()
) -> tuple[CatalogService, ConnectionService]:
    """A catalog service with an open session against a fake provider."""
    provider = FakeProvider(tables)
    connection = ConnectionService(
        profiles=ProfileStore(tmp_path / "profiles.toml"),
        secrets=EphemeralSecretStore(),
        provider_factory=lambda name: provider,
    )
    catalog = CatalogService(connection)
    await connection.connect(_profile())
    return catalog, connection


# -- pure helpers -------------------------------------------------------


def test_filter_matches_case_insensitively_on_reference_and_name() -> None:
    """FR-2.1: ``order``, ``ORDER`` and ``sales.order`` all find ``sales.Order``."""
    assert filter_summaries(ALL_SUMMARIES, "") is ALL_SUMMARIES  # no query: no copy
    assert filter_summaries(ALL_SUMMARIES, "  ") is ALL_SUMMARIES
    assert [s.name for s in filter_summaries(ALL_SUMMARIES, "order")] == ["Order"]
    assert [s.name for s in filter_summaries(ALL_SUMMARIES, "ORDER")] == ["Order"]
    assert [s.name for s in filter_summaries(ALL_SUMMARIES, "sales.order")] == ["Order"]
    assert [s.name for s in filter_summaries(ALL_SUMMARIES, "audit")] == ["AuditLog"]
    assert filter_summaries(ALL_SUMMARIES, "nothing-matches") == ()


def test_filter_also_searches_the_schema_name() -> None:
    """Typing a schema name narrows to that schema's objects."""
    summaries = (COUNTRY, ORDER)
    assert {s.name for s in filter_summaries(summaries, "sales")} == {"Order"}
    assert {s.name for s in filter_summaries(summaries, "dbo")} == {"Country"}


def test_group_by_schema_sorts_both_levels_and_skips_empty_schemas() -> None:
    """FR-2.2: headers are sorted case-insensitively; emptied schemas disappear."""
    groups = group_by_schema((ORDER, AUDIT, COUNTRY))
    assert [group.schema for group in groups] == ["dbo", "sales"]
    assert [s.name for s in groups[0].tables] == ["AuditLog", "Country"]
    assert groups[0].label == "dbo (2)"

    # Filtering first is what makes a schema with no match produce no group at all.
    filtered = group_by_schema(filter_summaries((ORDER, AUDIT, COUNTRY), "country"))
    assert [group.schema for group in filtered] == ["dbo"]
    assert [s.name for s in filtered[0].tables] == ["Country"]


def test_group_by_schema_on_empty_input() -> None:
    assert group_by_schema(()) == ()


# -- service ------------------------------------------------------------


async def test_listing_requires_a_connection(tmp_path: Path) -> None:
    """Calling a catalog method while disconnected is a programming error."""
    catalog = CatalogService(
        ConnectionService(
            profiles=ProfileStore(tmp_path / "profiles.toml"),
            secrets=EphemeralSecretStore(),
            provider_factory=lambda name: FakeProvider(),
        )
    )
    with pytest.raises(RuntimeError, match="not connected"):
        await catalog.list_tables()


async def test_list_tables_is_cached_until_refreshed(tmp_path: Path) -> None:
    """The picker filters on every keystroke; the server is read once per session."""
    catalog, connection = await _connected_catalog(tmp_path, (_table(),))

    first = await catalog.list_tables()
    second = await catalog.list_tables()
    assert first is second  # served from the cache: no second round trip
    assert [s.name for s in first] == ["Country"]

    refreshed = await catalog.list_tables(refresh=True)
    assert [s.name for s in refreshed] == ["Country"]
    assert connection.is_connected  # the session survives a refresh


async def test_metadata_is_cached_and_invalidatable(tmp_path: Path) -> None:
    catalog, _ = await _connected_catalog(tmp_path, (_table(),))

    first = await catalog.get_table("dbo", "Country")
    assert await catalog.get_table("dbo", "Country") is first

    catalog.invalidate()
    assert await catalog.get_table("dbo", "Country") == first


async def test_unknown_table_raises_a_metadata_error(tmp_path: Path) -> None:
    catalog, _ = await _connected_catalog(tmp_path)
    with pytest.raises(MetadataError):
        await catalog.get_table("dbo", "Nope")


async def test_list_databases_flags_the_current_one(tmp_path: Path) -> None:
    catalog, _ = await _connected_catalog(tmp_path)
    databases = await catalog.list_databases()
    assert [db.name for db in databases] == ["test"]
    assert databases[0].is_current
