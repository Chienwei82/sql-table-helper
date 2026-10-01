"""Tests for the ODBC connection-string builder, sanitizing and error mapping."""

import datetime
import struct

import pytest

from sql_table_swiss_knife.domain import AuthMode, ConnectionOptions, ConnectionProfile
from sql_table_swiss_knife.providers import AuthError, ConnectError, MetadataError, QueryError
from sql_table_swiss_knife.providers.mssql.connection import (
    _DATETIMEOFFSET_FORMAT,
    build_connection_string,
    decode_datetimeoffset,
    execute,
    fetch_all,
    sanitize_connection_string,
)
from sql_table_swiss_knife.providers.mssql.errors import map_pyodbc_error, sanitize_driver_message
from sql_table_swiss_knife.providers.mssql.metadata import COLUMNS_SQL, column_from_row

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


#: Passwords whose characters make ODBC brace-quoting kick in. Each of these leaked a
#: fragment of the secret into :func:`sanitize_connection_string`'s output, which is the
#: one string allowed to reach a log or a user-facing message.
AWKWARD_PASSWORDS = [
    "hunter2;TAIL",
    "semi;colon;and;more",
    "brace}inside",
    "brace{inside",
    "};UID=spoofed;PWD=spoofed;{",
    "}UID=spoofed",
]


@pytest.mark.parametrize("password", AWKWARD_PASSWORDS)
def test_sanitize_never_leaks_any_part_of_an_awkward_password(password: str) -> None:
    """A secret split across chunks used to survive masking as a bare flag chunk.

    ``PWD={hunter2;TAIL}`` splits naively into ``PWD={hunter2`` (masked) and ``TAIL}``
    — no ``=``, so it was passed through verbatim into the log.
    """
    sanitized = sanitize_connection_string(build_connection_string(_sql_profile(), password))
    for fragment in password.split(";"):
        assert len(fragment) < 3 or fragment not in sanitized, sanitized
    assert "spoofed" not in sanitized


@pytest.mark.parametrize("password", ["p}w", "a{b}c"])
def test_braces_inside_a_value_are_doubled_for_the_odbc_parser(password: str) -> None:
    """An unescaped ``}`` closes the value early, so the driver gets a truncated password."""
    doubled = password.replace("}", "}}").replace("{", "{{")
    assert f"PWD={{{doubled}}}" in build_connection_string(_sql_profile(), password)


def test_a_value_containing_a_brace_round_trips_through_the_splitter() -> None:
    """The splitter the sanitizer relies on must agree with the quoting that produced it.

    Counting braces cannot check this — doubling makes a chunk's brace count odd by
    design — so the check is behavioural: the masked output must not contain the value,
    and the non-secret keywords must survive intact.
    """
    from sql_table_swiss_knife.providers.mssql.connection import _split_chunks

    text = build_connection_string(_sql_profile(), "a;b}c")
    chunks = _split_chunks(text)
    assert "PWD={a;b}}c}" in chunks
    assert "SERVER=localhost,1433" in chunks
    assert "Encrypt=yes" in chunks
    sanitized = sanitize_connection_string(text)
    assert "PWD=***" in sanitized
    assert "SERVER=localhost,1433" in sanitized


def test_driver_message_redaction() -> None:
    message = "failed: PWD=hunter2;UID=sa;Trusted_Connection=yes"
    cleaned = sanitize_driver_message(message)
    assert "hunter2" not in cleaned
    assert "sa" not in cleaned
    assert "hunter2" not in sanitize_driver_message("x PWD = topsecret y")


class _FakePyodbcError(Exception):
    """Mimics the pyodbc.Error args shape: (sqlstate, vendor, message)."""


class _RealShapePyodbcError(Exception):
    """Mimics the args shape pyodbc actually raises: ``(sqlstate, message)``.

    The vendor number only appears inside the message text, so the old parsing that read
    ``args[1]`` as the vendor code never matched and login failures surfaced as
    ``ProviderError: InterfaceError: 28000`` instead of ``AuthError``.
    """


def test_login_failure_maps_to_auth_error_with_the_real_args_shape() -> None:
    error = _RealShapePyodbcError(
        "28000",
        "[28000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
        "Login failed for user 'no_such_login_12345'. (18456) (SQLDriverConnect)",
    )
    mapped = map_pyodbc_error(error)
    assert isinstance(mapped, AuthError)
    assert "no_such_login_12345" not in str(mapped)
    assert "18456" not in str(mapped)


def test_unknown_database_maps_to_auth_error_with_the_real_args_shape() -> None:
    error = _RealShapePyodbcError(
        "42000",
        "[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
        'Cannot open database "NoSuchDatabase_98765" requested by the login. (4060)',
    )
    assert isinstance(map_pyodbc_error(error), AuthError)


def test_missing_driver_library_maps_to_connect_error_with_the_real_args_shape() -> None:
    error = _RealShapePyodbcError(
        "01000",
        "[01000] [unixODBC][Driver Manager]Can't open lib 'ODBC Driver 99 for SQL Server' : "
        "file not found (0) (SQLDriverConnect)",
    )
    mapped = map_pyodbc_error(error)
    assert isinstance(mapped, ConnectError)
    assert "driver" in str(mapped).lower()


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
    """Records how ``execute`` was called, distinguishing an omitted argument from ``None``.

    pyodbc treats an explicit ``None`` as one NULL parameter, so the distinction is the
    whole point of these tests; a fake with a default of ``None`` would hide the bug.
    """

    def __init__(self, columns: list[str], rows: list[tuple[object, ...]]) -> None:
        self.description = [(name,) for name in columns]
        self._rows = rows
        self.closed = False
        self.params_provided = False
        self.params: object = None

    def execute(self, sql: str, *args: object) -> None:
        self.sql = sql
        self.params_provided = bool(args)
        self.params = args[0] if args else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def close(self) -> None:
        self.closed = True


