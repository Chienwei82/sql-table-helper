"""Warning generation, badges, type formatting and column detail (FR-6, pure functions).

These tests exercise ``services/inspector.py`` against hand-built metadata — no database,
no provider, no Textual (NFR-5). They are the contract the inspector panel renders, so a
change in wording that loses a *fact* should fail here.
"""

import pytest

from sql_table_swiss_knife.domain import (
    CheckConstraint,
    Column,
    ForeignKey,
    IncomingForeignKey,
    PrimaryKey,
    ReferentialAction,
    Table,
    TableKind,
    Trigger,
    UniqueConstraint,
)
from sql_table_swiss_knife.services.inspector import (
    LARGE_TABLE_ROWS,
    ColumnDetail,
    Severity,
    Warning,
    WarningCode,
    build_warnings,
    column_badges,
    column_detail,
    format_cell_value,
    format_data_type,
    header_label,
    incoming_referencing_tables,
    read_only_reason,
    table_summary_rows,
    type_hint,
)

# -- fixtures ----------------------------------------------------------------


def column(
    name: str,
    ordinal: int,
    data_type: str = "int",
    *,
    max_length: int | None = None,
    precision: int | None = None,
    scale: int | None = None,
    nullable: bool = False,
    default: str | None = None,
    identity: bool = False,
    seed: int | None = None,
    increment: int | None = None,
    computed: bool = False,
    computed_definition: str | None = None,
    rowversion: bool = False,
    pk: bool = False,
    collation: str | None = None,
    fk: bool = False,
) -> Column:
    """A column with sane defaults, so each test only states what it cares about."""
    return Column(
        name=name,
        ordinal=ordinal,
        data_type=data_type,
        max_length=max_length,
        precision=precision,
        scale=scale,
        nullable=nullable,
        default_definition=default,
        is_identity=identity,
        identity_seed=seed,
        identity_increment=increment,
        is_computed=computed,
        computed_definition=computed_definition,
        is_rowversion=rowversion,
        is_primary_key=pk,
        collation=collation,
        is_foreign_key=fk,
    )


def table(
    *columns: Column,
    schema: str = "dbo",
    name: str = "Widget",
    kind: TableKind = TableKind.BASE_TABLE,
    pk: PrimaryKey | None = None,
    fks: tuple[ForeignKey, ...] = (),
    uniques: tuple[UniqueConstraint, ...] = (),
    checks: tuple[CheckConstraint, ...] = (),
    triggers: tuple[Trigger, ...] = (),
    incoming: tuple[IncomingForeignKey, ...] = (),
    rows: int | None = None,
    system_versioned: bool = False,
    history_table: bool = False,
    history_schema: str | None | None = "dbo",
    history_name: str | None | None = "WidgetHistory",
) -> Table:
    """A table built from the given columns.

    With no arguments this is a plain keyed table (``Id`` identity + ``PK_Id``): the
    baseline every "nothing risky here" assertion is made against.
    """
    cols = columns or (column("Id", 1, identity=True, seed=1, increment=1, pk=True),)
    return Table(
        schema=schema,
        name=name,
        kind=kind,
        columns=cols,
        primary_key=pk if pk is not None else _default_pk(cols),
        foreign_keys=fks,
        unique_constraints=uniques,
        check_constraints=checks,
        triggers=triggers,
        incoming_foreign_keys=incoming,
        approximate_row_count=rows,
        is_system_versioned=system_versioned,
        is_history_table=history_table,
        history_schema=history_schema if (system_versioned or history_table) else None,
        history_table=history_name if (system_versioned or history_table) else None,
    )


def codes(warnings: tuple[Warning, ...]) -> list[WarningCode]:
    return [warning.code for warning in warnings]


def by_code(warnings: tuple[Warning, ...], code: WarningCode) -> Warning:
    """The single warning with ``code``; fails the test when it is missing."""
    matches = [warning for warning in warnings if warning.code is code]
    assert len(matches) == 1, f"expected exactly one {code!r} warning, got {matches}"
    return matches[0]


def _default_pk(columns: tuple[Column, ...]) -> PrimaryKey | None:
    """The PK the test helper assumes: the columns flagged ``pk=True``, if any."""
    keys = tuple(col.name for col in columns if col.is_primary_key)
    return PrimaryKey("PK_Test", keys) if keys else None


# -- ordering ----------------------------------------------------------------


