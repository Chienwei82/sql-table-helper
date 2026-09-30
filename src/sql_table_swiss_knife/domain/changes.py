"""Staged-change domain models (pending inserts/updates/deletes)."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .identifiers import TableRef
from .rows import RowKey

__all__ = ["ChangeKind", "PendingChange"]


class ChangeKind(Enum):
    """Kind of staged change."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


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
