"""The SQL panel's logic: three renderings, copy targets, generated statements (FR-5).

All of this is pure — a table, a change set and a dialect in, text out — so the panel's
behaviour is asserted here without a terminal. The three questions these tests keep honest:

* does the literal rendering really carry the same values as the parameterized one (FR-5.4)?
* is the script genuinely all-or-nothing, and does it only add ``SET IDENTITY_INSERT`` when
  that is both needed and legal?
* does every "generate SQL for…" action produce a statement, or refuse with a reason?
"""

import pytest

from sql_table_swiss_knife.domain.catalog import Column, PrimaryKey, Table, TableKind
from sql_table_swiss_knife.domain.changes import ChangeKind, PendingChange
from sql_table_swiss_knife.providers.mssql import TSqlDialect
from sql_table_swiss_knife.services.sqlpreview import (
    PREVIEW_ONLY_NOTE,
    RowAction,
    SqlMode,
    SqlPreview,
    entries_for,
    generate_for,
    next_mode,
)


@pytest.fixture
def dialect() -> TSqlDialect:
    return TSqlDialect()


def _table(*, identity: bool = False) -> Table:
    """A catalog table: PK ``Code``, a name, a population, a computed and a rowversion."""
    return Table(
        schema="dbo",
        name="Country",
        kind=TableKind.BASE_TABLE,
        columns=(
            Column("Code", 1, "char", 2, None, None, False, None, identity, is_primary_key=True),
            Column("Name", 2, "nvarchar", 200, None, None, True, None, False),
            Column("Population", 3, "int", None, 10, 0, False, None, False),
            Column(
                "NameUpper", 4, "nvarchar", 200, None, None, True, None, False, is_computed=True
            ),
            Column(
                "RowVer", 5, "rowversion", 8, None, None, False, None, False, is_rowversion=True
            ),
        ),
        primary_key=PrimaryKey("PK_Country", ("Code",)),
    )


@pytest.fixture
def table() -> Table:
    return _table()


def _changes() -> list[PendingChange]:
    """One of each kind, in staging order."""
    ref = _table().ref
    return [
        PendingChange(
            ChangeKind.UPDATE,
            ref,
            key=(("Code", "DE"),),
            before={"Code": "DE", "Name": "alt", "Population": 1},
            after={"Code": "DE", "Name": "neu", "Population": 1},
        ),
        PendingChange(ChangeKind.INSERT, ref, after={"Code": "FR", "Name": "O'Brien"}),
        PendingChange(
            ChangeKind.DELETE, ref, key=(("Code", "JP"),), before={"Code": "JP", "Name": "old"}
        ),
    ]


# -- the three renderings ----------------------------------------------------


def test_entries_are_in_staging_order(dialect: TSqlDialect, table: Table) -> None:
    """The panel shows the plan in the order Apply runs it, not a sorted summary (FR-5.1)."""
    entries = entries_for(dialect, table, _changes())
    assert [entry.kind for entry in entries] == [
        ChangeKind.UPDATE,
        ChangeKind.INSERT,
        ChangeKind.DELETE,
    ]
    assert [entry.index for entry in entries] == [1, 2, 3]


def test_every_mode_renders_the_same_values(dialect: TSqlDialect, table: Table) -> None:
    """FR-5.4: the three renderings must describe the same change.

    Checked on the *values* rather than the whole text: the renderings legitimately differ
    in shape, but a value present in one and absent from another would be a lie.
    """
    entries = entries_for(dialect, table, _changes())
    literal = "\n".join(entry.text(SqlMode.LITERAL) for entry in entries)
    script = SqlPreview(table=table, dialect=dialect, entries=entries).script()
    for value in ("N'neu'", "N'FR'", "N'O''Brien'", "N'DE'", "N'JP'"):
        assert value in literal
        assert value in script
    # The parameterized rendering carries no values at all — only placeholders.
    parameterized = "\n".join(entry.text(SqlMode.PARAMETERIZED) for entry in entries)
    assert "@p0" in parameterized
    assert "neu" not in parameterized


def test_the_script_mode_body_is_the_script(dialect: TSqlDialect, table: Table) -> None:
    entries = entries_for(dialect, table, _changes())
    preview = SqlPreview(table=table, dialect=dialect, entries=entries, mode=SqlMode.SCRIPT)
    assert preview.body() == preview.script()
    assert "BEGIN TRANSACTION;" in preview.body()