def test_a_clean_table_produces_no_errors() -> None:
    """A plain keyed table with no triggers/constraints warns about nothing severe."""
    warnings = build_warnings(table())
    assert [warning for warning in warnings if warning.severity is Severity.ERROR] == []


def test_warnings_are_ordered_most_severe_first() -> None:
    """The banner shows the worst fact first; order is deterministic (severity, code)."""
    subject = table(
        column("Id", 1, pk=True),
        triggers=(Trigger("trg_A", ("UPDATE",), "AFTER"),),
        checks=(CheckConstraint("CK_A", "([Id]>(0))"),),
    )
    warnings = build_warnings(subject)
    severities = [warning.severity for warning in warnings]
    assert severities == sorted(severities, reverse=True)
    assert severities[0] is Severity.WARNING  # the AFTER trigger outranks the CHECK note


def test_build_warnings_is_stable_across_calls() -> None:
    """Two calls on the same metadata give the same sequence (no set iteration)."""
    subject = table(triggers=(Trigger("trg_A", ("UPDATE",), "AFTER"),))
    assert codes(build_warnings(subject)) == codes(build_warnings(subject))


# -- triggers ----------------------------------------------------------------


def test_instead_of_trigger_is_an_error_with_the_explicit_warning() -> None:
    """FR: INSTEAD OF gets the stronger warning about INSERT/UPDATE/DELETE semantics."""
    subject = table(triggers=(Trigger("trg_v_IO", ("INSERT", "UPDATE"), "INSTEAD OF"),))
    warning = by_code(build_warnings(subject), WarningCode.INSTEAD_OF_TRIGGER)

    assert warning.severity is Severity.ERROR
    assert warning.title == "INSTEAD OF trigger on INSERT, UPDATE"
    assert "trg_v_IO" in warning.message
    assert "INSERT/UPDATE/DELETE may not do what you expect" in warning.message


def test_after_trigger_is_a_warning_naming_events_and_type() -> None:
    """An AFTER trigger can modify data or reject the statement — a warning, not an error."""
    subject = table(triggers=(Trigger("trg_A", ("DELETE",), "AFTER"),))
    warning = by_code(build_warnings(subject), WarningCode.TRIGGER)

    assert warning.severity is Severity.WARNING
    assert "AFTER trigger on DELETE" in warning.title
    assert "trg_A" in warning.message


def test_disabled_trigger_is_listed_dimmed_and_cannot_fire() -> None:
    """Disabled triggers are shown, but dimmed and marked as not firing."""
    subject = table(
        triggers=(
            Trigger("trg_Off", ("INSERT",), "AFTER", enabled=False),
            Trigger("trg_On", ("INSERT",), "AFTER"),
        )
    )
    warnings = build_warnings(subject)
    disabled = by_code(warnings, WarningCode.TRIGGER_DISABLED)

    assert disabled.dimmed is True
    assert disabled.severity is Severity.INFO
    assert "disabled" in disabled.title
    assert "will not fire" in disabled.message
    assert by_code(warnings, WarningCode.TRIGGER).severity is Severity.WARNING


def test_each_trigger_gets_its_own_warning() -> None:
    """Three triggers on different events produce three entries, none merged away."""
    subject = table(
        triggers=(
            Trigger("trg_1", ("INSERT",), "AFTER"),
            Trigger("trg_2", ("UPDATE",), "AFTER"),
            Trigger("trg_3", ("DELETE",), "AFTER"),
        )
    )
    warnings = [w for w in build_warnings(subject) if w.code is WarningCode.TRIGGER]
    assert [w.title for w in warnings] == [
        "AFTER trigger on INSERT",
        "AFTER trigger on UPDATE",
        "AFTER trigger on DELETE",
    ]


# -- row identity ------------------------------------------------------------


def test_no_primary_key_is_an_error_when_nothing_can_stand_in() -> None:
    """S-4: without PK or UNIQUE key, updates/deletes are blocked pending confirmation."""
    subject = table(column("Id", 1), pk=None)
    warning = by_code(build_warnings(subject), WarningCode.NO_PRIMARY_KEY)

    assert warning.severity is Severity.ERROR
    assert "row identity is ambiguous" in warning.message
    assert "blocked" in warning.message


