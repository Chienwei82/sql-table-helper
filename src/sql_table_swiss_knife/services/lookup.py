"""Foreign-key lookup: searchable pickers instead of typing raw identifiers (FR-4.4).

A foreign-key cell holds an identifier, and an identifier means nothing to a human —
``RegionId = 17`` tells you nothing, ``Country = DE`` does. So opening a foreign-key cell
offers a *list* of the referenced rows, showing the key plus a best-guess description
column, filtered by whatever the user types.

The description column is a guess, honestly labelled as one. Tables do not say which of
their columns is "the" human-readable one, so :func:`description_column` picks by a ranked
list of conventional names and falls back to the first non-key text column; the picker
shows the column's name in its header, so the user always knows what they are reading.

The search happens **server-side** through the ordinary paged fetch: the referenced table
may be far larger than one page, and filtering rows that were never loaded finds nothing.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from ..domain.catalog import Column, ForeignKey, Table
from ..domain.rows import FilterOp, Row, RowFilter, RowKey
from ..providers.dialect import SqlDialect
from .catalog import CatalogService
from .connection import ConnectionService
from .data import DataService

__all__ = [
    "LOOKUP_LIMIT",
    "LookupChoice",
    "LookupResult",
    "description_column",
    "lookup_predicates",
]

#: Rows offered before the user narrows the search: enough to recognise a value, few
#: enough that the modal stays readable.
LOOKUP_LIMIT = 200

#: Column names that conventionally hold the human-readable value, best first. Matched
#: case-insensitively with separators removed, so ``display_name`` and ``DisplayName``
#: both match ``displayname``.
_DESCRIPTION_NAMES: tuple[str, ...] = (
    "name",
    "fullname",
    "displayname",
    "title",
    "label",
    "description",
    "caption",
    "text",
)

#: Types that can hold a readable label. A numeric key is never a description.
_TEXT_TYPES = frozenset({"char", "varchar", "nchar", "nvarchar", "text", "ntext", "sysname"})


def description_column(table: Table, key_columns: Sequence[str]) -> Column | None:
    """Best guess at the referenced table's human-readable column.

    Preference order: a non-key column whose name matches one of the conventional
    ``_DESCRIPTION_NAMES``, then the first non-key text column, then ``None`` — in which
    case the picker shows keys only, which is honest rather than misleading.
    """
    keys = set(key_columns)
    candidates = [
        column
        for column in table.columns
        if column.name not in keys and not column.is_computed and not column.is_rowversion
    ]
    if not candidates:
        return None
    for wanted in _DESCRIPTION_NAMES:
        for column in candidates:
            if _normalized(column.name) == wanted:
                return column
    for column in candidates:
        if column.data_type.lower() in _TEXT_TYPES:
            return column
    return None


def _normalized(name: str) -> str:
    """Column name with separators removed and case folded, for the guess above."""
    return "".join(char for char in name.lower() if char.isalnum())


def lookup_predicates(
    table: Table,
    key_columns: Sequence[str],
    description: Column | None,
    term: str,
    dialect: SqlDialect,
) -> tuple[RowFilter, ...]:
    """Server-side search predicates for the picker's search box.

    Typing part of a name has to find the row, so the search covers the description
    column and every text key column with ``LIKE``. ``RowFilter`` has no OR, and the
    alternatives here are a *disjunction* ("key matches **or** name matches"), so this
    searches the description column when the table has one and the key columns otherwise
    rather than emitting a predicate combination that would narrow to nothing.
    """
    term = term.strip()
    if not term:
        return ()
    pattern = f"%{dialect.escape_like(term)}%"
    if description is not None and description.data_type.lower() in _TEXT_TYPES:
        return (RowFilter(column=description.name, operator=FilterOp.LIKE, value=pattern),)
    predicates = tuple(
        RowFilter(column=name, operator=FilterOp.LIKE, value=pattern)
        for name in key_columns
        if (column := table.column_or_none(name)) is not None
        and column.data_type.lower() in _TEXT_TYPES
    )
    if predicates:
        return predicates
    # A numeric key: equality is the only sensible search.
    return tuple(RowFilter(column=name, operator=FilterOp.EQ, value=term) for name in key_columns)


@dataclass(frozen=True, slots=True)
class LookupChoice:
    """One selectable referenced row: its key plus a display label."""

    key: RowKey
    label: str
    values: Row

    def matches(self, term: str) -> bool:
        """Client-side filter, used to re-filter an already fetched page."""
        needle = term.strip().lower()
        if not needle:
            return True
        return needle in self.label.lower() or any(
            needle in str(value).lower() for _, value in self.key
        )


@dataclass(frozen=True, slots=True)
class LookupResult:
    """What the picker renders: the choices plus how they are described."""

    choices: tuple[LookupChoice, ...]
    key_columns: tuple[str, ...]
    description_name: str | None
    search: str = ""
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.choices

    def __len__(self) -> int:
        return len(self.choices)

    def header(self) -> str:
        """Label of the description column, or the key columns when there is none."""
        return (
            self.description_name
            if self.description_name is not None
            else " / ".join(self.key_columns)
        )

    def key_label(self, choice: LookupChoice) -> str:
        """The key columns of one choice, as one readable cell."""
        return " / ".join(_format(value) for _, value in choice.key)


class LookupService:
    """Fetches the candidate list a foreign-key picker shows."""

    def __init__(self, connection: ConnectionService, catalog: CatalogService) -> None:
        self._connection = connection
        self._catalog = catalog
        self._data = DataService(connection)

    async def search(
        self, fk: ForeignKey, term: str = "", *, limit: int = LOOKUP_LIMIT
    ) -> LookupResult:
        """The searchable candidate list for ``fk``.

        Reads the referenced table's metadata (cached), works out the description column,
        then fetches one server-side-filtered page. A reference without a usable key still
        works: the first column is used as the display key.
        """
        dialect = self._connection.provider().dialect
        target = await self._catalog.get_table(fk.referenced_schema, fk.referenced_table)
        keys = list(fk.referenced_columns) or list(target.identity_columns)
        if not keys:
            keys = [target.columns[0].name] if target.columns else []
        description = description_column(target, keys)
        window = await self._data.fetch(
            target, limit=limit, filters=lookup_predicates(target, keys, description, term, dialect)
        )
        return LookupResult(
            choices=tuple(_to_choice(row, keys, description) for row in window.rows),
            key_columns=tuple(keys),
            description_name=description.name if description is not None else None,
            search=term,
            truncated=window.has_more,
        )


def _to_choice(row: Row, keys: Sequence[str], description: Column | None) -> LookupChoice:
    key: RowKey = tuple((name, row.get(name)) for name in keys)
    label = _format(row.get(description.name)) if description is not None else ""
    if not label:
        label = " / ".join(_format(value) for _, value in key)
    return LookupChoice(key=key, label=label, values=row)


def _format(value: object) -> str:
    """One readable cell: NULL spelled out, binary abbreviated, everything else as text."""
    if value is None:
        return "NULL"
    if isinstance(value, bytes | bytearray | memoryview):
        return "0x" + bytes(value)[:8].hex()
    return str(value)
