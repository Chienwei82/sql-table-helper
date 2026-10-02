"""Catalog service: databases, tables/views, table metadata (FR-1.5, FR-2).

The table picker needs the listing on every keystroke-free render but the full
metadata only when a table is opened, so this service keeps a per-session cache of
both. The pure helpers (:func:`filter_summaries`, :func:`group_by_schema`) do the
grouping and search work and are unit-testable without a database (NFR-5).
"""

from dataclasses import dataclass

from ..domain.catalog import Database, Table, TableSummary
from .connection import ConnectionService

__all__ = [
    "LARGE_TABLE_THRESHOLD",
    "CatalogService",
    "SchemaGroup",
    "filter_summaries",
    "group_by_schema",
    "needs_filter_prompt",
]

#: Above this many rows, opening a table offers a filter first (FR-2.5). A table bigger
#: than a person can scan by eye is exactly the one a filter helps; the prompt is a
#: courtesy, never a gate — the user can always open the whole table. Configurable via
#: ``filter_prompt_threshold`` in ``settings.toml`` (0 disables it).
LARGE_TABLE_THRESHOLD = 100


def needs_filter_prompt(row_count: int | None, *, threshold: int = LARGE_TABLE_THRESHOLD) -> bool:
    """Whether opening a table with ``row_count`` rows should offer a filter first.

    ``None`` means the count is unknown — the server's estimate is absent for some views
    and for tables whose statistics were never built — and an unknown count never
    prompts: badgering someone to filter a table that may hold three rows is worse than
    the occasional unfiltered open. A ``threshold`` of 0 (or less) disables the prompt
    entirely, which is how a user opts out.
    """
    if row_count is None or threshold <= 0:
        return False
    return row_count > threshold


@dataclass(frozen=True, slots=True)
class SchemaGroup:
    """One schema and the objects it contains, as rendered by the table tree."""

    schema: str
    tables: tuple[TableSummary, ...]

    @property
    def label(self) -> str:
        """Header text for the group, including the object count."""
        return f"{self.schema} ({len(self.tables)})"


def filter_summaries(summaries: tuple[TableSummary, ...], query: str) -> tuple[TableSummary, ...]:
    """Case-insensitive substring filter over ``schema.table`` (FR-2.1).

    An empty query returns everything unchanged, so the caller can use this on every
    keystroke without special-casing. Matching is on the full reference *and* the
    bare name, so ``dbo.order`` and ``order`` both find ``dbo.Orders``.
    """
    needle = query.strip().lower()
    if not needle:
        return summaries
    return tuple(
        summary
        for summary in summaries
        if _matches(summar := summary, needle) or needle in summar.schema.lower()
    )


def _matches(summary: TableSummary, needle: str) -> bool:
    """True when ``needle`` occurs in ``schema.table`` or in the bare name."""
    qualified = f"{summary.schema}.{summary.name}".lower()
    return needle in qualified or needle in summary.name.lower()


def group_by_schema(summaries: tuple[TableSummary, ...]) -> tuple[SchemaGroup, ...]:
    """Group objects under their schema, both levels sorted (FR-2.2).

    Empty schemas never appear, which is how "empty groups collapse" is implemented:
    filtering happens first, so a schema with no match produces no group at all.
    """
    by_schema: dict[str, list[TableSummary]] = {}
    for summary in summaries:
        by_schema.setdefault(summary.schema, []).append(summary)

    def by_name(summary: TableSummary) -> tuple[str, str]:
        """Sort objects by name, case-insensitively but stably."""
        return (summary.name.lower(), summary.name)

    return tuple(
        SchemaGroup(schema=schema, tables=tuple(sorted(items, key=by_name)))
        for schema, items in sorted(by_schema.items(), key=lambda pair: pair[0].lower())
    )


class CatalogService:
    """Read-side catalog operations against the active connection.

    Every method requires a live session; the caller checks
    :attr:`~sql_table_swiss_knife.services.connection.ConnectionService.is_connected`
    first. Results are cached per database so switching back to a previously visited
    database is instant, and the cache is dropped on reconnect so a new session never
    shows another session's metadata.
    """

    def __init__(self, connection: ConnectionService) -> None:
        self._connection = connection
        self._tables: tuple[TableSummary, ...] | None = None
        self._tables_database: str | None = None
        self._metadata: dict[tuple[str, str], Table] = {}

    async def list_databases(self) -> tuple[Database, ...]:
        """Databases visible to the login, current one flagged (FR-1.4).

        Raises:
            RuntimeError: when no connection is open.
        """
        provider = self._connection.provider()
        return tuple(await provider.list_databases(self._connection.connection()))

    async def list_tables(self, *, refresh: bool = False) -> tuple[TableSummary, ...]:
        """Tables and views of the current database, cached until ``refresh``.

        Raises:
            RuntimeError: when no connection is open.
        """
        if not refresh and self._tables is not None:
            return self._tables
        provider = self._connection.provider()
        session = self._connection.session
        summaries = tuple(await provider.list_tables(self._connection.connection()))
        if session is not None and self._tables_database != session.database:
            self._metadata.clear()  # a different database: its metadata is unrelated
        self._tables = summaries
        self._tables_database = session.database if session else None
        return summaries

    async def get_table(self, schema: str, name: str, *, refresh: bool = False) -> Table:
        """Full metadata for one table/view, cached per session (FR-6.1).

        Raises:
            RuntimeError: when no connection is open.
            MetadataError: when the object does not exist or is not visible.
        """
        key = (schema, name)
        if not refresh and key in self._metadata:
            return self._metadata[key]
        provider = self._connection.provider()
        table = await provider.get_table_metadata(self._connection.connection(), schema, name)
        self._metadata[key] = table
        return table

    def invalidate(self) -> None:
        """Drop every cached listing and metadata entry."""
        self._tables = None
        self._tables_database = None
        self._metadata.clear()