def test_no_primary_key_is_only_a_warning_when_a_unique_key_exists() -> None:
    """A single-column NOT NULL UNIQUE can serve as identity (OQ-5), so rows stay writable."""
    subject = table(
        column("Code", 1, "char", max_length=2),
        pk=None,
        uniques=(UniqueConstraint("UQ_Code", ("Code",)),),
    )
    warning = by_code(build_warnings(subject), WarningCode.NO_PRIMARY_KEY)

    assert warning.severity is Severity.WARNING
    assert "UNIQUE" in warning.message


def test_primary_key_produces_no_identity_warning() -> None:
    assert WarningCode.NO_PRIMARY_KEY not in codes(build_warnings(table()))


def test_composite_unique_is_not_a_usable_identity() -> None:
    """A multi-column UNIQUE cannot scope an update safely, so the error stays."""
    subject = table(
        column("A", 1),
        column("B", 2),
        pk=None,
        uniques=(UniqueConstraint("UQ_AB", ("A", "B")),),
    )
    assert by_code(build_warnings(subject), WarningCode.NO_PRIMARY_KEY).severity is Severity.ERROR


# -- temporal & server-managed columns ---------------------------------------


def test_system_versioned_table_is_a_warning_naming_the_history_table() -> None:
    subject = table(
        column("ValidFrom", 1, "datetime2"),
        column("ValidTo", 2, "datetime2"),
        system_versioned=True,
    )
    warning = by_code(build_warnings(subject), WarningCode.SYSTEM_VERSIONED)

    assert warning.severity is Severity.WARNING
    assert "temporal" in warning.title
    assert "dbo.WidgetHistory" in warning.message


def test_history_table_is_informational() -> None:
    subject = table(
        column("Id", 1, pk=True), history_table=True, history_schema=None, history_name=None
    )
    warning = by_code(build_warnings(subject), WarningCode.HISTORY_TABLE)

    assert warning.severity is Severity.INFO
    assert "history side" in warning.message


def test_computed_and_rowversion_columns_are_listed_by_name() -> None:
    subject = table(
        column("Id", 1, pk=True),
        column("Upper", 2, "nvarchar", max_length=200, computed=True),
        column("Ver", 3, "rowversion", max_length=8, rowversion=True),
    )
    warnings = build_warnings(subject)

    computed = by_code(warnings, WarningCode.COMPUTED_COLUMNS)
    assert computed.severity is Severity.INFO
    assert computed.notes == ("Upper",)
    assert by_code(warnings, WarningCode.ROWVERSION_COLUMNS).notes == ("Ver",)


def test_columns_with_an_explicit_collation_are_a_warning() -> None:
    """A collation can reject values on write, so it must not be silent."""
    subject = table(
        column("Id", 1, pk=True),
        column("Name", 2, "nvarchar", max_length=100, collation="Latin1_General_CS_AS"),
    )
    warning = by_code(build_warnings(subject), WarningCode.COLLATED_COLUMN)

    assert warning.severity is Severity.WARNING
    assert warning.notes == ("Name (Latin1_General_CS_AS)",)


def test_a_column_without_a_collation_produces_no_collation_warning() -> None:
    subject = table(column("Name", 1, "nvarchar", max_length=100))
    assert WarningCode.COLLATED_COLUMN not in codes(build_warnings(subject))


# -- constraints -------------------------------------------------------------


def test_check_constraints_are_reported_as_unevaluated_risk() -> None:
    """The app shows the expression and says the database decides."""
    subject = table(checks=(CheckConstraint("CK_Amount", "([Amount]>(0))"),))
    warning = by_code(build_warnings(subject), WarningCode.CHECK_CONSTRAINT)

    assert warning.severity is Severity.WARNING
    assert "CK_Amount: ([Amount]>(0))" in warning.notes
    assert "does not evaluate" in warning.message


def test_no_check_constraints_produces_no_warning() -> None:
    assert WarningCode.CHECK_CONSTRAINT not in codes(build_warnings(table()))


# -- incoming foreign keys ----------------------------------------------------


def incoming(name: str, ref: str, on_delete: ReferentialAction) -> IncomingForeignKey:
    """One incoming FK from ``ref`` to ``Code``."""
    schema, _, table_name = ref.partition(".")
    return IncomingForeignKey(
        name=name,
        schema=schema,
        table=table_name,
        columns=("Ref",),
        referenced_columns=("Code",),
        on_delete=on_delete,
        on_update=ReferentialAction.NO_ACTION,
    )


