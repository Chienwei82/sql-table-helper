"""Fixtures for the Textual Pilot tests (M3).

Every test app is fully injected — a temp profile store, an in-memory secret store, a
temp settings store and a ``FakeProvider`` — so nothing touches the developer's real
configuration, the OS keyring or a database (DESIGN §11).
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest
from textual.app import App

from sql_table_swiss_knife.domain import (
    AuthMode,
    CheckConstraint,
    Column,
    ConnectionProfile,
    ForeignKey,
    IncomingForeignKey,
    PrimaryKey,
    ReferentialAction,
    Table,
    TableKind,
    Trigger,
    UniqueConstraint,
)
from sql_table_swiss_knife.storage import (
    EphemeralSecretStore,
    ProfileStore,
    SettingsStore,
)
from sql_table_swiss_knife.tui.app import SwissKnifeApp
from tests.fakes import FakeProvider

# -- catalog used by the picker tests ----------------------------------

#: A rich table: PK, UNIQUE, CHECK, default, identity FK, computed, rowversion, an AFTER
#: trigger, a collation and an incoming FK — one of everything the inspector renders.
COUNTRY = Table(
    schema="dbo",
    name="Country",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Code", 1, "char", 2, None, None, False, None, False, is_primary_key=True),
        Column("Name", 2, "nvarchar", 200, None, None, False, None, False),
        Column(
            "Population",
            3,
            "int",
            None,
            10,
            0,
            False,
            "0",
            False,
            is_primary_key=False,
        ),
        Column(
            "NameUpper",
            4,
            "nvarchar",
            200,
            None,
            None,
            True,
            None,
            False,
            is_computed=True,
            computed_definition="UPPER([Name])",
        ),
        Column("RowVer", 5, "rowversion", 8, None, None, False, None, False, is_rowversion=True),
    ),
    primary_key=PrimaryKey("PK_Country", ("Code",)),
    unique_constraints=(UniqueConstraint("UQ_Country_Name", ("Name",)),),
    check_constraints=(CheckConstraint("CK_Country_Population", "([Population]>=(0))"),),
    triggers=(
        Trigger("trg_Country_Audit", ("UPDATE",), "AFTER"),
        Trigger("trg_Country_Off", ("INSERT",), "AFTER", enabled=False),
    ),
    approximate_row_count=5,
    incoming_foreign_keys=(
        IncomingForeignKey(
            "FK_Region_Country",
            "dbo",
            "Region",
            ("CountryCode",),
            ("Code",),
            ReferentialAction.CASCADE,
            ReferentialAction.NO_ACTION,
        ),
    ),
)

#: No primary key -> ⚠ badge and read-only rows (S-4).
AUDIT = Table(
    schema="dbo",
    name="AuditLog",
    kind=TableKind.BASE_TABLE,
    columns=(Column("Id", 1, "int", None, 10, 0, False, None, True, is_primary_key=True),),
    triggers=(Trigger("trg_Audit", ("INSERT",), "AFTER"),),
)

#: An identity PK, a foreign key to Country and a trigger -> 🔗 and ⚡ badges. The FK is
#: what the lookup picker opens on, so the editing tests have a reference to look up.
ORDER = Table(
    schema="sales",
    name="Order",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Id", 1, "int", None, 10, 0, False, None, True, is_primary_key=True),
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
    ),
    primary_key=PrimaryKey("PK_Order", ("Id",)),
    foreign_keys=(
        ForeignKey(
            "FK_Order_Country",
            ("CountryCode",),
            "dbo",
            "Country",
            ("Code",),
            ReferentialAction.NO_ACTION,
            ReferentialAction.NO_ACTION,
        ),
    ),
    triggers=(Trigger("trg_Order", ("UPDATE",), "AFTER"),),
)

CUSTOMER_VIEW = Table(
    schema="reporting",
    name="v_Customer",
    kind=TableKind.VIEW,
    columns=(Column("Code", 1, "char", 2, None, None, False, None, False),),
    triggers=(Trigger("trg_v_Customer_IO", ("INSERT", "UPDATE"), "INSTEAD OF"),),
)

#: A deliberately *wide* table with a long-text column: 12 columns (so the grid must
#: scroll horizontally and the PK freeze matters) and one nvarchar(max) description.
#: M8's wide/long-cell handling is only provable against a table shaped like this.
WIDE_NOTES = Table(
    schema="docs",
    name="Notes",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Id", 1, "int", None, 10, 0, False, None, True, is_primary_key=True),
        Column("Body", 2, "nvarchar", None, None, None, True, None, False),
        *(
            Column(f"Attr{index}", index + 3, "int", None, 10, 0, True, None, False)
            for index in range(10)
        ),
    ),
    primary_key=PrimaryKey("PK_Notes", ("Id",)),
)

#: A keyless table, to prove the freeze does not grab an arbitrary data column.
SAMPLE_TABLES: tuple[Table, ...] = (COUNTRY, AUDIT, ORDER, CUSTOMER_VIEW, WIDE_NOTES)


def demo_profile(name: str = "catalog", *, database: str | None = "test") -> ConnectionProfile:
    """The profile used by most tests: SQL auth against the fake provider."""
    return ConnectionProfile(
        name=name,
        provider="fake",
        host="localhost",
        port=1433,
        database=database,
        username="sa",
        auth=AuthMode.SQL,
    )


def no_database_profile(name: str = "no-db") -> ConnectionProfile:
    """A profile without a default database, so connecting opens the picker (FR-1.4)."""
    return ConnectionProfile(
        name=name,
        provider="fake",
        host="localhost",
        port=1433,
        database=None,
        username="sa",
        auth=AuthMode.SQL,
    )


#: Rows the fake provider serves, keyed by bare table name (FakeProvider's convention).
SAMPLE_ROWS: dict[str, list[dict[str, object]]] = {
    "Country": [
        {"Code": "DE", "Name": "Germany", "Population": 83_000_000, "RowVer": b"\x01"},
        {"Code": "FR", "Name": "France", "Population": 67_000_000, "RowVer": b"\x02"},
        {"Code": "JP", "Name": "Japan", "Population": 125_000_000, "RowVer": b"\x03"},
        {"Code": "US", "Name": "United States", "Population": 331_000_000, "RowVer": b"\x04"},
        {"Code": "CH", "Name": "Switzerland", "Population": 8_700_000, "RowVer": b"\x05"},
    ],
    "AuditLog": [
        {"Id": 1},
        {"Id": 2},
    ],
    "Order": [
        {"Id": 1, "CountryCode": "DE"},
        {"Id": 2, "CountryCode": "FR"},
    ],
    "v_Customer": [{"Code": "DE"}, {"Code": "FR"}],
    "Notes": [
        {
            "Id": 1,
            "Body": (
                "A deliberately long description, of the kind catalog tables really "
                "carry. The grid cannot show it in a column, and the user must still "
                "be able to read every word of it without widening the terminal past "
                "usability. End of the description."
            ),
            **{f"Attr{index}": index for index in range(10)},
        },
        {"Id": 2, "Body": "short", **{f"Attr{index}": None for index in range(10)}},
    ],
}


@pytest.fixture
def provider() -> FakeProvider:
    """A fake provider serving the sample catalog and its rows."""
    return FakeProvider(SAMPLE_TABLES, SAMPLE_ROWS)


def make_provider(*, conflict_on: int | None = None, fail_on: int | None = None) -> FakeProvider:
    """A fake provider scripted to fail or to report a concurrency conflict.

    ``app_factory`` takes a ``provider`` override so a test can exercise the failure
    paths (rollback, FR-7.8) without a real database.
    """
    return FakeProvider(SAMPLE_TABLES, SAMPLE_ROWS, fail_on=fail_on, conflict_on=conflict_on)


def active_screen[S](app: App[None], screen_type: type[S]) -> S:
    """The active screen, typed as its concrete class.

    Textual types ``App.screen`` as ``Screen[object]``; the tests know exactly which
    screen they are driving, so this cast documents the expectation instead of
    scattering ``# type: ignore`` comments.
    """
    return cast("S", app.screen)


#: Builds a fully injected :class:`SwissKnifeApp` (extra kwargs override the defaults).
#: ``provider=`` swaps the fake provider, which is how a test drives a rollback or a
#: concurrency conflict without a database.
AppFactory = Callable[..., SwissKnifeApp]


@pytest.fixture
def snap_compare(snap_compare: Callable[..., bool]) -> Callable[..., bool]:
    """Wrap pytest-textual-snapshot's ``snap_compare`` so a mismatch fails the test.

    The upstream fixture *returns* whether the screenshot matched; it does not raise.
    Every call site here discards that return value, so a mismatched snapshot left the
    run green ("1 passed") while only a line in the terminal summary and an HTML report
    recorded the failure — a green suite that was not evidence of anything. This
    override asserts the result, so a visual regression fails like any other test.

    The snapshot diff itself is still written to ``snapshot_report.html``.
    """
    upstream = snap_compare

    def compare(*args: object, **kwargs: object) -> bool:
        matched = upstream(*args, **kwargs)
        assert matched, (
            "snapshot mismatch: the screen no longer matches its stored SVG. Review the "
            "diff in snapshot_report.html; if the change is intended, re-run with "
            "--snapshot-update."
        )
        return matched

    return compare


@pytest.fixture
def app_factory(tmp_path: Path, provider: FakeProvider) -> AppFactory:
    """Build a fully injected app; returns a callable taking extra constructor kwargs.

    A test can pass ``provider=make_provider(conflict_on=0)`` to drive the failure paths
    (Apply rollback, a concurrency conflict) without a real database.
    """
    profiles = ProfileStore(tmp_path / "profiles.toml")
    fixture_provider = provider

    def build(*, provider: FakeProvider | None = None, **kwargs: object) -> SwissKnifeApp:
        active = provider if provider is not None else fixture_provider
        options: dict[str, object] = {
            "profiles": profiles,
            "secrets": EphemeralSecretStore(),
            "settings": SettingsStore(tmp_path / "settings.toml"),
            "provider_factory": lambda name: active,
        }
        options.update(kwargs)
        return SwissKnifeApp(**options)  # type: ignore[arg-type]

    return build


@pytest.fixture
def seeded_profiles(tmp_path: Path) -> Iterator[ProfileStore]:
    """A profile store holding one connected-to profile."""
    store = ProfileStore(tmp_path / "profiles.toml")
    store.save_all((demo_profile(),))
    yield store


__all__ = [
    "SAMPLE_ROWS",
    "SAMPLE_TABLES",
    "WIDE_NOTES",
    "AppFactory",
    "ProfileStore",
    "active_screen",
    "demo_profile",
    "make_provider",
    "no_database_profile",
]
