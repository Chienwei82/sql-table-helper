"""Live tests for catalog introspection: exact types, keys, referential actions, quoting.

``tests/live/test_mssql_live.py`` covers the happy path of every ``sys.*`` query. These
cover the shapes that only exist in ``init/02_edge_fixtures.sql``: referential actions
other than CASCADE, identity from a single-column UNIQUE, a table with no identity at
all, PERSISTED computed columns, MAX/precision types, and object names that only parse
when every identifier part is bracketed correctly.

The assertions restate the fixture SQL on purpose: a change to a ``sys.*`` query then
surfaces as a concrete diff rather than as an empty table.
"""

from typing import Any

import pytest

from sql_table_swiss_knife.domain import (
    ChangeKind,
    FetchSpec,
    FilterOp,
    PendingChange,
    ReferentialAction,
    RowFilter,
    TableKind,
)
from sql_table_swiss_knife.providers.mssql.provider import MssqlProvider
from tests.live.test_mssql_writes_live import table_of

pytestmark = pytest.mark.live


async def test_a_unicode_column_reports_its_declared_character_width(
    live_connection: Any,
) -> None:
    """``sys`` counts nvarchar widths in bytes; the app must report characters.

    ``dbo.Typed.TextShort`` is declared ``nvarchar(50)`` and ``sys.columns.max_length``
    reports 100. The metadata mapper halves that once; anything that halves it again
    advertises nvarchar(25) and rejects the last 25 characters on edit. Asserted against
    the real server so the byte-to-character step cannot drift unnoticed.
    """
    from sql_table_swiss_knife.services.inspector import format_data_type
    from sql_table_swiss_knife.services.validation import _length_limit

    typed = await table_of(live_connection, "dbo", "Typed")
    column = typed.column("TextShort")

    assert column.max_length == 50
    assert format_data_type(column) == "nvarchar(50)"
    assert _length_limit(column) == 50

    raw = await live_connection.afetch(
        "SELECT max_length FROM sys.columns "
        "WHERE object_id = OBJECT_ID(N'dbo.Typed') AND name = N'TextShort'"
    )
    assert raw[0]["max_length"] == 100  # the bytes the halving above is correcting for


async def test_exact_types_for_every_awkward_column(live_connection: Any) -> None:
    """``max_length`` for MAX types must be None, not -1, and precision/scale must survive."""
    typed = await table_of(live_connection, "dbo", "Typed")

    assert typed.column("TextMax").max_length is None  # nvarchar(max)
    assert typed.column("TextShort").max_length == 50
    assert typed.column("Binary").max_length == 64
    assert typed.column("BinaryMax").max_length is None  # varbinary(max)

    amount = typed.column("Amount")
    assert amount.data_type == "decimal"
    assert (amount.precision, amount.scale) == (18, 4)

    fixed = typed.column("Fixed")
    assert fixed.data_type == "numeric"
    assert (fixed.precision, fixed.scale) == (10, 2)

    for name in ("Ts", "Offset"):
        column = typed.column(name)
        assert column.data_type.startswith("datetime")
        assert column.precision == 7
        assert column.scale == 7

    assert typed.column("Day").data_type == "date"
    assert typed.column("Moment").data_type == "time"
    assert typed.column("Moment").scale == 7
    assert typed.column("Uid").data_type == "uniqueidentifier"
    assert typed.column("Ratio").data_type == "float"
    assert typed.column("Cash").data_type == "money"
    # Non-Unicode types have no collation; nvarchar ones do.
    assert typed.column("TextShort").collation
    assert typed.column("Amount").collation is None


async def test_referential_actions_other_than_cascade(live_connection: Any) -> None:
    """SET NULL and NO ACTION must be reported distinctly from CASCADE."""
    item = await table_of(live_connection, "dbo", "Item")
    actions = {fk.name: (fk.on_delete, fk.on_update) for fk in item.foreign_keys}
    assert set(actions) == {"FK_Item_Store", "FK_Item_Supplier"}
    assert actions["FK_Item_Store"][0] is ReferentialAction.SET_NULL
    assert actions["FK_Item_Supplier"][0] is ReferentialAction.NO_ACTION

    store = await table_of(live_connection, "dbo", "Store")
    incoming = {fk.name: fk for fk in store.incoming_foreign_keys}
    assert incoming["FK_Item_Store"].on_delete is ReferentialAction.SET_NULL


