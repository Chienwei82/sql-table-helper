"""Dialect-agnostic DML/SELECT builders producing ``SqlStatement`` objects (DESIGN §6).

The parametrized and literal renderings are composed from the *same* value lists and the
same ``SqlDialect`` statement-shape methods, so preview and execution can never diverge
(FR-5.4). Placeholder indices are allocated left-to-right: SET/VALUES columns first, then
WHERE conditions — identical order in both renderings.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..domain.catalog import Column, Table
from ..domain.changes import ChangeKind, PendingChange
from ..domain.rows import FetchSpec, RowKey
from .dialect import SqlDialect, SqlParam, SqlStatement

__all__ = [
    "SelectStatement",
    "build_delete",
    "build_insert",
    "build_select",
    "build_statements",
    "build_update",
    "sort_for_apply",
]

_KIND_ORDER = {ChangeKind.DELETE: 0, ChangeKind.UPDATE: 1, ChangeKind.INSERT: 2}


@dataclass(frozen=True, slots=True)
class SelectStatement:
    """A generated SELECT with its bound parameters (fetch_rows input)."""

    sql: str
    params: tuple[SqlParam, ...]


def sort_for_apply(changes: Iterable[PendingChange]) -> list[PendingChange]:
    """Stable DELETE → UPDATE → INSERT ordering for FK-friendly execution (DESIGN §6)."""
    return sorted(changes, key=lambda change: _KIND_ORDER[change.kind])


def _guard_server_managed(table: Table, name: str, *, context: str) -> Column:
    try:
        column = table.column(name)
    except KeyError as exc:
        raise ValueError(f"{context}: {exc.args[0]}") from exc
    if column.is_computed:
        raise ValueError(f"{context}: computed column {name!r} cannot be written")
    if column.is_rowversion:
        raise ValueError(f"{context}: rowversion column {name!r} cannot be written")
    if column.is_identity:
        raise ValueError(f"{context}: identity column {name!r} cannot be written")
    return column


def _require_target(change: PendingChange, table: Table) -> None:
    if change.table != table.ref:
        raise ValueError(f"change targets {change.table}, expected {table.ref}")


def _key_conditions(table: Table, key: RowKey) -> list[tuple[str, object]]:
    conditions: list[tuple[str, object]] = []
    for name, value in key:
        try:
            table.column(name)
        except KeyError as exc:
            raise ValueError(f"row key column not found: {exc.args[0]}") from exc
        conditions.append((name, value))
    if not conditions:
        raise ValueError("row key needs at least one column")
    return conditions


def build_insert(
    dialect: SqlDialect,
    table: Table,
    values: Mapping[str, object],
    *,
    row_key: RowKey | None = None,
) -> SqlStatement:
    """Build an INSERT for ``values`` (server-managed columns must be omitted)."""
    if not values:
        raise ValueError("INSERT needs at least one column")
    names = list(values.keys())
    for name in names:
        _guard_server_managed(table, name, context="INSERT")
    params = tuple(
        SqlParam(dialect.placeholder(index), values[name], name) for index, name in enumerate(names)
    )
    rendered_params = [param.name for param in params]
    rendered_literals = [dialect.literal(values[name]) for name in names]
    # With a rowversion column, fetch the generated PK back (DESIGN §6, OUTPUT clause).
    output_columns = (
        table.primary_key.columns
        if table.rowversion_column is not None and table.primary_key is not None
        else ()
    )
    sql_parametrized = dialect.insert_sql(
        table.schema, table.name, names, rendered_params, output_columns
    )
    sql_literal = dialect.insert_sql(
        table.schema, table.name, names, rendered_literals, output_columns
    )
    return SqlStatement(
        kind=ChangeKind.INSERT,
        table=str(table.ref),
        sql_parametrized=sql_parametrized,
        params=params,
        sql_literal=sql_literal,
        sql_script=sql_literal + ";",
        row_key=row_key,
    )


def build_update(dialect: SqlDialect, table: Table, change: PendingChange) -> SqlStatement:
    """Build an UPDATE for the changed columns, scoped by row identity (+ rowversion)."""
    _require_target(change, table)
    if change.kind is not ChangeKind.UPDATE:
        raise ValueError(f"build_update needs an UPDATE change, got {change.kind.value}")
    if change.key is None or change.before is None or change.after is None:
        raise ValueError("UPDATE change is incomplete")
    changed = change.changed_columns
    if not changed:
        raise ValueError("UPDATE has no changed columns")
    params: list[SqlParam] = []
    assignments_param: list[tuple[str, str]] = []
    assignments_literal: list[tuple[str, str]] = []
    for index, name in enumerate(changed):
        _guard_server_managed(table, name, context="UPDATE")
        value = change.after[name]
        placeholder = dialect.placeholder(index)
        params.append(SqlParam(placeholder, value, name))
        assignments_param.append((name, placeholder))
        assignments_literal.append((name, dialect.literal(value)))
    conditions = _key_conditions(table, change.key)
    rowversion = table.rowversion_column
    if rowversion is not None and rowversion.name in change.before:
        # Optimistic-concurrency guard: old rowversion must still match (FR-7.8).
        conditions.append((rowversion.name, change.before[rowversion.name]))
    conditions_param: list[tuple[str, str]] = []
    conditions_literal: list[tuple[str, str]] = []
    for offset, (name, value) in enumerate(conditions):
        index = len(changed) + offset
        placeholder = dialect.placeholder(index)
        params.append(SqlParam(placeholder, value, name))
        conditions_param.append((name, placeholder))
        conditions_literal.append((name, dialect.literal(value)))
    sql_parametrized = dialect.update_sql(
        table.schema, table.name, assignments_param, conditions_param
    )
    sql_literal = dialect.update_sql(
        table.schema, table.name, assignments_literal, conditions_literal
    )
    return SqlStatement(
        kind=ChangeKind.UPDATE,
        table=str(table.ref),
        sql_parametrized=sql_parametrized,
        params=tuple(params),
        sql_literal=sql_literal,
        sql_script=sql_literal + ";",
        row_key=change.key,
    )


def build_delete(dialect: SqlDialect, table: Table, change: PendingChange) -> SqlStatement:
    """Build a DELETE scoped by row identity (+ rowversion guard when present)."""
    _require_target(change, table)
    if change.kind is not ChangeKind.DELETE:
        raise ValueError(f"build_delete needs a DELETE change, got {change.kind.value}")
    if change.key is None or change.before is None:
        raise ValueError("DELETE change is incomplete")
    conditions = _key_conditions(table, change.key)
    rowversion = table.rowversion_column
    if rowversion is not None and rowversion.name in change.before:
        conditions.append((rowversion.name, change.before[rowversion.name]))
    params = tuple(
        SqlParam(dialect.placeholder(index), value, name)
        for index, (name, value) in enumerate(conditions)
    )
    conditions_param = [
        (name, param.name) for (name, _), param in zip(conditions, params, strict=True)
    ]
    conditions_literal = [(name, dialect.literal(value)) for name, value in conditions]
    sql_parametrized = dialect.delete_sql(table.schema, table.name, conditions_param)
    sql_literal = dialect.delete_sql(table.schema, table.name, conditions_literal)
    return SqlStatement(
        kind=ChangeKind.DELETE,
        table=str(table.ref),
        sql_parametrized=sql_parametrized,
        params=params,
        sql_literal=sql_literal,
        sql_script=sql_literal + ";",
        row_key=change.key,
    )


def build_select(dialect: SqlDialect, table: Table, spec: FetchSpec) -> SelectStatement:
    """Build a paged, sortable, filterable SELECT over all columns of ``table``."""
    predicates: list[str] = []
    params: list[SqlParam] = []
    index = 0
    for row_filter in spec.filters:
        try:
            table.column(row_filter.column)
        except KeyError as exc:
            raise ValueError(f"{exc.args[0]}") from exc
        if row_filter.operator.needs_value:
            placeholder = dialect.placeholder(index)
            index += 1
            params.append(SqlParam(placeholder, row_filter.value, row_filter.column))
            predicates.append(
                dialect.predicate_sql(row_filter.column, row_filter.operator, placeholder)
            )
        else:
            predicates.append(dialect.predicate_sql(row_filter.column, row_filter.operator))
    for sort_key in spec.sort:
        try:
            table.column(sort_key.column)
        except KeyError as exc:
            raise ValueError(f"{exc.args[0]}") from exc
    orders = [(sort_key.column, sort_key.descending) for sort_key in spec.sort]
    sql = dialect.select_rows_sql(
        table.schema,
        table.name,
        [column.name for column in table.columns],
        predicates,
        orders,
        spec.limit,
        spec.offset,
    )
    return SelectStatement(sql=sql, params=tuple(params))


def build_statements(
    dialect: SqlDialect,
    table: Table,
    changes: Iterable[PendingChange],
) -> list[SqlStatement]:
    """Build statements for staged changes in their given order (use sort_for_apply
    first when executing against the database)."""
    statements: list[SqlStatement] = []
    for change in changes:
        match change.kind:
            case ChangeKind.INSERT:
                if change.after is None:
                    raise ValueError("INSERT change is incomplete")
                statements.append(build_insert(dialect, table, change.after))
            case ChangeKind.UPDATE:
                statements.append(build_update(dialect, table, change))
            case ChangeKind.DELETE:
                statements.append(build_delete(dialect, table, change))
    return statements
