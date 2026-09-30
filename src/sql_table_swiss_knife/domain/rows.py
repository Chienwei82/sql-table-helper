"""Row-level domain models: values, keys, fetch specifications, pages."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from .identifiers import validate_identifier

__all__ = [
    "FetchSpec",
    "FilterOp",
    "Row",
    "RowFilter",
    "RowKey",
    "RowPage",
    "SortKey",
    "make_row_key",
]

#: Ordered (column, value) pairs uniquely identifying a row.
type RowKey = tuple[tuple[str, object], ...]


class FilterOp(Enum):
    """Comparison operators supported by fetch filters."""

    EQ = "="
    NE = "<>"
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    LIKE = "LIKE"
    IS_NULL = "IS NULL"
    IS_NOT_NULL = "IS NOT NULL"

    @property
    def needs_value(self) -> bool:
        return self not in {FilterOp.IS_NULL, FilterOp.IS_NOT_NULL}


@dataclass(frozen=True, slots=True)
class Row:
    """One fetched row: column name → value (None means SQL NULL)."""

    values: Mapping[str, object]

    def __post_init__(self) -> None:
        # Defensive copy: the frozen dataclass must not alias caller state.
        object.__setattr__(self, "values", dict(self.values))

    def __getitem__(self, column: str) -> object:
        return self.values[column]

    def get(self, column: str, default: object | None = None) -> object | None:
        return self.values.get(column, default)

    def __contains__(self, column: str) -> bool:
        return column in self.values


@dataclass(frozen=True, slots=True)
class SortKey:
    """ORDER BY element."""

    column: str
    descending: bool = False

    def __post_init__(self) -> None:
        validate_identifier(self.column, kind="sort column")


@dataclass(frozen=True, slots=True)
class RowFilter:
    """One WHERE predicate for a fetch."""

    column: str
    operator: FilterOp
    value: object | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.column, kind="filter column")
        if not isinstance(self.operator, FilterOp):
            raise ValueError(f"operator must be a FilterOp, got {self.operator!r}")
        if self.operator.needs_value and self.value is None:
            raise ValueError(f"filter operator {self.operator.value!r} requires a value")


@dataclass(frozen=True, slots=True)
class FetchSpec:
    """Paged, sortable, filterable row request (DESIGN §5, S-8 default limit).

    ``after_key`` turns the request into a *keyset* page: the identity value of the last
    row of the previous page, so the provider can ask for "everything after this row"
    instead of counting rows with ``OFFSET``. It is ignored when the requested order does
    not match the identity columns (see ``providers.sqlgen._keyset_usable``).
    """

    limit: int = 1000
    offset: int = 0
    sort: tuple[SortKey, ...] = ()
    filters: tuple[RowFilter, ...] = ()
    after_key: RowKey | None = None

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"limit must be >= 1, got {self.limit}")
        if self.offset < 0:
            raise ValueError(f"offset must be >= 0, got {self.offset}")


@dataclass(frozen=True, slots=True)
class RowPage:
    """One page of fetched rows."""

    rows: tuple[Row, ...]
    offset: int
    limit: int
    has_more: bool
    total_row_count: int | None = None

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError("offset must be >= 0")
        if self.limit < 1:
            raise ValueError("limit must be >= 1")
        if len(self.rows) > self.limit:
            raise ValueError(f"page has {len(self.rows)} rows but limit is {self.limit}")
        if self.total_row_count is not None and self.total_row_count < 0:
            raise ValueError("total_row_count must be >= 0 when set")

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def is_empty(self) -> bool:
        return not self.rows


def make_row_key(columns: Sequence[str], row: Mapping[str, object]) -> RowKey:
    """Build a RowKey from the given columns of a row (order preserved).

    Raises:
        KeyError: if any column is missing from the row.
    """
    if not columns:
        raise ValueError("row key needs at least one column")
    pairs: list[tuple[str, object]] = []
    for column in columns:
        if column not in row:
            raise KeyError(f"cannot build row key: column {column!r} missing")
        pairs.append((column, row[column]))
    return tuple(pairs)
