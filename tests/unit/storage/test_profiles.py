"""Profile store tests: round-trip, validation, and the hard no-secrets rule."""

from pathlib import Path

import pytest

from sql_table_swiss_knife.domain import AuthMode, ConnectionOptions, ConnectionProfile
from sql_table_swiss_knife.storage import ProfileError, ProfileStore


def _sql_profile() -> ConnectionProfile:
    return ConnectionProfile(
        name="local",
        provider="mssql",
        host="localhost",
        port=1433,
        database="SwissKnifeSample",
        auth=AuthMode.SQL,
        username="sa",
        secret_ref="local@localhost",
        options=ConnectionOptions(encrypt=True, trust_server_certificate=True, connect_timeout_s=3),
    )


def test_missing_file_is_empty(tmp_path: Path) -> None:
    assert ProfileStore(tmp_path / "profiles.toml").load() == ()


def test_round_trip(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles.toml")
    store.upsert(_sql_profile())
    loaded = store.load()
    assert loaded == (_sql_profile(),)
    assert store.get("local") == _sql_profile()


def test_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    ProfileStore(path).upsert(_sql_profile())
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_no_password_key_is_ever_written(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    ProfileStore(path).upsert(_sql_profile())
    text = path.read_text(encoding="utf-8").lower()
    assert "password" not in text
    assert "pwd" not in text
    # the username is stored; the secret is only referenced
    assert 'username = "sa"' in text
    assert 'secret_ref = "local@localhost"' in text


def test_password_key_in_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text(
        '[[profile]]\nname = "x"\nprovider = "mssql"\nhost = "h"\n'
        'username = "sa"\npassword = "hunter2"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileError, match="never"):
        ProfileStore(path).load()


def test_unknown_keys_rejected(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text(
        '[[profile]]\nname = "x"\nprovider = "mssql"\nhost = "h"\nusername = "u"\nnope = 1\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileError, match="unknown key"):
        ProfileStore(path).load()


def test_bad_auth_value_rejected(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text(
        '[[profile]]\nname = "x"\nprovider = "mssql"\nhost = "h"\nauth = "kerberos"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileError, match="auth"):
        ProfileStore(path).load()


def test_duplicate_names_rejected(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles.toml")
    with pytest.raises(ProfileError, match="duplicate"):
        store.save_all((_sql_profile(), _sql_profile()))


def test_upsert_replaces_by_name(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles.toml")
    store.upsert(_sql_profile())
    store.upsert(ConnectionProfile(name="local", provider="mssql", host="elsewhere", username="sa"))
    assert [profile.host for profile in store.load()] == ["elsewhere"]


def test_delete_and_get_errors(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles.toml")
    store.upsert(_sql_profile())
    store.delete("local")
    assert store.load() == ()
    with pytest.raises(ProfileError, match="unknown profile"):
        store.get("local")
    with pytest.raises(ProfileError, match="unknown profile"):
        store.delete("local")