def test_parameterized_mode_carries_a_parameter_legend(dialect: TSqlDialect, table: Table) -> None:
    """A bare ``@p0`` is not a statement anyone can use; the legend completes it (FR-5.2a)."""
    entries = entries_for(dialect, table, _changes())
    legend = entries[0].parameter_legend()
    # Values are rendered as SQL, not as Python repr.
    assert "@p0 = N'neu'  (Name)" in legend
    assert "@p1 = N'DE'  (Code)" in legend


def test_the_legend_renders_binary_as_hexadecimal(dialect: TSqlDialect) -> None:
    """A ``varbinary`` bound as bytes must read as ``0x…`` — the form the server receives.

    Python's ``repr`` would show ``b'\\x01\\xff'``, which is not valid T-SQL and would send
    the reader hunting for the difference.
    """
    table = Table(
        schema="dbo",
        name="Doc",
        kind=TableKind.BASE_TABLE,
        columns=(
            Column("Id", 1, "int", None, 10, 0, False, None, False, is_primary_key=True),
            Column("Body", 2, "varbinary", None, None, None, False, None, False),
        ),
        primary_key=PrimaryKey("PK_Doc", ("Id",)),
    )
    change = PendingChange(ChangeKind.INSERT, table.ref, after={"Id": 1, "Body": b"\x01\xff"})
    legend = entries_for(dialect, table, [change])[0].parameter_legend()
    assert "@p1 = 0x01FF  (Body)" in legend
    assert "b'" not in legend


def test_server_managed_columns_are_never_written(dialect: TSqlDialect) -> None:
    """A computed/rowversion column has no value the client can supply (S-3).

    Two different, both-correct behaviours: *staging* refuses such a change outright, while
    a *generated* statement silently drops those columns — it is asked to describe a whole
    row, and the target environment must keep its own computed/rowversion values.
    """
    table = _table()
    with pytest.raises(ValueError, match="computed column"):
        entries_for(
            dialect,
            table,
            [PendingChange(ChangeKind.INSERT, table.ref, after={"Code": "X", "NameUpper": "N"})],
        )
    generated = generate_for(
        dialect,
        table,
        RowAction.INSERT,
        values={"Code": "X", "Name": "n", "NameUpper": "N", "RowVer": b"\x01"},
    )
    assert "[NameUpper]" not in generated
    assert "[RowVer]" not in generated
    assert "[Code]" in generated and "[Name]" in generated


# -- the script --------------------------------------------------------------


def test_the_script_wraps_everything_in_one_transaction(dialect: TSqlDialect, table: Table) -> None:
    entries = entries_for(dialect, table, _changes())
    script = SqlPreview(table=table, dialect=dialect, entries=entries).script()
    assert script.count("BEGIN TRANSACTION;") == 1
    assert script.count("COMMIT TRANSACTION;") == 1
    assert "BEGIN TRY" in script and "BEGIN CATCH" in script
    # Every staged statement is in the script, in apply order.
    positions = [script.index(text) for text in ("N'neu'", "N'FR'", "N'JP'")]
    assert positions == sorted(positions)


def test_the_script_rolls_back_on_error(dialect: TSqlDialect, table: Table) -> None:
    """S-7: nothing is applied if any statement fails — that is what the CATCH is for."""
    entries = entries_for(dialect, table, _changes())
    script = SqlPreview(table=table, dialect=dialect, entries=entries).script()
    assert script.index("COMMIT TRANSACTION;") < script.index("BEGIN CATCH")
    assert "ROLLBACK TRANSACTION;" in script


def test_identity_insert_is_only_emitted_when_it_is_legal(dialect: TSqlDialect) -> None:
    """``SET IDENTITY_INSERT`` on a table with no IDENTITY column is a *runtime* error in
    SQL Server, so the script must not contain it there — even in identity_insert mode."""
    plain = _table(identity=False)
    changes = [PendingChange(ChangeKind.INSERT, plain.ref, after={"Code": "X", "Name": "n"})]
    script = SqlPreview(
        table=plain,
        dialect=dialect,
        entries=entries_for(dialect, plain, changes),
        identity_insert=True,
    ).script()
    assert "IDENTITY_INSERT" not in script

    keyed = _table(identity=True)
    changes = [PendingChange(ChangeKind.INSERT, keyed.ref, after={"Code": "X", "Name": "n"})]
    entries = entries_for(dialect, keyed, changes, identity_insert=True)
    script = SqlPreview(
        table=keyed, dialect=dialect, entries=entries, identity_insert=True
    ).script()
    assert "SET IDENTITY_INSERT [dbo].[Country] ON;" in script
    assert "SET IDENTITY_INSERT [dbo].[Country] OFF;" in script
    # The identity value is written explicitly, which is the whole point of the opt-in.
    assert "[Code]" in script


