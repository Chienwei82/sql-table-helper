"""Data service: paged row fetching for the grid (FR-3.8, S-8).

The grid never asks a provider for rows directly; it asks this service, which owns the
page size, the deterministic order (identity columns when the table has a usable key) and
the *append* semantics behind "fetch more". Keeping the paging state in one place means the
status bar, the grid and the tests all agree on what "rows 1-2000 of 12345" means.

Only reads here. Staging (FR-7) and Apply (M6) belong to ``services/changes.py``, which does
not exist yet — this milestone is deliberately read-only.
"""

from dataclasses import dataclass, field, replace

from ..domain.catalog import Table
from ..domain.rows import FetchSpec, Row, RowFilter, RowKey, RowPage, SortKey
from .connection import ConnectionService

__all__ = [
    "DEFAULT_LIMIT",
    "DataService",
    "RowWindow",
    "status_text",
]

#: Default page size (S-8 / OQ-7: 1000 rows per page).
DEFAULT_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class RowWindow:
    """The rows currently loaded for one table, plus the paging facts to display them.

    Immutable: ``fetch_more`` returns a new window, which keeps the grid's "display =
    what the service last returned" contract simple and makes the state easy to assert
    on in tests.
    """

    table: Table
    rows: tuple[Row, ...]
    limit: int
    has_more: bool
    total_row_count: int | None = None
    sort: tuple[SortKey, ...] = field(default_factory=tuple)
    #: The server-side filters this window was fetched with, so the grid and the status
    #: bar can say *why* it is showing these rows (FR-3.3).
    filters: tuple[RowFilter, ...] = field(default_factory=tuple)
    #: Keyset cursor for the next page: the identity of the last row fetched.
    after_key: RowKey | None = None

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def first_ordinal(self) -> int:
        """1-based index of the first loaded row (0 when empty)."""
        return 1 if self.rows else 0

    @property
    def last_ordinal(self) -> int:
        """1-based index of the last loaded row."""
        return len(self.rows)

    @property
    def is_empty(self) -> bool:
        return not self.rows


def default_sort(table: Table) -> tuple[SortKey, ...]:
    """Deterministic order: identity columns ascending (FR-3.8).

    Tables with no usable key (S-4) have no identity, so they sort on the first column —
    still deterministic for the rows that are fetched, and the table is read-only anyway.
    """
    identity = table.identity_columns
    if identity:
        return tuple(SortKey(column=name) for name in identity)
    return (SortKey(column=table.columns[0].name),)


def status_text(window: RowWindow) -> str:
    """The FR-3.8 status-bar wording: ``rows 1-1000 • more?``."""
    if window.is_empty:
        return "no rows"
    span = f"rows {window.first_ordinal}-{window.last_ordinal}"
    if window.has_more:
        span += " • more?"
    elif window.total_row_count is not None:
        span += f" of {window.total_row_count}"
    return span


class DataService:
    """Paged row reads against the active connection."""

    def __init__(self, connection: ConnectionService) -> None:
        self._connection = connection

    async def fetch(
        self,
        table: Table,
        *,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        sort: tuple[SortKey, ...] | None = None,
        filters: tuple[RowFilter, ...] = (),
        after_key: RowKey | None = None,
    ) -> RowWindow:
        """Fetch one page of ``table``'s rows.

        Sorting and filtering are *requests to the server*, not in-memory operations:
        the rows that were never loaded cannot be filtered locally, and the whole table is
        never loaded (S-8). ``after_key`` switches the provider to keyset paging when the
        order is the table's identity columns.

        Raises:
            RuntimeError: when no connection is open.
            ValueError: for an offset beyond a non-negative range (from ``FetchSpec``).
        """
        spec = FetchSpec(
            limit=limit,
            offset=offset,
            sort=default_sort(table) if sort is None else sort,
            filters=filters,
            after_key=after_key,
        )
        provider = self._connection.provider()
        page: RowPage = await provider.fetch_rows(self._connection.connection(), table, spec)
        return RowWindow(
            table=table,
            rows=page.rows,
            limit=page.limit,
            has_more=page.has_more,
            total_row_count=page.total_row_count,
            sort=spec.sort,
            filters=filters,
            after_key=after_key,
        )

    async def fetch_more(self, window: RowWindow) -> RowWindow:
        """Fetch the next page and append it to ``window`` (the "fetch more" action).

        The previous page's sort order is reused so the appended rows line up with what the
        user is already looking at; asking for more when there is nothing more returns an
        equivalent window instead of raising.
        """
        if not window.has_more:
            return window
        offset = window.count
        grown = await self.fetch(
            window.table,
            limit=max(window.limit, DEFAULT_LIMIT),
            offset=offset,
            sort=window.sort,
        )
        return replace(
            window,
            rows=(*window.rows, *grown.rows),
            has_more=grown.has_more,
            total_row_count=grown.total_row_count,
            limit=grown.limit,
        )
