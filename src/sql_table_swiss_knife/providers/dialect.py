"""SQL dialect protocol and statement value objects (DBMS-specific seam)."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.changes import ChangeKind
from ..domain.rows import FilterOp, RowKey

__all__ = [
    "ApplyOptions",
    "Condition",
    "ErrorFacts",
    "SqlDialect",
    "SqlParam",
    "SqlScript",
    "SqlStatement",
]


@dataclass(frozen=True, slots=True)
class ErrorFacts:
    """What a driver error message says about the failure, in DBMS-neutral terms.

    Produced by :meth:`SqlDialect.inspect_error`, which is the only place that knows a
    vendor's error wording. ``kind`` is a coarse class the UI can branch on, ``hint`` the
    wording to show instead of raw vendor text, and ``constraint``/``column`` whatever the
    message explicitly named.
    """

    kind: str = "error"
    hint: str = ""
    constraint: str | None = None
    column: str | None = None


@dataclass(frozen=True, slots=True)
class Condition:
    """One WHERE element: a column, its rendered value, and whether it is NULL.

    ``is_null`` exists because ``WHERE [col] = NULL`` is never true in SQL: a NULL
    original value has to be compared with ``IS NULL`` for the optimistic-concurrency
    guards to be correct.
    """

    column: str
    rendered: str
    is_null: bool = False


@dataclass(frozen=True, slots=True)
class ApplyOptions:
    """Knobs for statement generation and execution (DESIGN §6, FR-7.8).

    * ``identity_insert`` — allow explicit values for identity columns. This requires
      ``SET IDENTITY_INSERT … ON`` around the statements and is opt-in behind a
      warning, because writing explicit identity values desynchronizes the identity
      counter from the rows actually present.
    * ``compare_original_values`` — for tables *without* a rowversion column, add the
      original value of every unchanged column to the WHERE clause, so a concurrent
      modification by someone else is detected as 0 rows affected.
    * ``schema_locking`` — emit ``SET DEADLOCK_PRIORITY LOW`` / a holdlock hint so a
      long Apply does not escalate into a deadlock victim.
    """

    identity_insert: bool = False
    compare_original_values: bool = False
    schema_locking: bool = False


@dataclass(frozen=True, slots=True)
class SqlParam:
    """One bound parameter: placeholder, Python value, and its column."""

    name: str  # e.g. "@p0"
    value: object
    column: str


@dataclass(frozen=True, slots=True)
class SqlScript:
    """A complete, copy-ready script wrapping one or more statements (FR-5.3).

    ``body`` is the literal form of every statement in apply order, already terminated.
    ``identity_insert`` records that the script needs ``SET IDENTITY_INSERT … ON`` around
    the statements, which the dialect decides — it is the only thing that knows whether the
    DBMS has such a concept at all.
    """

    body: tuple[str, ...]
    identity_insert: tuple[str, str] | None = None

    @property
    def statements(self) -> tuple[str, ...]:
        """The statements without the transaction wrapper."""
        return self.body


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

    def escape_like(self, text: str) -> str:
        """Escape LIKE metacharacters in ``text`` so it matches literally.

        The escaping syntax is dialect-specific (SQL Server uses brackets, others use a
        backslash), so a service that builds a ``LIKE`` pattern must ask the dialect
        rather than hard-code one dialect's rules.
        """
        raise NotImplementedError

    def literal(self, value: object) -> str:
        """Render a *value* as a self-contained literal — display/copy only (FR-5.2).

        This is the single seam through which every value reaches SQL text, so escaping
        lives here and nowhere else (S-6: user text never becomes SQL text).
        """
        ...

    def select_by_key_sql(
        self,
        schema: str,
        table: str,
        columns: Sequence[str],
        conditions: Sequence[Condition],
    ) -> str:
        """SELECT of the rows matching ``conditions`` (the "generate SELECT" action)."""
        ...

    def insert_rows_sql(
        self,
        schema: str,
        table: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[str]],
    ) -> str:
        """One multi-row INSERT; empty ``rows`` yields an empty string."""
        ...

    def merge_sql(
        self,
        schema: str,
        table: str,
        key_columns: Sequence[str],
        columns: Sequence[str],
        rows: Sequence[Sequence[str]],
    ) -> str:
        """Upsert ``rows`` matched on ``key_columns`` (the "generate MERGE" action)."""
        ...

    def script_sql(self, script: SqlScript) -> str:
        """Wrap statements in the dialect's transaction/error-handling envelope (FR-5.3).

        The contract every implementation must honour: a statement that fails leaves
        **nothing** applied — a transaction that rolls back, or a dialect whose scripts are
        explicitly not all-or-nothing (documented by the implementation).
        """
        ...

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
        conditions: Sequence[Condition],
    ) -> str: ...

    def delete_sql(
        self,
        schema: str,
        table: str,
        conditions: Sequence[Condition],
    ) -> str: ...

    def identity_insert_sql(self, schema: str, table: str, *, enabled: bool) -> str:
        """``SET IDENTITY_INSERT [s].[t] ON|OFF`` (empty string for other dialects)."""
        ...

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
        after_key: Sequence[tuple[str, str]] = (),
    ) -> str:
        """Paged SELECT; ``after_key`` enables keyset paging when it is non-empty."""
        ...

    def keyset_predicate_sql(self, key_columns: Sequence[str], placeholders: Sequence[str]) -> str:
        """A lexicographic ``>`` comparison over the key columns, for keyset paging."""
        ...

    def inspect_error(self, message: str) -> ErrorFacts:
        """Classify a driver error message into DBMS-neutral :class:`ErrorFacts`.

        Which messages mean "duplicate key" is vendor wording, so it belongs here rather
        than in a service that would otherwise have to hard-code one DBMS's English.
        """
        raise NotImplementedError

    def begin_transaction(self) -> str: ...

    def commit_transaction(self) -> str: ...

    def rollback_transaction(self) -> str: ...