# -- copy targets (FR-5.5) ---------------------------------------------------


def test_copy_all_is_always_the_runnable_script(dialect: TSqlDialect, table: Table) -> None:
    """Whatever the panel shows, "copy script" copies the thing that can actually be run.

    Copying a *fragment* of a transaction would be a trap: it looks runnable and is not.
    """
    entries = entries_for(dialect, table, _changes())
    for mode in SqlMode:
        preview = SqlPreview(table=table, dialect=dialect, entries=entries, mode=mode)
        assert preview.copy_all() == preview.script()
        assert "BEGIN TRANSACTION;" in preview.copy_all()


def test_copy_entry_follows_the_mode(dialect: TSqlDialect, table: Table) -> None:
    entries = entries_for(dialect, table, _changes())
    literal = SqlPreview(table=table, dialect=dialect, entries=entries, mode=SqlMode.LITERAL)
    assert literal.copy_entry(1) == entries[0].text(SqlMode.LITERAL)
    assert "BEGIN TRANSACTION;" not in literal.copy_entry(1)


def test_copying_a_parameterized_statement_includes_its_values(
    dialect: TSqlDialect, table: Table
) -> None:
    """``@p0`` with no values is not a statement; the copy has to stand on its own."""
    entries = entries_for(dialect, table, _changes())
    preview = SqlPreview(table=table, dialect=dialect, entries=entries, mode=SqlMode.PARAMETERIZED)
    copied = preview.copy_entry(1)
    assert "@p0" in copied
    assert "N'neu'" in copied


def test_copying_a_row_that_does_not_exist_is_empty(dialect: TSqlDialect, table: Table) -> None:
    preview = SqlPreview(
        table=table, dialect=dialect, entries=entries_for(dialect, table, _changes())
    )
    assert preview.copy_entry(99) == ""
    assert preview.entry(0) is None


# -- the generated statements (FR-5.6) --------------------------------------


def test_generate_select_for_one_row(dialect: TSqlDialect, table: Table) -> None:
    sql = generate_for(dialect, table, RowAction.SELECT, key=(("Code", "DE"),))
    assert sql.startswith("SELECT ")
    assert "FROM [dbo].[Country]" in sql
    assert "WHERE [Code] = N'DE'" in sql


def test_generate_select_uses_is_null_for_a_null_key_part(dialect: TSqlDialect) -> None:
    """``WHERE [col] = NULL`` is never true, so a NULL key part must become ``IS NULL``."""
    sql = generate_for(dialect, _table(), RowAction.SELECT, key=(("Name", None),))
    assert "[Name] IS NULL" in sql


def test_generate_update_and_delete_for_one_row(dialect: TSqlDialect, table: Table) -> None:
    values = {"Code": "DE", "Name": "neu", "Population": 1}
    key = (("Code", "DE"),)
    update = generate_for(dialect, table, RowAction.UPDATE, key=key, values=values)
    assert update.startswith("UPDATE [dbo].[Country] SET")
    assert "N'neu'" in update
    delete = generate_for(dialect, table, RowAction.DELETE, key=key)
    assert delete.startswith("DELETE FROM [dbo].[Country]")


def test_generate_merge_is_an_upsert_by_key(dialect: TSqlDialect, table: Table) -> None:
    """Moving a row between environments should update the row that is already there."""
    sql = generate_for(dialect, table, RowAction.MERGE, values={"Code": "DE", "Name": "neu"})
    assert sql.startswith("MERGE INTO [dbo].[Country] AS target")
    assert "ON target.[Code] = source.[Code]" in sql
    assert "WHEN MATCHED THEN" in sql
    assert "WHEN NOT MATCHED BY TARGET THEN" in sql
    assert sql.endswith(";")  # MERGE requires a terminating semicolon