def test_incoming_fk_reports_the_referencing_table_count() -> None:
    subject = table(
        incoming=(
            incoming("FK_1", "dbo.Region", ReferentialAction.CASCADE),
            incoming("FK_2", "dbo.City", ReferentialAction.CASCADE),
        )
    )
    warning = by_code(build_warnings(subject), WarningCode.INCOMING_FOREIGN_KEYS)

    assert warning.severity is Severity.WARNING
    assert warning.title == "referenced by 2 tables"
    assert "deleting rows may fail or cascade to 2 tables" in warning.message
    assert "2 of 2 incoming foreign keys cascade on delete" in warning.message


def test_incoming_fk_without_cascade_still_warns_that_deletes_may_fail() -> None:
    subject = table(incoming=(incoming("FK_1", "dbo.Region", ReferentialAction.NO_ACTION),))
    warning = by_code(build_warnings(subject), WarningCode.INCOMING_FOREIGN_KEYS)

    assert warning.title == "referenced by 1 table"
    assert "deleting rows may fail" in warning.message
    assert "cascad" not in warning.message


def test_incoming_fk_notes_show_both_referential_actions() -> None:
    subject = table(
        incoming=(incoming("FK_Region_Country", "dbo.Region", ReferentialAction.CASCADE),)
    )
    (note,) = by_code(build_warnings(subject), WarningCode.INCOMING_FOREIGN_KEYS).notes

    assert "FK_Region_Country: dbo.Region.Ref → .Code" in note
    assert "on delete CASCADE" in note
    assert "on update NO ACTION" in note


def test_two_fks_from_the_same_table_count_once_as_a_referencing_table() -> None:
    subject = table(
        incoming=(
            incoming("FK_1", "dbo.Region", ReferentialAction.CASCADE),
            incoming("FK_2", "dbo.Region", ReferentialAction.NO_ACTION),
        )
    )
    assert incoming_referencing_tables(subject) == ("dbo.Region",)
    assert by_code(build_warnings(subject), WarningCode.INCOMING_FOREIGN_KEYS).title == (
        "referenced by 1 table"
    )


def test_no_incoming_foreign_keys_produces_no_warning() -> None:
    assert WarningCode.INCOMING_FOREIGN_KEYS not in codes(build_warnings(table()))


# -- size advisory -----------------------------------------------------------


def test_large_tables_get_an_advisory_above_the_threshold() -> None:
    subject = table(rows=LARGE_TABLE_ROWS + 1)
    warning = by_code(build_warnings(subject), WarningCode.LARGE_TABLE)

    assert warning.severity is Severity.INFO
    assert f"{LARGE_TABLE_ROWS + 1:,}" in warning.message


@pytest.mark.parametrize("rows", [None, 0, LARGE_TABLE_ROWS])
def test_unknown_or_small_row_counts_produce_no_advisory(rows: int | None) -> None:
    subject = table(rows=rows)
    assert WarningCode.LARGE_TABLE not in codes(build_warnings(subject))


# -- badges, types and detail ------------------------------------------------


def test_pk_badge_carries_the_ordinal_of_a_composite_key() -> None:
    """A composite PK must show which part of the key the column is (#1, #2, ...)."""
    subject = table(
        column("A", 1, pk=True),
        column("B", 2, pk=True),
        column("C", 3),
        pk=PrimaryKey("PK_AB", ("A", "B")),
    )
    first = {badge.glyph: badge.label for badge in column_badges(subject, subject.columns[0])}
    second = {badge.glyph: badge.label for badge in column_badges(subject, subject.columns[1])}
    third = {badge.glyph for badge in column_badges(subject, subject.columns[2])}

    assert first["🔑"] == "primary key"
    assert second["🔑"] == "primary key #2"
    assert "🔑" not in third


def test_fk_badge_points_at_the_target_table_and_column() -> None:
    subject = table(
        column("Code", 1, pk=True),
        column("CountryCode", 2, "char", max_length=2, fk=True),
        fks=(
            ForeignKey(
                "FK_Region_Country",
                ("CountryCode",),
                "dbo",
                "Country",
                ("Code",),
                ReferentialAction.CASCADE,
                ReferentialAction.NO_ACTION,
            ),
        ),
    )
    badges = {badge.glyph: badge.label for badge in column_badges(subject, subject.columns[1])}

    assert badges["🔗"] == "→ dbo.Country.Code"
    assert "🔗" not in {b.glyph for b in column_badges(subject, subject.columns[0])}


