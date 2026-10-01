"""Staged-change domain models (pending inserts/updates/deletes) and the staging area."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum

from .identifiers import TableRef
from .rows import RowKey

__all__ = [
    "NEW_ROW_COLUMN",
    "ChangeKind",
    "ChangeSet",
    "PendingChange",
    "describe_changes",
    "is_new_row",
    "new_row_key",
    "row_label",
]

#: Column name used as the sole member of the synthetic key of a not-yet-inserted row.
#: A real table can never have it (``validate_identifier`` rejects ``#``), so a staged
#: row can never collide with a fetched one.
NEW_ROW_COLUMN = "#new"


class ChangeKind(Enum):
    """Kind of staged change."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


def new_row_key(counter: int) -> RowKey:
    """Synthetic row key for a staged (not yet inserted) row.

    INSERTs have no database identity, so the staging area needs *some* addressable
    handle for them. The key is deliberately a shape real metadata cannot produce.
    """
    return ((NEW_ROW_COLUMN, counter),)


def is_new_row(key: RowKey | None) -> bool:
    """True when ``key`` is a synthetic key from :func:`new_row_key`."""
    return key is not None and len(key) == 1 and key[0][0] == NEW_ROW_COLUMN


def row_label(key: RowKey | None) -> str:
    """Short human label for a row key, used in status text and error messages."""
    if key is None:
        return "(new row)"
    return ", ".join(f"{name}={value!r}" for name, value in key)


def describe_changes(counts: Mapping[ChangeKind, int]) -> str:
    """``2 inserts, 1 update, 3 deletes`` wording for the status bar (FR-7.2)."""
    parts: list[str] = []
    for kind, singular, plural in (
        (ChangeKind.INSERT, "insert", "inserts"),
        (ChangeKind.UPDATE, "update", "updates"),
        (ChangeKind.DELETE, "delete", "deletes"),
    ):
        count = counts.get(kind, 0)
        if count == 1:
            parts.append(f"1 {singular}")
        elif count > 1:
            parts.append(f"{count} {plural}")
    return ", ".join(parts) if parts else "no changes"


@dataclass(frozen=True, slots=True)
class PendingChange:
    """One staged change against a single table row (in-memory only until Apply).

    - INSERT: key=None, before=None, after=values to insert
    - UPDATE: key=row identity, before=original values, after=staged values
    - DELETE: key=row identity, before=original values, after=None
    """

    kind: ChangeKind
    table: TableRef
    key: RowKey | None = None
    before: Mapping[str, object] | None = None
    after: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.before is not None:
            object.__setattr__(self, "before", dict(self.before))
        if self.after is not None:
            object.__setattr__(self, "after", dict(self.after))
        match self.kind:
            case ChangeKind.INSERT:
                if self.key is not None:
                    raise ValueError("INSERT must not carry a row key")
                if self.before is not None:
                    raise ValueError("INSERT must not carry before-values")
                if self.after is None:
                    raise ValueError("INSERT requires after-values")
            case ChangeKind.UPDATE:
                if self.key is None:
                    raise ValueError("UPDATE requires a row key")
                if self.before is None or self.after is None:
                    raise ValueError("UPDATE requires both before- and after-values")
            case ChangeKind.DELETE:
                if self.key is None:
                    raise ValueError("DELETE requires a row key")
                if self.before is None:
                    raise ValueError("DELETE requires before-values")
                if self.after is not None:
                    raise ValueError("DELETE must not carry after-values")

    @property
    def changed_columns(self) -> tuple[str, ...]:
        """Columns whose staged value differs from the original (UPDATE only)."""
        if self.kind is not ChangeKind.UPDATE:
            raise ValueError("changed_columns is only defined for UPDATE")
        if self.before is None or self.after is None:
            raise ValueError("UPDATE must carry both before- and after-values")
        return tuple(
            name
            for name, value in self.after.items()
            if name not in self.before or self.before[name] != value
        )


