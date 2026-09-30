"""ConnectionService: profile CRUD, connect/test/disconnect, session state (M3).

Runs entirely against ``FakeProvider`` — no database, deterministic (NFR-5).
"""

from pathlib import Path

import pytest
from tests.fakes import FakeConnection, FakeProvider

from sql_table_swiss_knife.domain import AuthMode, ConnectionProfile
from sql_table_swiss_knife.providers import AuthError, ConnectError
from sql_table_swiss_knife.services import ConnectionService, ConnectionState, duplicate_profile
from sql_table_swiss_knife.storage import EphemeralSecretStore, ProfileError, ProfileStore


class FailingProvider(FakeProvider):
    """A provider whose connections always fail, to prove errors are surfaced."""

    async def connect(
        self, profile: ConnectionProfile, password: str | None = None
    ) -> FakeConnection:
        del profile, password
        raise ConnectError("login failed for user 'sa'")


class RecordingProvider(FakeProvider):
    """Records the passwords it is handed, to prove where secrets come from."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str | None] = []

    async def connect(
        self, profile: ConnectionProfile, password: str | None = None
    ) -> FakeConnection:
        self.seen.append(password)
        return await super().connect(profile, password)


class AuthFailingProvider(FakeProvider):
    """Fails the way a wrong password does (FR-1.2 / provider error mapping)."""

    async def connect(
        self, profile: ConnectionProfile, password: str | None = None
    ) -> FakeConnection:
        del profile, password
        raise AuthError("login failed for user 'sa'")


def _profile(name: str = "catalog", **kwargs: object) -> ConnectionProfile:
    return ConnectionProfile(
        name=name,
        provider="fake",
        host="localhost",
        database="test",
        username="sa",
        auth=AuthMode.SQL,
        **kwargs,  # type: ignore[arg-type]
    )


def _service(tmp_path: Path, provider: FakeProvider | None = None) -> ConnectionService:
    """A service wired to a temp profile store and an in-memory secret store."""
    store = ProfileStore(tmp_path / "profiles.toml")
    resolved: FakeProvider = provider if provider is not None else FakeProvider()
    return ConnectionService(
        profiles=store,
        secrets=EphemeralSecretStore(),
        provider_factory=lambda name: resolved,
    )


def test_profile_crud_round_trips_through_the_store(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.list_profiles() == ()

    service.save_profile(_profile("alpha"))
    service.save_profile(_profile("beta"))
    assert [p.name for p in service.list_profiles()] == ["alpha", "beta"]

    service.delete_profile("alpha")
    assert [p.name for p in service.list_profiles()] == ["beta"]
    with pytest.raises(ProfileError):
        service.get_profile("alpha")


def test_duplicate_profile_copies_settings_but_not_the_secret_ref() -> None:
    """A duplicate gets its own credential slot (FR-1.1)."""
    original = _profile("prod", secret_ref="prod@sql01")
    copy = duplicate_profile(original, "prod-staging")
    assert copy.name == "prod-staging"
    assert copy.secret_ref == "prod-staging@localhost"
    assert copy.host == original.host
    assert copy.database == original.database
    assert copy.options == original.options


async def test_connect_then_disconnect_tracks_state_and_session(tmp_path: Path) -> None:
    service = _service(tmp_path)
    profile = _profile()
    service.save_profile(profile)

    assert service.state is ConnectionState.DISCONNECTED
    assert service.session is None

    session = await service.connect(profile)
    # Read through a fresh reference: mypy narrows a literal-typed property otherwise.
    state: ConnectionState = service.state
    assert state is ConnectionState.CONNECTED
    assert service.is_connected
    assert session.profile_name == "catalog"
    assert session.database == "test"
    assert session.label == "localhost:1433/test"

    await service.disconnect()
    assert service.state is ConnectionState.DISCONNECTED
    assert service.session is None
    assert not service.is_connected
    await service.disconnect()  # idempotent: quitting twice must be safe


async def test_connecting_twice_replaces_the_first_connection(tmp_path: Path) -> None:
    """Only one handle ever exists: the previous one is closed first."""
    service = _service(tmp_path)
    await service.connect(_profile("first"))
    await service.connect(_profile("second"))
    assert service.session is not None
    assert service.session.profile_name == "second"


async def test_connect_failure_records_the_error_and_stays_disconnected(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, FailingProvider())
    with pytest.raises(ConnectError):
        await service.connect(_profile())
    # The ERROR state is what the UI shows a retry action for (DESIGN §9.3).
    assert service.state is ConnectionState.ERROR
    assert not service.is_connected
    assert service.last_error == "login failed for user 'sa'"


async def test_test_connection_reports_the_server_and_leaves_nothing_open(
    tmp_path: Path,
) -> None:
    """FR-1.3: the probe opens a throwaway connection and always closes it."""
    service = _service(tmp_path)
    result = await service.test_connection(_profile())
    assert result.database == "test"
    assert result.server_name == "localhost:1433"
    assert result.latency_ms >= 0
    assert not service.is_connected


async def test_test_connection_failure_is_reported_without_credentials(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, AuthFailingProvider())
    with pytest.raises(AuthError):
        await service.test_connection(_profile())
    assert service.state is ConnectionState.ERROR
    assert service.last_error == "login failed for user 'sa'"


async def test_password_comes_from_the_secret_store_and_stays_out_of_the_file(
    tmp_path: Path,
) -> None:
    """S-sec-1: the password is read from the store and never persisted (FR-1.6)."""
    provider = RecordingProvider()
    service = _service(tmp_path, provider)
    profile = _profile(secret_ref="catalog@localhost")
    service.save_profile(profile)
    service.store_password(profile, "s3cret")

    await service.connect(profile)
    assert provider.seen == ["s3cret"]
    assert "s3cret" not in (tmp_path / "profiles.toml").read_text(encoding="utf-8")


async def test_integrated_auth_never_looks_up_a_password(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service = _service(tmp_path, provider)
    profile = ConnectionProfile(
        name="win", provider="fake", host="localhost", auth=AuthMode.INTEGRATED
    )
    await service.connect(profile)
    assert provider.seen == [None]
    assert service.session is not None
    assert service.session.profile_name == "win"


async def test_cannot_use_the_connection_after_disconnect(tmp_path: Path) -> None:
    """Callers must check :attr:`is_connected`; the accessors say so loudly."""
    service = _service(tmp_path)
    with pytest.raises(RuntimeError, match="not connected"):
        service.connection()
    with pytest.raises(RuntimeError, match="not connected"):
        service.provider()
    await service.connect(_profile())
    assert service.connection() is not None
    await service.disconnect()
    with pytest.raises(RuntimeError, match="not connected"):
        service.connection()


async def test_deleting_the_active_profile_is_refused(tmp_path: Path) -> None:
    """Deleting the live session's profile would leave the header lying."""
    service = _service(tmp_path)
    profile = _profile()
    service.save_profile(profile)
    await service.connect(profile)
    with pytest.raises(ValueError, match="active session"):
        service.delete_profile("catalog")


def test_store_password_requires_a_real_secret(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="must not be empty"):
        service.store_password(_profile(), "")