def test_columns_sql_selects_no_nonexistent_sys_columns_field() -> None:
    """Regression: ``sys.columns`` has no ``is_rowversion`` column.

    Selecting it made every metadata read fail against a real server with
    "Invalid column name 'is_rowversion'" (SQLSTATE 42S22). The rowversion flag is
    derived from the type name instead, which is what the server does expose.
    """
    assert "is_rowversion" not in COLUMNS_SQL
    assert "sys.columns" in COLUMNS_SQL


def test_rowversion_is_detected_from_the_type_name() -> None:
    row = {
        "column_name": "RowVer",
        "column_id": 1,
        "data_type": "timestamp",
        "max_length": 8,
        "is_nullable": 0,
        "is_identity": 0,
        "is_computed": 0,
    }
    assert column_from_row(row).is_rowversion is True
    assert column_from_row({**row, "data_type": "int"}).is_rowversion is False


def test_decode_datetimeoffset_accepts_a_buffer_like_value() -> None:
    """A ``NameError`` here only showed up when a table actually had a datetimeoffset.

    The cast helper was used but never imported, so the branch was dead until a live
    read reached it.
    """
    raw = struct.pack(_DATETIMEOFFSET_FORMAT, 2024, 5, 17, 10, 30, 0, 0, 120)
    assert decode_datetimeoffset(raw) == datetime.datetime(
        2024, 5, 17, 10, 30, tzinfo=datetime.timezone(datetime.timedelta(minutes=120))
    )


def test_decode_datetimeoffset_passes_none_through() -> None:
    assert decode_datetimeoffset(None) is None


def test_columns_sql_casts_identity_seed_away_from_an_unreadable_odbc_type() -> None:
    """Regression: raw ``sys.identity_columns`` values are unreadable through pyodbc.

    They arrive as ODBC SQL type -16 and raise "ODBC SQL type -16 is not yet supported",
    which broke the metadata read for every table. The cast makes them plain bigints.
    """
    assert "CAST(ic.seed_value AS bigint) AS seed_value" in COLUMNS_SQL
    assert "CAST(ic.increment_value AS bigint) AS increment_value" in COLUMNS_SQL


def test_temporal_period_columns_are_server_managed() -> None:
    """``GENERATED ALWAYS`` period columns must not be offered as editable."""
    row = {
        "column_name": "ValidFrom",
        "column_id": 2,
        "data_type": "datetime2",
        "is_nullable": 0,
        "is_identity": 0,
        "is_computed": 0,
        "generated_always_type": 1,
    }
    column = column_from_row(row)
    assert column.generated_always_type == 1
    assert column.is_server_managed is True

    assert column_from_row({**row, "generated_always_type": 0}).is_server_managed is False


def test_identity_seed_and_increment_are_read_as_ints() -> None:
    row = {
        "column_name": "Id",
        "column_id": 1,
        "data_type": "int",
        "max_length": 4,
        "is_nullable": 0,
        "is_identity": 1,
        "is_computed": 0,
        "seed_value": 1000,
        "increment_value": 5,
    }
    column = column_from_row(row)
    assert column.identity_seed == 1000
    assert column.identity_increment == 5


def test_fetch_all_returns_plain_dicts() -> None:
    cursor = _FakeCursor(["a", "b"], [(1, "x"), (2, None)])
    rows = fetch_all(cursor, "SELECT 1", ())
    assert rows == [{"a": 1, "b": "x"}, {"a": 2, "b": None}]
    assert isinstance(rows[0], dict)


def test_execute_omits_the_argument_when_there_are_no_parameters() -> None:
    """Regression: pyodbc 5.3 reads an explicit ``None`` as a single NULL parameter.

    Passing ``None`` through made every parameterless statement fail against a real
    server with "The SQL contains 0 parameter markers, but 1 parameters were supplied".
    """
    cursor = _FakeCursor([], [])
    execute(cursor, "DELETE FROM t")
    assert cursor.params_provided is False


def test_execute_passes_an_empty_sequence_without_a_null_parameter() -> None:
    cursor = _FakeCursor([], [])
    execute(cursor, "DELETE FROM t", ())
    assert cursor.params_provided is False


def test_execute_forwards_real_parameters_as_a_tuple() -> None:
    cursor = _FakeCursor([], [])
    execute(cursor, "SELECT ?", [7, "x"])
    assert cursor.params_provided is True
    assert cursor.params == (7, "x")


def test_fetch_all_with_no_params_does_not_supply_a_null() -> None:
    cursor = _FakeCursor(["n"], [(1,)])
    assert fetch_all(cursor, "SELECT 1") == [{"n": 1}]
    assert cursor.params_provided is False


class _FakeDmlCursor(_FakeCursor):
    """A cursor for a statement with no result set, as pyodbc reports it.

    ``fetchall`` on such a cursor raises; the fake reproduces that so a test cannot
    silently pass by not reading rows.
    """

    def __init__(self) -> None:
        super().__init__([], [])
        self.fetchall_called = False

    def fetchall(self) -> list[tuple[object, ...]]:
        self.fetchall_called = True
        raise AssertionError("fetchall must not be called on a statement with no result set")


def test_fetch_all_returns_empty_for_a_statement_with_no_result_set() -> None:
    """Regression: DML run for its effect has an empty description, not rows.

    Reading ``fetchall`` anyway raised "No results. Previous SQL was not a query."
    """
    cursor = _FakeDmlCursor()
    assert fetch_all(cursor, "DELETE FROM t") == []
    assert cursor.fetchall_called is False