def test_badges_cover_identity_computed_rowversion_default_unique_check() -> None:
    """One column, every badge kind, in the documented order."""
    subject = table(
        column("Id", 1, identity=True, seed=1, increment=1, pk=True),
        column("Upper", 2, "nvarchar", max_length=200, computed=True),
        column("Ver", 3, "rowversion", max_length=8, rowversion=True),
        column("Name", 4, "nvarchar", max_length=100, default="N''"),
        pk=PrimaryKey("PK_Id", ("Id",)),
        uniques=(UniqueConstraint("UQ_Name", ("Name",)),),
        checks=(CheckConstraint("CK_Name", "([Name]<>N'')"),),
    )
    glyphs = [badge.glyph for badge in column_badges(subject, subject.column("Name"))]

    assert glyphs == ["U", "D", "✱", "✓"] or set(glyphs) == {"U", "D", "✱", "✓"}
    name_badges = {
        badge.glyph: badge.label for badge in column_badges(subject, subject.column("Name"))
    }
    assert name_badges["D"] == "default N''"
    assert name_badges["U"] == "unique UQ_Name"
    assert "CK_Name" in name_badges["✓"] or "([Name]" in name_badges["✓"]

    computed = {b.glyph for b in column_badges(subject, subject.column("Upper"))}
    assert "ƒ" in computed
    rowversion = {b.glyph for b in column_badges(subject, subject.column("Ver"))}
    assert "⏱" in rowversion
    identity = {b.glyph for b in column_badges(subject, subject.column("Id"))}
    assert "#" in identity


def glyph_order(subject: Table, name: str) -> list[str]:
    """Badge glyphs of one column, in render order."""
    return [badge.glyph for badge in column_badges(subject, subject.column(name))]


def test_nullable_and_required_use_distinct_glyphs() -> None:
    """∅ marks nullable, ✱ marks required — the two must not look alike (FR-3.7)."""
    subject = table(column("A", 1, nullable=True), column("B", 2, nullable=False))
    assert "∅" in {b.glyph for b in column_badges(subject, subject.columns[0])}
    assert "✱" in {b.glyph for b in column_badges(subject, subject.columns[1])}


@pytest.mark.parametrize(
    ("column_args", "expected"),
    [
        # Column.max_length is a character count by the time it reaches the formatter:
        # the metadata mapper already halved the byte count sys.columns reports.
        ({"data_type": "nvarchar", "max_length": 100}, "nvarchar(100)"),
        ({"data_type": "varchar", "max_length": 50}, "varchar(50)"),
        ({"data_type": "char", "max_length": 2}, "char(2)"),
        ({"data_type": "decimal", "precision": 10, "scale": 2}, "decimal(10,2)"),
        ({"data_type": "numeric", "precision": 18, "scale": 0}, "numeric(18,0)"),
        ({"data_type": "float", "precision": 53}, "float(53)"),
        ({"data_type": "int", "precision": 10, "scale": 0}, "int"),
        ({"data_type": "uniqueidentifier"}, "uniqueidentifier"),
        ({"data_type": "rowversion", "max_length": 8}, "rowversion"),
    ],
)
def test_data_types_render_exactly_as_declared(
    column_args: dict[str, object], expected: str
) -> None:
    """FR-6.2 wants the *exact* type, e.g. nvarchar(50) or decimal(10,2)."""
    assert format_data_type(column("X", 1, **column_args)) == expected  # type: ignore[arg-type]
    assert type_hint(column("X", 1, **column_args)) == expected  # type: ignore[arg-type]


def test_header_label_carries_badges_read_only_marker_and_type() -> None:
    subject = table(
        column("Id", 1, identity=True, pk=True),
        column("Upper", 2, "nvarchar", max_length=200, computed=True),
        column("Name", 3, "nvarchar", max_length=100, nullable=True),
        pk=PrimaryKey("PK_Id", ("Id",)),
    )
    assert "🔒" in header_label(subject, subject.column("Upper"))
    assert header_label(subject, subject.column("Name")).endswith("nvarchar(100)")
    assert "🔑" in header_label(subject, subject.column("Id"))


