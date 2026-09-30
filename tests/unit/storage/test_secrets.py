"""Secret store tests: keyring wrapper, ephemeral fallback, password resolution."""

import pytest

from sql_table_swiss_knife.domain import AuthMode, ConnectionProfile
from sql_table_swiss_knife.storage import (
    EphemeralSecretStore,
    SecretStoreError,
    default_secret_store,
    resolve_password,
    secret_ref_for,
)
from sql_table_swiss_knife.storage.secrets import KeyringSecretStore

SECRET = "sup3r-s3cret"


def _profile() -> ConnectionProfile:
    return ConnectionProfile(
        name="local",
        provider="mssql",
        host="localhost",
        auth=AuthMode.SQL,
        username="sa",
        secret_ref="local@localhost",
    )


def test_secret_ref_derivation() -> None:
    assert secret_ref_for(_profile()) == "local@localhost"


def test_ephemeral_store_round_trip() -> None:
    store = EphemeralSecretStore()
    assert store.persistent is False
    assert store.get("ref") is None
    store.set("ref", SECRET)
    assert store.get("ref") == SECRET
    store.delete("ref")
    assert store.get("ref") is None
    store.delete("ref")  # idempotent


def test_empty_secret_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        EphemeralSecretStore().set("ref", "")


def test_ephemeral_repr_hides_values() -> None:
    store = EphemeralSecretStore()
    store.set("ref", SECRET)
    assert SECRET not in repr(store)


def test_resolve_prefers_store_then_prompts() -> None:
    store = EphemeralSecretStore()
    store.set("local@localhost", SECRET)
    assert resolve_password(store, _profile()) == SECRET

    empty = EphemeralSecretStore()
    assert resolve_password(empty, _profile(), prompt=lambda _: "typed") == "typed"
    assert resolve_password(empty, _profile(), prompt=lambda _: "") is None
    assert resolve_password(empty, _profile()) is None


def test_resolve_uses_derived_ref_when_profile_has_none() -> None:
    profile = ConnectionProfile(
        name="derived", provider="mssql", host="db1", auth=AuthMode.SQL, username="sa"
    )
    store = EphemeralSecretStore()
    store.set(secret_ref_for(profile), SECRET)
    assert resolve_password(store, profile) == SECRET


def test_keyring_store_wraps_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    saved: dict[tuple[str, str], str] = {}

    monkeypatch.setattr(keyring, "get_password", lambda service, ref: saved.get((service, ref)))
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, ref, secret: saved.__setitem__((service, ref), secret),
    )
    monkeypatch.setattr(keyring, "delete_password", lambda service, ref: saved.pop((service, ref)))

    store = KeyringSecretStore()
    assert store.persistent is True
    store.set("ref", SECRET)
    assert store.get("ref") == SECRET
    store.delete("ref")
    assert store.get("ref") is None


def test_keyring_failure_becomes_secret_store_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    def boom(service: str, ref: str) -> str:
        raise RuntimeError("no wallet")

    monkeypatch.setattr(keyring, "get_password", boom)
    with pytest.raises(SecretStoreError):
        KeyringSecretStore().get("ref")


def test_keyring_delete_of_missing_secret_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring

    def boom(service: str, ref: str) -> None:
        raise keyring.errors.PasswordDeleteError(ref)

    monkeypatch.setattr(keyring, "delete_password", boom)
    KeyringSecretStore().delete("ref")  # must not raise


def test_default_store_follows_keyring_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    from keyring.backends import fail

    def _fail_backend() -> object:
        # constructing the untyped fail backend is exactly what we want to simulate
        return fail.Keyring()  # type: ignore[no-untyped-call]

    monkeypatch.setattr(keyring, "get_keyring", _fail_backend)
    assert isinstance(default_secret_store(), EphemeralSecretStore)

    class _Backend:
        priority = 10

    monkeypatch.setattr(keyring, "get_keyring", lambda: _Backend())
    assert isinstance(default_secret_store(), KeyringSecretStore)
