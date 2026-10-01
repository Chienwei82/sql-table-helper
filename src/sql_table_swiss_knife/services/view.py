"""Grid view state: sort, quick filter, column visibility (FR-3.2, FR-3.3).

Everything here is **pure state plus pure functions**: the grid's view of a table (which
columns are shown, in what order, filtered how) is a value, not a pile of widget calls.
That makes the rules testable without a terminal and keeps "what am I looking at" in one
place the status bar can describe.

Two rules shape it:

* **Never the whole table.** Filtering and sorting become
  :class:`~domain.rows.RowFilter` / :class:`~domain.rows.SortKey` objects sent to the
  server; nothing here filters a Python list, because rows that were never loaded cannot
  be filtered locally (S-8).
* **Quick filter is per column.** "column contains X" and "column equals X" are different
  SQL predicates (``LIKE`` vs ``=``) and the user picks which — guessing wrong silently
  returns the wrong rows, which is worse than making the choice explicit.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import Enum

from ..domain.catalog import Column, Table
from ..domain.rows import FilterOp, RowFilter, SortKey
from ..providers.dialect import SqlDialect

__all__ = [
    "FilterMode",
    "GridView",
    "QuickFilter",
    "filter_for",
    "toggle_sort",
    "toggle_visible",
    "visible_columns",
]


class FilterMode(Enum):
    """How a quick-filter term is matched (FR-3.3)."""

    CONTAINS = "contains"
    EQUALS = "equals"

    @property
    def operator(self) -> FilterOp:
        """The SQL operator this mode maps to."""
        return FilterOp.LIKE if self is FilterMode.CONTAINS else FilterOp.EQ

    @property
    def label(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class QuickFilter:
    """One column filter: which column, how to match, and what to match."""

    column: str
    text: str
    mode: FilterMode = FilterMode.CONTAINS

    def __post_init__(self) -> None:
        if not self.column:
            raise ValueError("quick filter needs a column")
        if not isinstance(self.mode, FilterMode):
            raise ValueError(f"mode must be a FilterMode, got {self.mode!r}")

    @property
    def is_empty(self) -> bool:
        """A blank term means "no filter" rather than "match the empty string"."""
        return not self.text.strip()

    def describe(self) -> str:
        """``Name contains 'err'`` — the wording shown in the status bar."""
        if self.is_empty:
            return "no filter"
        return f"{self.column} {self.mode.label} '{self.text}'"


def filter_for(filters: Iterable[QuickFilter], dialect: SqlDialect) -> tuple[RowFilter, ...]:
    """Turn the quick filters into server-side predicates (FR-3.3).

    ``CONTAINS`` becomes ``LIKE '%text%'`` (a server-side substring search, so it also
    matches rows that were never loaded); ``EQUALS`` becomes a plain ``=``. Blank terms
    are dropped, so clearing the filter box really removes the filter.
    """
    predicates: list[RowFilter] = []
    for quick in filters:
        if quick.is_empty:
            continue
        value: object = (
            f"%{dialect.escape_like(quick.text.strip())}%"
            if quick.mode is FilterMode.CONTAINS
            else quick.text
        )
        predicates.append(RowFilter(column=quick.column, operator=quick.mode.operator, value=value))
    return tuple(predicates)


def toggle_sort(sort: Sequence[SortKey], column: str) -> tuple[SortKey, ...]:
    """Ascending → descending → unsorted, for one column (FR-3.2).

    Sorting by a second column replaces the first rather than appending: a single-column
    sort is what the status bar can describe unambiguously, and a multi-column sort is not
    worth extra keys in a dense grid.
    """
    current = sort[0] if sort else None
    if current is None or current.column != column:
        return (SortKey(column=column),)
    if not current.descending:
        return (SortKey(column=column, descending=True),)
    return ()


def toggle_visible(hidden: frozenset[str], column: str) -> frozenset[str]:
    """Show a hidden column / hide a visible one (FR-3.3)."""
    if column in hidden:
        return hidden - {column}
    return hidden | {column}


def visible_columns(table: Table, hidden: frozenset[str]) -> tuple[Column, ...]:
    """The table's columns minus the hidden ones, in ordinal order."""
    return tuple(column for column in table.columns if column.name not in hidden)


@dataclass(frozen=True, slots=True)
class GridView:
    """The complete, immutable description of what the grid is showing.

    Changing the view never touches the database or a widget: it produces a new
    ``GridView``, which the screen then turns into a fresh fetch. That is what keeps
    "rows on screen" and "what was asked for" from drifting apart.
    """

    hidden: frozenset[str] = frozenset()
    sort: tuple[SortKey, ...] = ()
    filters: tuple[QuickFilter, ...] = ()

    def columns(self, table: Table) -> tuple[Column, ...]:
        """Columns the grid should render for ``table``."""
        return visible_columns(table, self.hidden)

    def is_hidden(self, column: str) -> bool:
        return column in self.hidden

    def sort_label(self) -> str:
        """``Name ↑`` / ``Name ↓`` / ``unsorted`` for the status bar."""
        if not self.sort:
            return "unsorted"
        key = self.sort[0]
        return f"{key.column} {'↓' if key.descending else '↑'}"

    def filter_label(self) -> str:
        """The active quick filters as one line, or ``no filter``."""
        active = [quick for quick in self.filters if not quick.is_empty]
        if not active:
            return "no filter"
        return " · ".join(quick.describe() for quick in active)

    @property
    def is_filtered(self) -> bool:
        return any(not quick.is_empty for quick in self.filters)

    def predicates(self, dialect: SqlDialect) -> tuple[RowFilter, ...]:
        """The server-side filters this view implies, in this dialect's LIKE syntax."""
        return filter_for(self.filters, dialect)

    def sort_for(self, table: Table) -> tuple[SortKey, ...]:
        """The ORDER BY for a fetch, defaulting to the table's identity columns."""
        from .data import default_sort

        return self.sort if self.sort else default_sort(table)

    def with_sort(self, column: str) -> GridView:
        return replace(self, sort=toggle_sort(self.sort, column))

    def with_visibility(self, column: str) -> GridView:
        return replace(self, hidden=toggle_visible(self.hidden, column))

    def with_filter(self, quick: QuickFilter) -> GridView:
        """Add or replace the filter for ``quick.column``; a blank term clears it."""
        others = tuple(item for item in self.filters if item.column != quick.column)
        return replace(self, filters=() if quick.is_empty else (*others, quick))

    def with_filter_cleared(self, column: str) -> GridView:
        return replace(self, filters=tuple(f for f in self.filters if f.column != column))