async def test_composite_unique_does_not_confer_row_identity(live_connection: Any) -> None:
    """S-4/OQ-5: a multi-column UNIQUE is not a usable row identity, so rows are read-only."""
    keyless = await table_of(live_connection, "dbo", "Keyless")
    assert keyless.primary_key is None
    assert {c.name for c in keyless.unique_constraints} == {"UQ_Keyless_AB"}
    assert keyless.identity_columns == ()
    assert keyless.updatable is False


async def test_single_column_non_null_unique_is_row_identity(live_connection: Any) -> None:
    unique = await table_of(live_connection, "dbo", "UniqueOnly")
    assert unique.primary_key is None
    assert unique.identity_columns == ("Code",)
    assert unique.updatable is True


async def test_persisted_computed_column_is_flagged(live_connection: Any) -> None:
    order_line = await table_of(live_connection, "dbo", "OrderLine")
    total = order_line.column("Total")
    assert total.is_computed is True
    assert total.computed_persisted is True
    assert total.is_server_managed is True
    assert "Qty" in (total.computed_definition or "")
    assert total.nullable is True  # a PERSISTED column reports as nullable in sys.columns


async def test_non_unique_index_is_not_reported_as_a_constraint(live_connection: Any) -> None:
    """A plain index must not be mistaken for a PK or a UNIQUE constraint."""
    order_line = await table_of(live_connection, "dbo", "OrderLine")
    assert order_line.primary_key is not None
    assert order_line.primary_key.columns == ("LineId",)
    assert order_line.unique_constraints == ()
    assert order_line.column("Qty").is_primary_key is False
    assert order_line.column("Total").is_primary_key is False


async def test_temporal_pairing_is_reported_on_both_sides(live_connection: Any) -> None:
    account = await table_of(live_connection, "dbo", "Account")
    assert account.is_system_versioned is True
    assert account.history_schema == "dbo"
    assert account.history_table == "AccountHistory"
    # The period columns are GENERATED ALWAYS, but SQL Server reports that through
    # sys.columns.generated_always_type (1 = period start, 2 = period end), *not*
    # is_computed, so they are not computed columns as far as the catalog goes.
    # Writing them is rejected by the server, which is the guarantee that matters here.
    for name, kind in (("ValidFrom", 1), ("ValidTo", 2)):
        column = account.column(name)
        assert column.is_computed is False
        assert column.generated_always_type == kind

    history = await table_of(live_connection, "dbo", "AccountHistory")
    assert history.is_history_table is True
    assert history.is_system_versioned is False


async def test_identifiers_needing_bracketed_quoting_are_introspected(live_connection: Any) -> None:
    """A space, a reserved keyword and a literal ']' in a column name must all survive.

    ``get_table_metadata`` builds its ``OBJECT_ID`` target by hand as ``[schema].[table]``
    rather than through the dialect's ``quote_qualified``, so a name containing ``]`` is
    exactly where that shortcut would break. This test is what tells us whether it does.
    """
    provider = MssqlProvider()
    weird = await provider.get_table_metadata(live_connection, "Lookups", "Weird ]Name")

    assert weird.schema == "Lookups"
    assert weird.name == "Weird ]Name"
    # The literal name is "Cola]B": the fixture's [Cola]]B] escapes the bracket.
    assert [c.name for c in weird.columns] == ["Col with space", "select", "Cola]B"]
    assert weird.primary_key is not None
    assert weird.primary_key.columns == ("Col with space",)
    assert weird.column("Col with space").is_identity is True
    assert weird.column("select").data_type == "nvarchar"
    assert weird.column("select").max_length == 50


async def test_accented_schema_and_columns_are_introspected(live_connection: Any) -> None:
    provider = MssqlProvider()
    moneda = await provider.get_table_metadata(live_connection, "catálogos", "Moneda")

    assert moneda.schema == "catálogos"
    assert moneda.name == "Moneda"
    assert [c.name for c in moneda.columns] == ["Código", "Descripción"]
    assert moneda.column("Código").data_type == "char"
    assert moneda.column("Código").max_length == 3
    assert moneda.primary_key is not None
    assert moneda.primary_key.columns == ("Código",)
    assert moneda.updatable is True


async def test_list_tables_covers_non_dbo_schemas(live_connection: Any) -> None:
    """The table browser lists every schema, not just ``dbo``."""
    provider = MssqlProvider()
    summaries = await provider.list_tables(live_connection)
    refs = {f"{s.schema}.{s.name}" for s in summaries}

    assert "dbo.Country" in refs
    assert "Lookups.Weird ]Name" in refs
    assert "catálogos.Moneda" in refs

    only_lookups = await provider.list_tables(live_connection, "Lookups")
    assert [s.name for s in only_lookups] == ["Weird ]Name"]
    assert all(s.schema == "Lookups" for s in only_lookups)

    assert await provider.list_tables(live_connection, "no_such_schema") == []