@dataclass(frozen=True, slots=True)
class StagedRow:
    """One row's staged change *plus* the key that addresses it.

    The key is held here rather than on the :class:`PendingChange` because an INSERT must
    not carry a row key — the server assigns it. A staged row still needs an address so the
    grid can render, re-edit and revert it, so the key for a new row is the synthetic one
    from :func:`new_row_key`, and ``change.key`` stays ``None`` for INSERTs exactly as the
    domain model requires.
    """

    key: RowKey
    change: PendingChange

    @property
    def kind(self) -> ChangeKind:
        return self.change.kind

    @property
    def is_new(self) -> bool:
        return is_new_row(self.key)

    def values_for(self, original: Mapping[str, object]) -> Mapping[str, object]:
        """Staged values over the fetched ones: ``display = staged ?? original``."""
        if self.change.after is None:
            return dict(original)
        return {**original, **self.change.after}


@dataclass(slots=True)
class ChangeSet:
    """The ordered, deduplicated staging area for one table (DESIGN §7.1).

    This is the single source of truth for "what will Apply do". It owns the collapse
    rules the design calls for, so no caller has to remember them:

    * editing a fetched cell twice collapses into **one** UPDATE;
    * editing a staged INSERT row updates that INSERT (it is not in the database yet);
    * marking a fetched row for deletion after editing it drops the UPDATE and yields a
      plain DELETE (its original values are still needed for the WHERE clause);
    * deleting a staged INSERT row just removes the INSERT (nothing to do in the DB);
    * editing a row back to its original value unstages the row entirely.

    Undo/redo is implemented with whole-changeset snapshots: the staged set is small
    (one entry per touched row), so copying it is cheaper than maintaining a per-command
    inverse, and a snapshot can never drift out of sync with the list it represents.
    """

    table: TableRef
    rows: list[StagedRow] = field(default_factory=list)
    _undo: list[list[StagedRow]] = field(default_factory=list)
    _redo: list[list[StagedRow]] = field(default_factory=list)
    _new_rows: int = 0
    #: Pre-batch snapshot while a :meth:`batch` block is open, or ``None``.
    _batch_base: list[StagedRow] | None = None

    # -- queries ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[StagedRow]:
        return iter(self.rows)

    @property
    def changes(self) -> tuple[PendingChange, ...]:
        """The staged changes, in staging order (what Apply sends)."""
        return tuple(row.change for row in self.rows)

    @property
    def is_empty(self) -> bool:
        return not self.rows

    @property
    def counts(self) -> dict[ChangeKind, int]:
        """Number of staged changes per kind — the status-bar numbers (FR-7.2)."""
        counts = dict.fromkeys(ChangeKind, 0)
        for row in self.rows:
            counts[row.kind] += 1
        return counts

    @property
    def summary(self) -> str:
        """``2 inserts, 1 update`` wording for the status bar."""
        return describe_changes(self.counts)

    def find(self, key: RowKey) -> StagedRow | None:
        """The staged row for ``key``, or ``None`` when the row is untouched."""
        for row in self.rows:
            if row.key == key:
                return row
        return None

    def keys(self) -> tuple[RowKey, ...]:
        """Staged row keys in staging order."""
        return tuple(row.key for row in self.rows)

    def _of_kind(self, kind: ChangeKind) -> tuple[PendingChange, ...]:
        return tuple(row.change for row in self.rows if row.kind is kind)

    def inserts(self) -> tuple[PendingChange, ...]:
        return self._of_kind(ChangeKind.INSERT)

    def updates(self) -> tuple[PendingChange, ...]:
        return self._of_kind(ChangeKind.UPDATE)

    def deletes(self) -> tuple[PendingChange, ...]:
        return self._of_kind(ChangeKind.DELETE)

    def values_for(self, key: RowKey, original: Mapping[str, object]) -> Mapping[str, object]:
        """The values the grid should display for ``key``: staged over original.

        A row marked for deletion keeps showing its values, dimmed.
        """
        staged = self.find(key)
        if staged is None:
            return dict(original)
        return staged.values_for(original)

    def is_staged(self, key: RowKey) -> bool:
        return self.find(key) is not None

    # -- staging ------------------------------------------------------------

    def stage_cell(
        self, key: RowKey, column: str, new_value: object, original: Mapping[str, object]
    ) -> StagedRow | None:
        """Stage one edited cell against ``key``.

        Args:
            key: The row's identity (or a synthetic key for a staged INSERT row).
            column: Column being edited.
            new_value: The parsed value to stage.
            original: The fetched values of the row (empty for a staged INSERT).

        Returns:
            The row's change after staging, or ``None`` when the edit reverted the row
            back to its original state (nothing left staged).
        """
        existing = self.find(key)
        if existing is not None and existing.is_new:
            after = dict(existing.change.after or {})
            after[column] = new_value
            self._commit(
                existing, StagedRow(key, PendingChange(ChangeKind.INSERT, self.table, after=after))
            )
            return self.find(key)
        if existing is not None and existing.kind is ChangeKind.DELETE:
            # A row marked for deletion has no staged "after"; the edit un-deletes it and
            # applies the new value on top of the original values.
            before = dict(existing.change.before or {})
            update = _as_update(self.table, key, before, column, new_value)
            self._commit(existing, StagedRow(key, update))
            return self.find(key)
        before = (
            dict(existing.change.before)
            if existing is not None and existing.change.before
            else dict(original)
        )
        after = (
            dict(existing.change.after)
            if existing is not None and existing.change.after
            else dict(before)
        )
        after[column] = new_value
        if not _differs(before, after):
            self._commit(existing, None)  # editing back to the original unstages the row
            return None
        self._commit(
            existing,
            StagedRow(
                key,
                PendingChange(ChangeKind.UPDATE, self.table, key=key, before=before, after=after),
            ),
        )
        return self.find(key)

    def stage_insert(self, values: Mapping[str, object]) -> RowKey:
        """Stage a brand new row and return the synthetic key addressing it."""
        key = new_row_key(self._new_rows)
        self._new_rows += 1
        # The INSERT carries no row key (the server assigns it on Apply); the synthetic key
        # on the StagedRow is what the grid renders and re-edits.
        self._commit(
            None,
            StagedRow(key, PendingChange(ChangeKind.INSERT, self.table, after=dict(values))),
        )
        return key

    def stage_duplicate(self, values: Mapping[str, object]) -> RowKey:
        """Stage a copy of an existing row; the caller fills in the remaining cells."""
        return self.stage_insert(values)

    def stage_delete(self, key: RowKey, original: Mapping[str, object]) -> None:
        """Mark a row for deletion (FR-7.4).

        Deleting a row that only had staged edits yields a DELETE (the UPDATE is dropped
        but its original values are preserved for the WHERE clause); deleting a staged
        INSERT simply removes the INSERT.
        """
        existing = self.find(key)
        if existing is not None and existing.is_new:
            self._commit(existing, None)
            return
        before = (
            dict(existing.change.before)
            if existing is not None and existing.change.before is not None
            else dict(original)
        )
        self._commit(
            existing,
            StagedRow(key, PendingChange(ChangeKind.DELETE, self.table, key=key, before=before)),
        )

    # -- reverting ----------------------------------------------------------

    def revert(self, key: RowKey) -> bool:
        """Drop the change staged for ``key``. Returns True when something changed."""
        existing = self.find(key)
        if existing is None:
            return False
        self._commit(existing, None)
        return True

    def clear(self) -> None:
        """Discard every staged change (undoable, so it can be brought back)."""
        if not self.rows:
            return
        self._commit(self.rows[0], None, discard_all=True)

    # -- undo / redo --------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        """Restore the previous staging state. True when something changed.

        The restored list is a copy: handing the snapshot's own objects back to callers
        would let a later in-place edit of a staged value rewrite the history, so an undo
        would no longer be able to return the state the user actually had.
        """
        if not self._undo:
            return False
        self._redo.append(_clone_rows(self.rows))
        self.rows = _clone_rows(self._undo.pop())
        return True

    def redo(self) -> bool:
        """Re-apply the last undone staging step. True when something changed."""
        if not self._redo:
            return False
        self._undo.append(_clone_rows(self.rows))
        self.rows = _clone_rows(self._redo.pop())
        return True

    # -- batching ------------------------------------------------------------

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Group many mutations into one undoable step.

        Snapshotting per cell is what makes a paste of *n* cells cost O(n²) — every cell
        deep-copies a staged set that already holds *n* rows — and it also leaves the
        paste as *n* undo steps, so undoing one paste takes *n* presses of ``ctrl+z``.
        Inside this block the set is mutated freely and snapshotted once, on entry, and
        that single snapshot becomes the one undo step on exit.

        A batch that changes nothing leaves the history untouched, which keeps the
        guarantee that ``ctrl+z`` always undoes a real user action.
        """
        if self._batch_base is not None:
            # Already batching: an inner block must not close the outer one.
            yield
            return
        self._batch_base = _clone_rows(self.rows)
        try:
            yield
        finally:
            base, self._batch_base = self._batch_base, None
            if base is not None and _clone_rows(self.rows) != base:
                self._undo.append(base)
                self._redo.clear()

    # -- internals ----------------------------------------------------------

    def _commit(
        self,
        existing: StagedRow | None,
        replacement: StagedRow | None,
        *,
        discard_all: bool = False,
    ) -> None:
        """Apply one mutation atomically, snapshotting first so it can be undone.

        A no-op mutation (replacing a row with an identical one, or removing an absent
        key) leaves the undo history alone, so ``ctrl+z`` always undoes a real user
        action rather than an internal no-op. Inside :meth:`batch` no snapshot is taken
        here; the enclosing block records one for the whole group instead.
        """
        if self._batch_base is not None:
            self._mutate(existing, replacement, discard_all=discard_all)
            return
        snapshot = _clone_rows(self.rows)
        self._mutate(existing, replacement, discard_all=discard_all)
        if _clone_rows(self.rows) == snapshot:
            return
        self._undo.append(snapshot)
        self._redo.clear()

    def _mutate(
        self,
        existing: StagedRow | None,
        replacement: StagedRow | None,
        *,
        discard_all: bool = False,
    ) -> None:
        """Apply the mutation to the staged list, with no undo bookkeeping."""
        if discard_all:
            self.rows = []
        elif existing is None:
            if replacement is None:
                return
            self.rows.append(replacement)
        elif replacement is None:
            self.rows = [row for row in self.rows if row is not existing]
        elif self.rows[self.rows.index(existing)] == replacement:
            return
        else:
            self.rows[self.rows.index(existing)] = replacement


def _as_update(
    table: TableRef,
    key: RowKey,
    before: Mapping[str, object],
    column: str,
    new_value: object,
) -> PendingChange:
    """The UPDATE that results from editing one cell of an already-staged row."""
    return PendingChange(
        ChangeKind.UPDATE,
        table,
        key=key,
        before=before,
        after={**before, column: new_value},
    )


def _differs(before: Mapping[str, object], after: Mapping[str, object]) -> bool:
    """True when any column in ``after`` differs from ``before``."""
    return any(before.get(name) != value for name, value in after.items())


def _clone_rows(rows: Sequence[StagedRow]) -> list[StagedRow]:
    """Copy of a staged-row list.

    ``PendingChange`` is frozen and copies its own value mappings, and ``StagedRow`` is
    frozen too, so a shallow rebuild is a genuine deep copy: nothing a caller does to a
    restored row's mappings can reach back into the snapshot.
    """
    return [
        StagedRow(
            row.key,
            PendingChange(
                kind=row.change.kind,
                table=row.change.table,
                key=row.change.key,
                before=row.change.before,
                after=row.change.after,
            ),
        )
        for row in rows
    ]