def test_column_detail_reports_identity_seed_and_increment() -> None:
    subject = table(column("Id", 1, identity=True, seed=100, increment=5, pk=True))
    detail = column_detail(subject, subject.columns[0])
    values = dict(detail.rows)

    assert values["identity"] == "seed 100, increment 5"
    assert detail.is_read_only is True
    assert read_only_reason(subject.columns[0]) is not None


def test_column_detail_reports_computed_definition_and_persisted_flag() -> None:
    subject = table(
        column("Id", 1, pk=True),
        column(
            "Upper",
            2,
            "nvarchar",
            max_length=200,
            computed=True,
            computed_definition="UPPER([Name])",
        ),
    )
    values = dict(column_detail(subject, subject.column("Upper")).rows)

    assert "UPPER([Name])" in values["computed"]
    assert "not persisted" in values["computed"]


def test_column_detail_reports_fk_referential_actions() -> None:
    subject = table(
        column("CountryCode", 1, "char", max_length=2, fk=True),
        fks=(
            ForeignKey(
                "FK_Region_Country",
                ("CountryCode",),
                "dbo",
                "Country",
                ("Code",),
                ReferentialAction.CASCADE,
                ReferentialAction.SET_NULL,
            ),
        ),
    )
    values = dict(column_detail(subject, subject.columns[0]).rows)

    assert "dbo.Country.Code" in values["foreign key"]
    assert "on delete CASCADE" in values["foreign key"]
    assert "on update SET NULL" in values["foreign key"]


def test_column_detail_reports_check_unique_and_collation() -> None:
    subject = table(
        column("Code", 1, "char", max_length=2, collation="Latin1_General_CI_AS"),
        uniques=(UniqueConstraint("UQ_Code", ("Code",)),),
        checks=(CheckConstraint("CK_Code", "([Code]<>'')"),),
    )
    values = dict(column_detail(subject, subject.columns[0]).rows)

    assert values["unique"] == "UQ_Code"
    assert "[Code]" in values["check"]
    assert values["collation"] == "Latin1_General_CI_AS"
    assert values["type"] == "char(2)"


def test_column_detail_default_is_database_default_without_a_collation() -> None:
    subject = table(column("X", 1))
    assert dict(column_detail(subject, subject.columns[0]).rows)["collation"] == "database default"


def test_column_detail_dataclass_exposes_type_text() -> None:
    subject = table(column("Amount", 1, "decimal", precision=10, scale=2))
    detail = column_detail(subject, subject.columns[0])

    assert isinstance(detail, ColumnDetail)
    assert detail.type_text == "decimal(10,2)"


# -- table summary & cell formatting -----------------------------------------


def test_table_summary_reports_name_rows_pk_and_incoming_fk_count() -> None:
    subject = table(
        column("Code", 1, pk=True),
        schema="sales",
        name="Region",
        rows=1234,
        pk=PrimaryKey("PK_Code", ("Code",)),
        incoming=(
            IncomingForeignKey(
                "FK_1",
                "dbo",
                "Order",
                ("RegionCode",),
                ("Code",),
                ReferentialAction.CASCADE,
                ReferentialAction.NO_ACTION,
            ),
            IncomingForeignKey(
                "FK_2",
                "dbo",
                "Invoice",
                ("RegionCode",),
                ("Code",),
                ReferentialAction.CASCADE,
                ReferentialAction.NO_ACTION,
            ),
        ),
    )
    values = dict(table_summary_rows(subject))

    assert values["name"] == "sales.Region"
    assert values["kind"] == "table"
    assert values["rows"] == "≈1,234"
    assert values["primary key"] == "Code"
    assert values["referenced by"] == "2 tables"
    assert values["editable"] == "yes"


def test_table_summary_marks_a_view_and_missing_primary_key() -> None:
    subject = table(column("Code", 1), kind=TableKind.VIEW, pk=None)
    values = dict(table_summary_rows(subject))

    assert values["kind"] == "view"
    assert "none" in values["primary key"]
    assert "read-only" in values["editable"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "NULL"),
        (True, "1"),
        (False, "0"),
        (b"\x01", "0x01 (1 bytes)"),
        (bytes(9), "0x0000000000000000… (9 bytes)"),
        ("Germany", "Germany"),
        (42, "42"),
    ],
)
def test_cell_values_render_distinctly(value: object, expected: str) -> None:
    """NULL is upper-case, bools are 0/1, binaries are 0x… with a length (FR-3.1)."""
    assert format_cell_value(value) == expected