async def test_list_tables_flags_triggers_and_foreign_keys(live_connection: Any) -> None:
    provider = MssqlProvider()
    by_ref = {f"{s.schema}.{s.name}": s for s in await provider.list_tables(live_connection)}

    assert by_ref["dbo.Region"].has_triggers is True
    assert by_ref["dbo.Region"].has_primary_key is True
    assert by_ref["dbo.Item"].has_foreign_keys is True
    assert by_ref["dbo.Country"].has_foreign_keys is False  # FKs point *at* it
    assert by_ref["dbo.Keyless"].has_primary_key is False
    assert by_ref["dbo.v_Country"].kind is TableKind.VIEW


async def test_approximate_row_count_is_reported(live_connection: Any) -> None:
    provider = MssqlProvider()
    by_ref = {f"{s.schema}.{s.name}": s for s in await provider.list_tables(live_connection)}
    assert by_ref["dbo.Country"].approximate_row_count == 3
    assert by_ref["dbo.UniqueOnly"].approximate_row_count == 2
    assert by_ref["dbo.Keyless"].approximate_row_count == 2


async def test_missing_schema_or_table_raises_metadata_error(live_connection: Any) -> None:
    from sql_table_swiss_knife.providers import MetadataError

    provider = MssqlProvider()
    with pytest.raises(MetadataError, match="does not exist"):
        await provider.get_table_metadata(live_connection, "dbo", "NoSuchTable")
    with pytest.raises(MetadataError, match="does not exist"):
        await provider.get_table_metadata(live_connection, "no_such_schema", "Country")


async def test_a_procedure_is_not_reported_as_a_table(live_connection: Any) -> None:
    """sys.objects holds procedures and functions too; they must not appear as tables."""
    from sql_table_swiss_knife.providers import MetadataError

    with pytest.raises((MetadataError, Exception)):
        await MssqlProvider().get_table_metadata(live_connection, "dbo", "sp_renamed")


async def test_a_table_whose_columns_are_named_like_placeholders_is_usable(
    live_connection: Any, clean_slate: None
) -> None:
    """``dbo.PlaceholderNames`` has columns called ``@p0`` and ``@p10``.

    The dialect names its own parameters ``@pN``, and the driver-bound rewrite turns those
    into ODBC's ``?``. A plain text substitution also hit the *column names*, so every
    statement came out as ``SELECT [?], [?] ... ORDER BY [?]`` and the server rejected the
    whole table with "Invalid column name '?'" (Msg 207) — unreadable, and unwritable for
    the same reason. Reads, writes and the generated SQL all have to survive the name.
    """
    provider = MssqlProvider()
    table = await provider.get_table_metadata(live_connection, "dbo", "PlaceholderNames")
    assert [c.name for c in table.columns] == ["@p0", "@p10", "Note"]

    page = await provider.fetch_rows(live_connection, table, FetchSpec(limit=10))
    assert [dict(row.values) for row in page.rows] == [
        {"@p0": 1, "@p10": "@p0", "Note": "contains @p1 marker"}
    ]

    # An UPDATE addressing the placeholder-named PK column and a data column.
    change = PendingChange(
        kind=ChangeKind.UPDATE,
        table=table.ref,
        key=(("@p0", 1),),
        before={"@p0": 1, "@p10": "@p0"},
        after={"@p0": 1, "@p10": "@p1", "Note": "updated"},
    )
    result = await provider.execute_changes(live_connection, table, [change])
    assert result.committed, result.error
    assert [
        dict(row.values)
        for row in (await provider.fetch_rows(live_connection, table, FetchSpec(limit=10))).rows
    ] == [{"@p0": 1, "@p10": "@p1", "Note": "updated"}]

    # The stored value keeps the literal text: a '@p1' filter must match, not '@p?'.
    matching = await provider.fetch_rows(
        live_connection,
        table,
        FetchSpec(limit=10, filters=(RowFilter(column="@p10", operator=FilterOp.EQ, value="@p1"),)),
    )
    assert len(matching.rows) == 1
    non_matching = await provider.fetch_rows(
        live_connection,
        table,
        FetchSpec(limit=10, filters=(RowFilter(column="@p10", operator=FilterOp.EQ, value="?"),)),
    )
    assert non_matching.rows == ()
