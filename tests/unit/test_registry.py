"""Tests for the provider registry (plugin seam)."""

from importlib import metadata

import pytest

from sql_table_swiss_knife.providers import (
    DatabaseProvider,
    UnknownProviderError,
    available_providers,
    get_provider,
    load_entry_point_providers,
    register_provider,
)
from tests.fakes import FakeProvider


def test_register_and_get_is_case_insensitive() -> None:
    register_provider("Fake", FakeProvider)
    assert available_providers() == ("fake",)
    provider = get_provider("  FAKE ")
    assert provider.name == "fake"


def test_duplicate_registration_requires_replace() -> None:
    register_provider("fake", FakeProvider)
    with pytest.raises(ValueError, match="already registered"):
        register_provider("fake", FakeProvider)
    register_provider("fake", FakeProvider, replace=True)
    assert available_providers() == ("fake",)


def test_empty_name_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        register_provider("   ", FakeProvider)


def test_unknown_provider_reports_available() -> None:
    register_provider("fake", FakeProvider)
    with pytest.raises(UnknownProviderError, match="available: fake"):
        get_provider("nope")


def test_builtin_mssql_provider_is_available_lazily() -> None:
    """Since M2 the mssql provider ships with the app and resolves without a driver."""
    provider = get_provider("mssql")
    assert provider.name == "mssql"
    assert "mssql" in available_providers()
    assert provider.capabilities.supports_integrated_auth


def test_unknown_builtin_still_raises_when_registry_is_empty() -> None:
    with pytest.raises(UnknownProviderError, match=r"available: \(none\)"):
        get_provider("not-a-provider")


def test_entry_point_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Entry:
        def __init__(self, name: str) -> None:
            self.name = name

        def load(self) -> object:
            return FakeProvider

    monkeypatch.setattr(
        metadata,
        "entry_points",
        lambda *, group=None: (_Entry("ep-fake"),),
    )
    loaded = load_entry_point_providers()
    assert loaded == ("ep-fake",)
    provider = get_provider("ep-fake")
    assert isinstance(provider, DatabaseProvider)


def test_fake_provider_satisfies_protocol() -> None:
    provider: DatabaseProvider = FakeProvider()
    assert isinstance(provider, DatabaseProvider)
    assert provider.name == "fake"
