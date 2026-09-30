"""Tests for the Apply audit log (M8 safety).

The properties that matter here are narrow and absolute: an Apply leaves a record
identifying who wrote where and what SQL, and *no* record ever contains a credential.
"""

from pathlib import Path

import pytest

from sql_table_swiss_knife.domain import Environment
from sql_table_swiss_knife.storage.audit import (
    AuditEntry,
    AuditLog,
    AuditLogError,
    assert_no_credentials,
)


def make_entry(**overrides: object) -> AuditEntry:
    """A committed Apply record with overridable fields."""
    defaults: dict[str, object] = {
        "timestamp": "2026-03-01T10:15:00+00:00",
        "profile": "catalog-prod",
        "environment": Environment.PRODUCTION.value,
        "server": "sql01:1433",
        "database": "CatalogDB",
        "table": "dbo.Country",
        "counts": {"insert": 0, "update": 1, "delete": 0},
        "statements": ("UPDATE dbo.Country SET Name = N'Japan 2' WHERE Code = 'JP';",),
        "duration_ms": 42,
    }
    return AuditEntry(**{**defaults, **overrides})  # type: ignore[arg-type]


# -- entry shape -------------------------------------------------------------


def test_entry_now_stamps_a_utc_timestamp() -> None:
    entry = AuditEntry.now(
        profile="dev",
        environment=Environment.DEVELOPMENT,
        server="localhost",
        database="CatalogDB",
        table="dbo.Country",
        counts={"update": 1},
    )
    assert entry.timestamp.endswith("+00:00")
    assert entry.environment == "development"


def test_entry_records_the_timestamp_profile_and_statements() -> None:
    entry = make_entry()
    assert entry.timestamp == "2026-03-01T10:15:00+00:00"
    assert entry.profile == "catalog-prod"
    assert entry.environment == "production"
    assert entry.table == "dbo.Country"
    assert entry.statement_count == 1
    assert "UPDATE dbo.Country" in entry.statements[0]


def test_entry_rejects_an_unknown_outcome() -> None:
    with pytest.raises(ValueError, match="outcome must be one of"):
        make_entry(outcome="maybe")


def test_entry_rejects_a_negative_duration() -> None:
    with pytest.raises(ValueError, match="duration_ms"):
        make_entry(duration_ms=-1)


def test_accepted_outcomes_are_the_three_that_exist() -> None:
    for outcome in ("committed", "rolled_back", "refused"):
        assert make_entry(outcome=outcome).outcome == outcome


# -- the credential guarantee ----------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "PWD=hunter2",
        "password=hunter2",
        "Driver={x};UID=sa;PWD=***",
        "Integrated Security=SSPI",
        "my secret value",
        "User ID=admin",
    ],
)
def test_credential_patterns_are_refused(payload: str) -> None:
    with pytest.raises(AuditLogError, match="must never carry credentials"):
        assert_no_credentials(payload)


def test_a_statement_carrying_a_password_is_never_written(tmp_path: Path) -> None:
    """The guard is on the serialized payload, not on a field name."""
    log = AuditLog(path=tmp_path / "audit.jsonl")
    entry = make_entry(statements=("SELECT * FROM t WHERE pw = 'PWD=topsecret';",))
    with pytest.raises(AuditLogError):
        log.record(entry)
    assert not (tmp_path / "audit.jsonl").exists()


def test_an_ordinary_payload_passes_the_guard() -> None:
    assert_no_credentials(make_entry().to_json())


# -- the log file -----------------------------------------------------------


def test_records_are_appended_one_per_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path)
    log.record(make_entry())
    log.record(make_entry(profile="dev", environment=Environment.DEVELOPMENT.value))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [entry.profile for entry in log.entries()] == ["catalog-prod", "dev"]


def test_entries_are_ordered_oldest_first(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl")
    for index in range(3):
        log.record(make_entry(table=f"dbo.T{index}"))
    assert [entry.table for entry in log] == ["dbo.T0", "dbo.T1", "dbo.T2"]


def test_limit_returns_the_most_recent_entries(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl")
    for index in range(5):
        log.record(make_entry(table=f"dbo.T{index}"))
    assert [entry.table for entry in log.entries(limit=2)] == ["dbo.T3", "dbo.T4"]


def test_a_missing_file_reads_as_an_empty_history(tmp_path: Path) -> None:
    assert AuditLog(path=tmp_path / "absent.jsonl").entries() == ()


def test_a_corrupt_line_does_not_hide_the_other_records(tmp_path: Path) -> None:
    """A half-written line from a crash must not make the history unreadable."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=path)
    log.record(make_entry(table="dbo.Good"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"profile": "trunc')  # no newline: a crash mid-write
    log.record(make_entry(table="dbo.AlsoGood"))
    assert [entry.table for entry in log.entries()] == ["dbo.Good", "dbo.AlsoGood"]


def test_the_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path=path).record(make_entry())
    assert path.stat().st_mode & 0o077 == 0  # no group/other access (S-sec-7)


def test_statements_can_be_omitted_from_the_record(tmp_path: Path) -> None:
    """A deployment that treats the SQL as sensitive keeps the shape, drops the SQL."""
    log = AuditLog(path=tmp_path / "audit.jsonl", include_statements=False)
    log.record(make_entry())
    (entry,) = log.entries()
    assert entry.statement_count == 0
    assert entry.counts == {"insert": 0, "update": 1, "delete": 0}


def test_a_failure_is_recorded_with_its_reason(tmp_path: Path) -> None:
    log = AuditLog(path=tmp_path / "audit.jsonl")
    log.record(
        make_entry(outcome="rolled_back", error="the FK PK_Country_Name rejected row (Code=DE)")
    )
    (entry,) = log.entries()
    assert entry.outcome == "rolled_back"
    assert "PK_Country_Name" in (entry.error or "")


def test_an_unwritable_path_raises_an_audit_error(tmp_path: Path) -> None:
    # A directory where the file should be: the open() fails for a reason the
    # caller must be able to report rather than swallow.
    (tmp_path / "audit.jsonl").mkdir()
    with pytest.raises(AuditLogError, match="cannot write the audit log"):
        AuditLog(path=tmp_path / "audit.jsonl").record(make_entry())


def test_round_trips_through_json() -> None:
    entry = make_entry()
    assert AuditEntry.from_json(entry.to_json()) == entry


def test_from_json_rejects_a_malformed_line() -> None:
    with pytest.raises(AuditLogError, match="cannot read audit entry"):
        AuditEntry.from_json("not json at all")


def test_from_json_reports_a_missing_field() -> None:
    with pytest.raises(AuditLogError, match="cannot read audit entry"):
        AuditEntry.from_json('{"profile": "x"}')
