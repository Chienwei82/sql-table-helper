"""Safety policy: the rules that decide whether a write may proceed (M8, FR-8).

Every "may I write here?" question in the application is answered by this module,
which keeps the decisions **pure and testable without a terminal or a database**.
The TUI asks for a verdict and renders it; it never re-implements a rule.

Four independent guards, in the order a reviewer would expect them:

**Read-only posture** (S-9)
    A session is read-only when the profile says so, when the environment is
    production without an explicit opt-out, or when ``--read-only`` forced it for the
    process. Read-only is *not* "Apply is hidden" — it renders every cell read-only
    and refuses staging, so there is never a half-staged change set the user cannot
    apply.

**Identity** (S-4)
    A row with no usable key cannot be scoped by a WHERE clause, so UPDATE and DELETE
    are refused. INSERT is unaffected: a new row needs no key to be written, it needs
    one to be *found* later. This guard is what stops a "delete everything" where the
    user meant "delete this one".

**PROD friction** (FR-7.5)
    Against production an Apply requires a typed confirmation, and the plan shown to
    the user names the counts *and* the affected tables, because a count alone does
    not tell you which table you are about to change.

**Large deletes** (S-2)
    Above a configurable threshold of deletes the confirmation escalates. The threshold
    exists because "delete 60 rows" and "delete 6000 rows" look identical in a list.

The verdict is a dataclass rather than a bool so the UI can always say *why* — a bare
"no" is the kind of refusal that makes people retry until they get a yes.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ..domain.catalog import Table
from ..domain.changes import ChangeKind
from ..domain.connection import ConnectionProfile, Environment
from ..infra.errors import AppError

__all__ = [
    "DEFAULT_DELETE_CONFIRM_THRESHOLD",
    "PRODUCTION_CONFIRM_WORD",
    "ApplyVerdict",
    "BlockedReason",
    "SafetyPolicy",
    "TypedConfirmation",
    "WritePermission",
    "affected_tables",
    "describe_plan",
]


#: Above this many staged deletes an Apply needs the escalated confirmation (S-2).
DEFAULT_DELETE_CONFIRM_THRESHOLD = 50

#: The word a user must type to Apply against production (FR-7.5).
PRODUCTION_CONFIRM_WORD = "APPLY TO PRODUCTION"


class BlockedReason(Enum):
    """Why a write is refused — each carries its own user-facing wording."""

    READ_ONLY = "read_only"
    NO_IDENTITY = "no_identity"
    VIEW = "view"


class SafetyError(AppError):
    """A safety rule was violated in a way the caller could have prevented."""


@dataclass(frozen=True, slots=True)
class WritePermission:
    """Whether the session may write, and what to tell the user if it may not.

    ``reason`` is only meaningful when ``allowed`` is False; ``message`` is always
    populated so the UI never has to invent wording.
    """

    allowed: bool
    reason: BlockedReason | None = None
    message: str = ""

    @classmethod
    def allow(cls) -> WritePermission:
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: BlockedReason, message: str) -> WritePermission:
        return cls(allowed=False, reason=reason, message=message)


@dataclass(frozen=True, slots=True)
class TypedConfirmation:
    """How an Apply must be confirmed, and the text the user has to produce.

    ``word`` is ``None`` for the ordinary case (a yes/no dialog is enough); a word
    means the dialog must not accept ``enter`` until the user has typed it exactly.
    """

    word: str | None = None
    reason: str = ""

    @property
    def required(self) -> bool:
        return self.word is not None

    @property
    def label(self) -> str:
        """What the dialog tells the user to type."""
        return f"type {self.word}" if self.word else "confirm"


@dataclass(frozen=True, slots=True)
class ApplyVerdict:
    """The full answer to "may this Apply run, and how must it be confirmed?"."""

    allowed: bool
    #: Table-qualified names the Apply will touch, deduplicated and sorted.
    tables: tuple[str, ...] = ()
    #: ``N inserts / M updates / K deletes`` for the dialog headline (FR-7.5).
    summary: str = ""
    confirmation: TypedConfirmation = TypedConfirmation()
    #: Why the Apply was refused; ``None`` when it was allowed.
    reason: BlockedReason | None = None
    message: str = ""

    @classmethod
    def blocked(cls, reason: BlockedReason, message: str, tables: tuple[str, ...]) -> ApplyVerdict:
        return cls(allowed=False, reason=reason, message=message, tables=tables)

    @property
    def blocked_reason(self) -> BlockedReason | None:
        """Alias kept for call-site readability next to :attr:`reason`."""
        return self.reason


@dataclass(slots=True)
class SafetyPolicy:
    """The session's write posture, and the rules applied on top of it.

    Constructed once per session by the app and handed to the screens. The read-only
    state is *mutable* on purpose: the user toggles it with ``f5``, and that toggle is
    a deliberate act that the UI makes visible (the badge changes).
    """

    #: Read-only forced for the whole process (``--read-only``); a toggle cannot clear it.
    forced_read_only: bool = False
    #: Read-only currently in effect.
    read_only: bool = False
    #: Per-profile default, set when the session connects (S-9).
    environment: Environment = Environment.DEVELOPMENT
    #: Deletes above which the confirmation escalates (S-2).
    delete_confirm_threshold: int = DEFAULT_DELETE_CONFIRM_THRESHOLD
    #: When True, a keyless table may still be written to (S-4 override).
    allow_keyless_writes: bool = False

    @classmethod
    def for_profile(
        cls,
        profile: ConnectionProfile,
        *,
        forced_read_only: bool = False,
        delete_confirm_threshold: int = DEFAULT_DELETE_CONFIRM_THRESHOLD,
        allow_keyless_writes: bool = False,
    ) -> SafetyPolicy:
        """The policy a freshly connected profile opens with.

        A production profile starts read-only unless the profile explicitly opted out
        (``read_only=False``); ``--read-only`` overrides everything and cannot be
        toggled off.
        """
        read_only = forced_read_only or profile.read_only_effective
        return cls(
            forced_read_only=forced_read_only,
            read_only=read_only,
            environment=profile.environment,
            delete_confirm_threshold=delete_confirm_threshold,
            allow_keyless_writes=allow_keyless_writes,
        )

    # -- read-only posture ---------------------------------------------------

    def toggle_read_only(self) -> bool:
        """Flip read-only and return the new state.

        A session forced read-only by ``--read-only`` refuses the toggle rather than
        silently doing nothing: the process was started in a posture the user is not
        allowed to leave.
        """
        if self.forced_read_only:
            return self.read_only
        self.read_only = not self.read_only
        return self.read_only

    @property
    def read_only_reason(self) -> str:
        """Why the session is read-only — the wording shown in the badge tooltip."""
        if self.forced_read_only:
            return "read-only forced by --read-only"
        if self.read_only and self.environment is Environment.PRODUCTION:
            return f"read-only: {self.environment.badge} opens read-only"
        if self.read_only:
            return f"read-only: set for {self.environment.badge}"
        return ""

    # -- per-write rules -----------------------------------------------------

    def check_table(self, table: Table) -> WritePermission:
        """May the session write to ``table`` at all?

        Refuses a read-only session, a view (SPEC OQ-6: views are read-only in v1)
        and — unless explicitly overridden — a table whose rows have no usable key.
        """
        if self.read_only:
            detail = self.read_only_reason or "this session is read-only"
            return WritePermission.deny(
                BlockedReason.READ_ONLY,
                f"{table.ref} is read-only — {detail}. Press f5 to allow writes.",
            )
        if not table.updatable:
            if table.is_view:
                return WritePermission.deny(
                    BlockedReason.VIEW,
                    f"{table.ref} is a view; only base tables can be edited (OQ-6).",
                )
            if not self.allow_keyless_writes:
                return WritePermission.deny(
                    BlockedReason.NO_IDENTITY,
                    f"{table.ref} has no primary key or unique key, so an UPDATE or "
                    "DELETE cannot be scoped to a single row — it is read-only (S-4). "
                    "Use SQL if you really need to change it, or set "
                    "allow_keyless_writes = true in settings.toml.",
                )
        return WritePermission.allow()

    # -- the Apply verdict ---------------------------------------------------

    def review(
        self,
        tables: Sequence[Table | str],
        counts: Mapping[ChangeKind, int],
    ) -> ApplyVerdict:
        """Decide whether a staged Apply may run, and how it must be confirmed.

        This is the one function the Apply confirmation dialog asks. It returns
        everything the dialog needs: the affected tables, the counts, the confirmation
        requirement and — when refused — the wording to display.

        Args:
            tables: The tables the staged changes touch (the editor passes its one
                table; a multi-table Apply would pass all of them).
            counts: Per-kind staged counts, from the change set.

        Returns:
            An :class:`ApplyVerdict`. A refused verdict always carries a message and a
            :class:`BlockedReason`, so the caller renders rather than guesses.
        """
        names = affected_tables(tables)
        summary = describe_plan(counts)
        if summary == "nothing":
            return ApplyVerdict.blocked(
                BlockedReason.READ_ONLY,
                "There is nothing staged to apply.",
                names,
            )

        # The per-table rules: a read-only session, a view, or a keyless table.
        for table in _as_tables(tables):
            permission = self.check_table(table)
            if not permission.allowed:
                return ApplyVerdict.blocked(
                    permission.reason or BlockedReason.READ_ONLY,
                    permission.message,
                    names,
                )

        return ApplyVerdict(
            allowed=True,
            tables=names,
            summary=summary,
            confirmation=self.confirmation_for(counts),
        )

    def confirmation_for(self, counts: Mapping[ChangeKind, int]) -> TypedConfirmation:
        """How this particular Apply must be confirmed (FR-7.5, S-2).

        Two independent triggers, either of which escalates to a typed word:

        * **production** — a mistyped click is far likelier than a mistyped phrase;
        * **many deletes** — S-2 exists precisely because the *number* of deletes is
          the dangerous part, and "delete 60 rows" must not read like "delete 6".

        Other environments get the ordinary yes/no dialog: friction applied to every
        action stops being friction, and the escalation only means something because
        it is rare.
        """
        deletes = counts.get(ChangeKind.DELETE, 0)
        if self.environment.requires_typed_confirmation:
            return TypedConfirmation(
                word=PRODUCTION_CONFIRM_WORD,
                reason=f"this Apply writes to PRODUCTION ({self.environment.value})",
            )
        if deletes > self.delete_confirm_threshold:
            return TypedConfirmation(
                word="DELETE",
                reason=(
                    f"{deletes} deletes exceed the confirmation threshold of "
                    f"{self.delete_confirm_threshold}"
                ),
            )
        return TypedConfirmation()


def affected_tables(tables: Sequence[Table | str]) -> tuple[str, ...]:
    """Deduplicated, sorted ``schema.table`` names an Apply will touch.

    Accepts a mixed sequence of :class:`Table` objects and plain names, so a caller
    already holding statement targets does not have to build empty :class:`Table`
    objects just to call this — and so a list literal of both type-checks.
    """
    names: set[str] = set()
    for entry in tables:
        # ``Table.ref`` is a ``TableRef`` value object; the confirmation dialog and the
        # audit trail both want its ``schema.name`` text form.
        names.add(str(entry.ref) if isinstance(entry, Table) else entry)
    return tuple(sorted(names))


def describe_plan(counts: Mapping[ChangeKind, int]) -> str:
    """``2 inserts / 1 update / 3 deletes`` — the Apply headline (FR-7.5).

    Only the non-zero kinds are listed: "0 updates" in a summary is noise that makes
    the numbers that matter harder to see.
    """
    labels = {
        ChangeKind.INSERT: ("insert", "inserts"),
        ChangeKind.UPDATE: ("update", "updates"),
        ChangeKind.DELETE: ("delete", "deletes"),
    }
    parts: list[str] = []
    for kind, (singular, plural) in labels.items():
        count = counts.get(kind, 0)
        if count <= 0:
            continue
        parts.append(f"{count} {singular if count == 1 else plural}")
    return " / ".join(parts) if parts else "nothing"


def _as_tables(tables: Sequence[Table | str]) -> tuple[Table, ...]:
    """The entries that are real :class:`Table` objects (names carry no rules).

    A caller holding only names still gets the read-only and count checks; it just has
    no per-table identity rule to evaluate, because it has no metadata.
    """
    return tuple(entry for entry in tables if isinstance(entry, Table))
