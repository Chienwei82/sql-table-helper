"""Tests for the ODBC connection-string builder, sanitizing and error mapping."""

import pytest

from sql_table_swiss_knife.domain import AuthMode, ConnectionOptions, ConnectionProfile
from sql_table_swiss_knife.providers import AuthError, ConnectError, MetadataError, QueryError
from sql_table_swiss_knife.providers.mssql.connection import (
    build_connection_string,
    fetch_all,
    sanitize_connection_string,
)
from sql_table_swiss_knife.providers.mssql.errors import map_pyodbc_error, sanitize_driver_message

SECRET = "sup3r-s3cret"


def _sql_profile(**overrides: object) -> ConnectionProfile:
    defaults: dict[str, object] = {
        "name": "local",
        "provider": "mssql",
        "host": "localhost",
        "port": 1433,
        "database": "SwissKnifeSample",
        "auth": AuthMode.SQL,
        "username": "sa",
        "options": ConnectionOptions(trust_server_certificate=True),
    }
    defaults.update(overrides)
    return ConnectionProfile(**defaults)  # type: ignore[arg-type]


def test_sql_auth_connection_string() -> None:
    text = build_connection_string(_sql_profile(), SECRET)
    assert "DRIVER=ODBC Driver 18 for SQL Server" in text
    assert "SERVER=localhost,1433" in text
    assert "DATABASE=SwissKnifeSample" in text
    assert "UID=sa" in text
    assert "PWD=sup3r-s3cret" in text
    assert "TrustServerCertificate=yes" in text
    assert "Connection Timeout=5" in text


def test_trust_server_certificate_option_is_honored() -> None:
    text = build_connection_string(
        _sql_profile(options=ConnectionOptions(trust_server_certificate=False)), SECRET
    )
    assert "TrustServerCertificate=no" in text


def test_encrypt_option_is_honored() -> None:
    text = build_connection_string(_sql_profile(options=ConnectionOptions(encrypt=False)), SECRET)
    assert "Encrypt=no" in text


def test_integrated_auth_uses_trusted_connection() -> None:
    profile = _sql_profile(auth=AuthMode.INTEGRATED, username=None)
    text = build_connection_string(profile)
    assert "Trusted_Connection=yes" in text
    assert "PWD=" not in text
    assert "UID=" not in text


def test_integrated_auth_rejects_password() -> None:
    profile = _sql_profile(auth=AuthMode.INTEGRATED, username=None)
    with pytest.raises(ValueError, match="does not use a password"):
        build_connection_string(profile, SECRET)


def test_sql_auth_without_password_raises_auth_error() -> None:
    with pytest.raises(AuthError, match="no password"):
        build_connection_string(_sql_profile(), None)
    with pytest.raises(AuthError, match="no password"):
        build_connection_string(_sql_profile(), "")


def test_port_and_timeout_are_rendered() -> None:
    profile = _sql_profile(port=14330, options=ConnectionOptions(connect_timeout_s=15))
    text = build_connection_string(profile, SECRET)
    assert "SERVER=localhost,14330" in text
    assert "Connection Timeout=15" in text


def test_application_intent() -> None:
    text = build_connection_string(
        _sql_profile(options=ConnectionOptions(application_intent="ReadOnly")), SECRET
    )
    assert "ApplicationIntent=ReadOnly" in text


def test_values_with_separators_are_braced() -> None:
    text = build_connection_string(_sql_profile(host="host;evil=1"), SECRET)
    assert "SERVER={host;evil=1,1433}" in text
    assert SECRET not in text.split("SERVER=")[0]


def test_sanitize_masks_password_and_user() -> None:
    text = sanitize_connection_string(build_connection_string(_sql_profile(), SECRET))
    assert SECRET not in text
    assert "PWD=***" in text
    assert "UID=***" in text
    assert "SERVER=localhost,1433" in text


def test_driver_message_redaction() -> None:
    message = "failed: PWD=hunter2;UID=sa;Trusted_Connection=yes"
    cleaned = sanitize_driver_message(message)
    assert "hunter2" not in cleaned
    assert "sa" not in cleaned
    assert "hunter2" not in sanitize_driver_message("x PWD = topsecret y")


class _FakePyodbcError(Exception):
    """Mimics the pyodbc.Error args shape: (sqlstate, vendor, message)."""


def test_auth_error_mapping() -> None:
    error = _FakePyodbcError("28000", "18456", "Login failed for user 'sa'.")
    mapped = map_pyodbc_error(error)
    assert isinstance(mapped, AuthError)
    assert "18456" not in str(mapped)


def test_timeout_mapping() -> None:
    assert isinstance(
        map_pyodbc_error(_FakePyodbcError("HYT00", "", "Timeout expired")), ConnectError
    )


def test_connection_refused_mapping() -> None:
    mapped = map_pyodbc_error(_FakePyodbcError("08S01", "", "[Microsoft] server not found"))
    assert isinstance(mapped, ConnectError)
    assert not isinstance(mapped, AuthError)


def test_constraint_violation_names_the_constraint() -> None:
    error = _FakePyodbcError(
        "23000",
        "547",
        "The INSERT statement conflicted with the FOREIGN KEY constraint 'FK_Region_Country'.",
    )
    mapped = map_pyodbc_error(error)
    assert isinstance(mapped, QueryError)
    assert "FK_Region_Country" in str(mapped)
    assert mapped.vendor_code == 547


def test_missing_object_mapping() -> None:
    assert isinstance(
        map_pyodbc_error(_FakePyodbcError("42S02", "208", "Invalid object name")), MetadataError
    )


def test_password_never_appears_in_mapped_message() -> None:
    error = _FakePyodbcError("08001", "", f"connect failed PWD={SECRET}")
    assert SECRET not in str(map_pyodbc_error(error))


def test_app_errors_pass_through() -> None:
    original = MetadataError("already translated")
    assert map_pyodbc_error(original) is original


class _FakeCursor:
    def __init__(self, columns: list[str], rows: list[tuple[object, ...]]) -> None:
        self.description = [(name,) for name in columns]
        self._rows = rows
        self.closed = False

    def execute(self, sql: str, params: object = None) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def close(self) -> None:
        self.closed = True


def test_fetch_all_returns_plain_dicts() -> None:
    cursor = _FakeCursor(["a", "b"], [(1, "x"), (2, None)])
    rows = fetch_all(cursor, "SELECT 1", ())
    assert rows == [{"a": 1, "b": "x"}, {"a": 2, "b": None}]
    assert isinstance(rows[0], dict)