def test_generate_merge_needs_a_key(dialect: TSqlDialect) -> None:
    """MERGE without a match condition has no meaning, so a keyless table is refused."""
    table = Table(
        schema="dbo",
        name="AuditLog",
        kind=TableKind.BASE_TABLE,
        columns=(Column("At", 1, "datetime2", None, None, None, True, None, False),),
    )
    with pytest.raises(ValueError, match="needs a key"):
        generate_for(dialect, table, RowAction.MERGE, values={"At": "2024-01-01"})


def test_insert_script_batches_every_row(dialect: TSqlDialect, table: Table) -> None:
    """The point of this action: one statement to paste, not hundreds (FR-5.6)."""
    rows = [
        {"Code": "DE", "Name": "Germany", "Population": 83, "RowVer": b"\x01"},
        {"Code": "FR", "Name": "France", "Population": 67, "RowVer": b"\x02"},
    ]
    sql = generate_for(dialect, table, RowAction.INSERT_SCRIPT, rows=rows)
    assert sql.count("INSERT INTO") == 1
    assert "N'DE'" in sql and "N'FR'" in sql
    # Server-managed columns are dropped, so the target environment keeps its own.
    assert "[RowVer]" not in sql
    assert "[NameUpper]" not in sql


def test_insert_script_fills_missing_values_with_null(dialect: TSqlDialect, table: Table) -> None:
    """Rows of different shapes still produce one aligned batch."""
    rows = [{"Code": "DE", "Name": "Germany"}, {"Code": "FR"}]
    sql = generate_for(dialect, table, RowAction.INSERT_SCRIPT, rows=rows)
    assert "(N'FR', NULL)" in sql


def test_insert_script_needs_rows(dialect: TSqlDialect, table: Table) -> None:
    with pytest.raises(ValueError, match="no rows"):
        generate_for(dialect, table, RowAction.INSERT_SCRIPT, rows=[])


def test_generating_without_the_needed_input_is_refused_with_a_reason(
    dialect: TSqlDialect, table: Table
) -> None:
    """A refusal must say *why*, so the UI can show it instead of an empty panel."""
    with pytest.raises(ValueError, match="needs a row key"):
        generate_for(dialect, table, RowAction.SELECT)
    with pytest.raises(ValueError, match="needs the row's values"):
        generate_for(dialect, table, RowAction.INSERT)
    with pytest.raises(ValueError, match="needs a row key"):
        generate_for(dialect, table, RowAction.DELETE)


def test_a_generated_statement_replaces_the_change_list(dialect: TSqlDialect, table: Table) -> None:
    """Showing both at once would be ambiguous: the panel is looking at one thing."""
    entries = entries_for(dialect, table, _changes())
    preview = SqlPreview(table=table, dialect=dialect, entries=entries)
    generated = preview.with_generated("SELECT 1", "SELECT")
    assert generated.body() == "SELECT 1"
    assert generated.copy_all() == "SELECT 1"
    assert generated.summary() == "SELECT"


# -- panel state -------------------------------------------------------------


def test_the_mode_key_cycles_and_wraps() -> None:
    assert next_mode(SqlMode.PARAMETERIZED) is SqlMode.LITERAL
    assert next_mode(SqlMode.LITERAL) is SqlMode.SCRIPT
    assert next_mode(SqlMode.SCRIPT) is SqlMode.PARAMETERIZED


def test_an_empty_preview_explains_itself(dialect: TSqlDialect, table: Table) -> None:
    """An empty panel the user must decode is a worse experience than one sentence."""
    preview = SqlPreview(table=table, dialect=dialect)
    assert preview.is_empty
    assert preview.summary() == "nothing staged"
    assert "no pending changes" in preview.body()
    assert preview.script() == ""


def test_the_panel_says_it_never_executes() -> None:
    """S-1: the user must never have to wonder whether looking at SQL writes anything."""
    assert "nothing is executed" in PREVIEW_ONLY_NOTE


def test_the_summary_counts_each_kind(dialect: TSqlDialect, table: Table) -> None:
    entries = entries_for(dialect, table, _changes())
    preview = SqlPreview(table=table, dialect=dialect, entries=entries)
    assert preview.summary() == "3 statements · 1 insert, 1 update, 1 delete"
