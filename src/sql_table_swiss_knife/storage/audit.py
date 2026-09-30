"""Apply audit log: an append-only, local record of what was written and where.

An Apply is the only moment this application touches a database. When something goes
wrong later — a bad row, "who changed this flag on the catalog?" — the answer has to
survive the session, so every Apply appends one JSON object describing *what* ran.

What is recorded: the UTC timestamp, the profile and its environment, the server and
database, the table, the per-kind statement counts, and the statements themselves.

What is **never** recorded: credentials. A record carries no connection string, no
user id and no password — :func:`assert_no_credentials` re-checks the payload before it
reaches the file, so a future caller cannot widen the surface by accident (S-sec-3).
"""

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from ..domain.connection import Environment
from ..infra.errors import AppError
from .paths import audit_log_path

__all__ = [
    "AuditEntry",
    "AuditLog",
    "AuditLogError",
    "assert_no_credentials",
]

#: The three outcomes an Apply can have in the log.
_OUTCOMES = frozenset({"committed", "rolled_back", "refused"})


class AuditLogError(AppError):
    """The audit log exists but cannot be read or written."""


#: Patterns that must not appear anywhere in an audit payload (S-sec-3).
_CREDENTIAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("password", "password"),
    ("pwd=", "pwd="),
    ("secret", "secret"),
    ("uid=", "uid="),
    ("user id=", "user id="),
    ("integrated security", "integrated security"),
)


def assert_no_credentials(payload: str) -> None:
    """Raise when ``payload`` looks like it carries credentials.

    The check is deliberately blunt and case-insensitive: a false positive costs a
    failed audit write with an explicit reason, while a false negative would put a
    password in a file other users can read. The failure is loud, never silent.

    Raises:
        AuditLogError: naming the pattern that matched.
    """
    lowered = payload.lower()
    for pattern, label in _CREDENTIAL_PATTERNS:
        if pattern in lowered:
            raise AuditLogError(
                f"refusing to write an audit record containing {label!r}: the audit log "
                "must never carry credentials"
            )


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One Apply, as written to the log.

    ``statements`` holds the rendered SQL of what ran, in execution order. It is the
    literal rendering (values inlined, no bound parameters) because the point of the
    record is to be readable later by somebody auditing the change — a parameterized
    statement with no values would answer nothing.
    """

    #: UTC ISO-8601 timestamp; the log is read across machines, so it is never local.
    timestamp: str
    profile: str
    environment: str
    server: str
    database: str
    table: str
    #: ``{"insert": 1, "update": 2, "delete": 0}`` — the change mix of the Apply.
    counts: dict[str, int]
    #: The rendered statements, in execution order.
    statements: tuple[str, ...] = ()
    #: ``committed`` / ``rolled_back`` / ``refused``.
    outcome: str = "committed"
    #: Wall-clock duration the provider reported.
    duration_ms: int = 0
    #: Free-text failure reason when the outcome is not ``committed``.
    error: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in _OUTCOMES:
            known = ", ".join(sorted(_OUTCOMES))
            raise ValueError(f"outcome must be one of {known}, got {self.outcome!r}")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be >= 0")

    @property
    def statement_count(self) -> int:
        """How many statements this Apply ran."""
        return len(self.statements)

    @classmethod
    def now(
        cls,
        *,
        profile: str,
        environment: Environment,
        server: str,
        database: str,
        table: str,
        counts: dict[str, int],
        statements: tuple[str, ...] = (),
        outcome: str = "committed",
        duration_ms: int = 0,
        error: str | None = None,
    ) -> AuditEntry:
        """Build an entry stamped with the current UTC time."""
        return cls(
            timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
            profile=profile,
            environment=environment.value,
            server=server,
            database=database,
            table=table,
            counts=dict(counts),
            statements=tuple(statements),
            outcome=outcome,
            duration_ms=duration_ms,
            error=error,
        )

    def to_json(self) -> str:
        """The single line this entry occupies in the log file."""
        payload = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)
        assert_no_credentials(payload)
        return payload

    @classmethod
    def from_json(cls, line: str) -> AuditEntry:
        """Parse one log line.

        Raises:
            AuditLogError: when the line is not a readable record.
        """
        try:
            data = json.loads(line)
            return cls(
                timestamp=str(data["timestamp"]),
                profile=str(data["profile"]),
                environment=str(data["environment"]),
                server=str(data["server"]),
                database=str(data["database"]),
                table=str(data["table"]),
                counts={str(k): int(v) for k, v in dict(data.get("counts", {})).items()},
                statements=tuple(str(s) for s in data.get("statements", ())),
                outcome=str(data.get("outcome", "committed")),
                duration_ms=int(data.get("duration_ms", 0)),
                error=data.get("error"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuditLogError(f"cannot read audit entry: {exc}") from exc

    def without_statements(self) -> AuditEntry:
        """The same record with the SQL removed, keeping the counts (see ``AuditLog``)."""
        return replace(self, statements=())


@dataclass(slots=True)
class AuditLog:
    """Append-only JSONL file of :class:`AuditEntry` records.

    The file is opened in append mode per write and never buffered in memory, so a
    crash mid-session still leaves every completed Apply on disk. Reads are tolerant
    for the same reason: one corrupt line must not hide the rest of the history.
    """

    path: Path = field(default_factory=audit_log_path)
    #: When False the log records the *shape* of an Apply (who, where, how many) but
    #: not the statements — useful when the SQL is itself considered sensitive.
    include_statements: bool = True

    def record(self, entry: AuditEntry) -> None:
        """Append one entry.

        Raises:
            AuditLogError: when the payload carries credentials or the file is unwritable.
        """
        payload = entry if self.include_statements else entry.without_statements()
        line = payload.to_json()  # the credential check happens here
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+", encoding="utf-8") as handle:
                # A crash can leave the file without a trailing newline. Appending
                # straight onto that partial line would splice two records into one
                # unreadable line and lose the *new* record too, so the line is closed
                # first: the truncated remnant stays one skipped corrupt line and the
                # new entry survives intact.
                handle.seek(0, os.SEEK_END)
                if handle.tell() > 0:
                    handle.seek(handle.tell() - 1)
                    if handle.read(1) != "\n":
                        handle.write("\n")
                handle.write(line + "\n")
            os.chmod(self.path, 0o600)  # user-only (S-sec-7)
        except OSError as exc:
            raise AuditLogError(f"cannot write the audit log {self.path}: {exc}") from exc

    def entries(self, *, limit: int | None = None) -> tuple[AuditEntry, ...]:
        """Read the log oldest-first, optionally only the last ``limit`` entries."""
        if not self.path.exists():
            return ()
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AuditLogError(f"cannot read the audit log {self.path}: {exc}") from exc
        records: list[AuditEntry] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                records.append(AuditEntry.from_json(line))
            except AuditLogError:
                continue  # a half-written line must not hide the rest of the history
        if limit is not None:
            return tuple(records[-limit:]) if limit > 0 else ()
        return tuple(records)

    def __iter__(self) -> Iterator[AuditEntry]:
        return iter(self.entries())
