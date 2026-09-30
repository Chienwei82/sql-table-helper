"""Tests for connection profile domain models."""

import pytest

from sql_table_swiss_knife.domain import (
    AuthMode,
    ConnectionOptions,
    ConnectionProfile,
    ConnectionResult,
)


def test_sql_profile_requires_username() -> None:
    profile = ConnectionProfile(name="prod", provider="mssql", host="sql01", username="sa")
    assert profile.auth is AuthMode.SQL
    assert profile.secret_ref is None
    assert profile.options.driver == "ODBC Driver 18 for SQL Server"
    with pytest.raises(ValueError, match="username"):
        ConnectionProfile(name="prod", provider="mssql", host="sql01")


def test_integrated_profile_needs_no_username() -> None:
    profile = ConnectionProfile(
        name="corp", provider="mssql", host="sql02", auth=AuthMode.INTEGRATED
    )
    assert profile.auth is AuthMode.INTEGRATED
    assert profile.username is None


def test_profile_validation() -> None:
    with pytest.raises(ValueError, match="profile name"):
        ConnectionProfile(name="", provider="mssql", host="h")
    with pytest.raises(ValueError, match="provider"):
        ConnectionProfile(name="p", provider="", host="h")
    with pytest.raises(ValueError, match="host"):
        ConnectionProfile(name="p", provider="mssql", host="")
    with pytest.raises(ValueError, match="port"):
        ConnectionProfile(name="p", provider="mssql", host="h", port=70000)
    with pytest.raises(ValueError, match="secret reference"):
        ConnectionProfile(name="p", provider="mssql", host="h", username="u", secret_ref="")


def test_connection_options_validation() -> None:
    assert ConnectionOptions().encrypt  # default: encrypted
    with pytest.raises(ValueError, match="driver"):
        ConnectionOptions(driver="")
    with pytest.raises(ValueError, match="connect_timeout_s"):
        ConnectionOptions(connect_timeout_s=0)


def test_connection_result_validation() -> None:
    result = ConnectionResult(
        server_version="16.0.4105",
        server_name="SQL01",
        database="SwissKnifeSample",
        latency_ms=12,
        auth_used=AuthMode.SQL,
    )
    assert result.latency_ms == 12
    with pytest.raises(ValueError, match="latency_ms"):
        ConnectionResult(
            server_version="16",
            server_name="S",
            database="db",
            latency_ms=-1,
            auth_used=AuthMode.SQL,
        )
