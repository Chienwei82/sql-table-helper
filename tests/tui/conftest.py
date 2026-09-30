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
    Column,
    ConnectionProfile,
    PrimaryKey,
    Table,
    TableKind,
    Trigger,
)
from sql_table_swiss_knife.storage import (
    EphemeralSecretStore,
    ProfileStore,
    SettingsStore,
)
from sql_table_swiss_knife.tui.app import SwissKnifeApp
from tests.fakes import FakeProvider

# -- catalog used by the picker tests ----------------------------------

COUNTRY = Table(
    schema="dbo",
    name="Country",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Code", 1, "char", 2, None, None, False, None, False, is_primary_key=True),
        Column("Name", 2, "nvarchar", 100, None, None, False, None, False),
    ),
    primary_key=PrimaryKey("PK_Country", ("Code",)),
    approximate_row_count=3,
)

#: No primary key -> ⚠ badge and read-only rows (S-4).
AUDIT = Table(
    schema="dbo",
    name="AuditLog",
    kind=TableKind.BASE_TABLE,
    columns=(Column("Id", 1, "int", None, 10, 0, False, None, True, is_primary_key=True),),
    triggers=(Trigger("trg_Audit", ("INSERT",), "AFTER"),),
)

#: Foreign keys and a trigger -> 🔗 and ⚡ badges.
ORDER = Table(
    schema="sales",
    name="Order",
    kind=TableKind.BASE_TABLE,
    columns=(
        Column("Id", 1, "int", None, 10, 0, False, None, True, is_primary_key=True),
        Column("CountryCode", 2, "char", 2, None, None, False, None, False),
    ),
    primary_key=PrimaryKey("PK_Order", ("Id",)),
    triggers=(Trigger("trg_Order", ("UPDATE",), "AFTER"),),
)

CUSTOMER_VIEW = Table(
    schema="reporting",
    name="v_Customer",
    kind=TableKind.VIEW,
    columns=(Column("Code", 1, "char", 2, None, None, False, None, False),),
)

SAMPLE_TABLES: tuple[Table, ...] = (COUNTRY, AUDIT, ORDER, CUSTOMER_VIEW)


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


@pytest.fixture
def provider() -> FakeProvider:
    """A fake provider serving the sample catalog."""
    return FakeProvider(SAMPLE_TABLES)


def active_screen[S](app: App[None], screen_type: type[S]) -> S:
    """The active screen, typed as its concrete class.

    Textual types ``App.screen`` as ``Screen[object]``; the tests know exactly which
    screen they are driving, so this cast documents the expectation instead of
    scattering ``# type: ignore`` comments.
    """
    return cast("S", app.screen)


#: Builds a fully injected :class:`SwissKnifeApp` (extra kwargs override the defaults).
AppFactory = Callable[..., SwissKnifeApp]


@pytest.fixture
def app_factory(tmp_path: Path, provider: FakeProvider) -> AppFactory:
    """Build a fully injected app; returns a callable taking extra constructor kwargs."""
    profiles = ProfileStore(tmp_path / "profiles.toml")

    def build(**kwargs: object) -> SwissKnifeApp:
        options: dict[str, object] = {
            "profiles": profiles,
            "secrets": EphemeralSecretStore(),
            "settings": SettingsStore(tmp_path / "settings.toml"),
            "provider_factory": lambda name: provider,
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
    "SAMPLE_TABLES",
    "AppFactory",
    "ProfileStore",
    "active_screen",
    "demo_profile",
    "no_database_profile",
]
