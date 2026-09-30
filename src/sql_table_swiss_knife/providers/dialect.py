"""SQL dialect protocol and statement value objects (DBMS-specific seam)."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.changes import ChangeKind
from ..domain.rows import FilterOp, RowKey

__all__ = ["SqlDialect", "SqlParam", "SqlStatement"]


@dataclass(frozen=True, slots=True)
class SqlParam:
    """One bound parameter: placeholder, Python value, and its column."""

    name: str  # e.g. "@p0"
    value: object
    column: str


@dataclass(frozen=True, slots=True)
class SqlStatement:
    """A generated DML statement in both renderings (FR-5, DESIGN §6).

    ``sql_parametrized`` is exactly what is sent to the driver; ``sql_literal`` is the
    copy-ready form with values inlined; ``sql_script`` is the literal form terminated
    for inclusion in a transaction script. Both come from the same builder, so the
    preview can never diverge from execution (FR-5.4).
    """

    kind: ChangeKind
    table: str  # qualified display name, e.g. "dbo.Country"
    sql_parametrized: str
    params: tuple[SqlParam, ...]
    sql_literal: str
    sql_script: str
    row_key: RowKey | None = None

    @property
    def param_values(self) -> tuple[object, ...]:
        """Parameter values in placeholder order (for driver execute)."""
        return tuple(param.value for param in self.params)


@runtime_checkable
class SqlDialect(Protocol):
    """DBMS-specific SQL generation (quoting, placeholders, literals, statement shapes).

    All ``schema``/``table``/``column`` arguments are *unquoted* identifiers; the
    dialect quotes them. Rendered value strings (placeholders or literals) are passed
    into the statement-shape methods so parametrized and literal renderings share one
    composition path.
    """

    name: str

    def quote_ident(self, ident: str) -> str: ...

    def quote_qualified(self, schema: str | None, name: str) -> str: ...

    def placeholder(self, index: int) -> str: ...

    def literal(self, value: object) -> str: ...

    def insert_sql(
        self,
        schema: str,
        table: str,
        columns: Sequence[str],
        values: Sequence[str],
        output_columns: Sequence[str] = (),
    ) -> str: ...

    def update_sql(
        self,
        schema: str,
        table: str,
        assignments: Sequence[tuple[str, str]],
        conditions: Sequence[tuple[str, str]],
    ) -> str: ...

    def delete_sql(
        self,
        schema: str,
        table: str,
        conditions: Sequence[tuple[str, str]],
    ) -> str: ...

    def predicate_sql(self, column: str, op: FilterOp, placeholder: str | None = None) -> str: ...

    def select_rows_sql(
        self,
        schema: str,
        table: str,
        columns: Sequence[str],
        predicates: Sequence[str],
        orders: Sequence[tuple[str, bool]],
        limit: int,
        offset: int,
    ) -> str: ...

    def begin_transaction(self) -> str: ...

    def commit_transaction(self) -> str: ...

    def rollback_transaction(self) -> str: ...
