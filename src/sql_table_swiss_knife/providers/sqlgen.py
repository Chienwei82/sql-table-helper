"""Dialect-agnostic DML/SELECT builders producing ``SqlStatement`` objects (DESIGN §6).

The parametrized and literal renderings are composed from the *same* value lists and the
same ``SqlDialect`` statement-shape methods, so preview and execution can never diverge
(FR-5.4). Placeholder indices are allocated left-to-right: SET/VALUES columns first, then
WHERE conditions — identical order in both renderings.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ..domain.catalog import Column, Table
from ..domain.changes import ChangeKind, PendingChange
from ..domain.rows import FetchSpec, RowKey
from .dialect import ApplyOptions, Condition, SqlDialect, SqlParam, SqlScript, SqlStatement

__all__ = [
    "SelectStatement",
    "build_delete",
    "build_insert",
    "build_merge",
    "build_row_select",
    "build_row_update",
    "build_select",
    "build_statements",
    "build_table_insert",
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


def _guard_server_managed(
    table: Table, name: str, *, context: str, allow_identity: bool = False
) -> Column:
    try:
        column = table.column(name)
    except KeyError as exc:
        raise ValueError(f"{context}: {exc.args[0]}") from exc
    if column.is_computed:
        raise ValueError(f"{context}: computed column {name!r} cannot be written")
    if column.is_rowversion:
        raise ValueError(f"{context}: rowversion column {name!r} cannot be written")
    if column.is_identity and not allow_identity:
        raise ValueError(f"{context}: identity column {name!r} cannot be written")
    return column


def _require_target(change: PendingChange, table: Table) -> None:
    if change.table != table.ref:
        raise ValueError(f"change targets {change.table}, expected {table.ref}")


def _condition(column: str, value: object, placeholder: str) -> Condition:
    """Build a WHERE condition, using ``IS NULL`` for a NULL original value.

    ``WHERE [col] = NULL`` is never true in SQL, so a NULL must be compared with
    ``IS NULL``; this is exactly the case the "compare all original values"
    optimistic-concurrency mode has to get right (FR-7.8).
    """
    return Condition(column=column, rendered=placeholder, is_null=value is None)


def _bound_conditions(
    dialect: SqlDialect,
    conditions: Sequence[tuple[str, object]],
    *,
    first_index: int,
) -> tuple[list[Condition], list[Condition], list[SqlParam]]:
    """Split WHERE conditions into both renderings, plus the values to bind.

    A NULL original value is compared with ``IS NULL``, which contains no placeholder —
    so it must not contribute a bound parameter either. A driver rejects a parameter
    count that does not match the statement's markers, and the optimistic-concurrency
    guards hit this on every nullable column of a table without a rowversion, which is
    the common case rather than an edge case.

    Placeholder indices are allocated left-to-right over the conditions that actually
    bind, so the numbering stays contiguous and matches ``SqlStatement.param_values``.
    """
    conditions_param: list[Condition] = []
    conditions_literal: list[Condition] = []
    params: list[SqlParam] = []
    index = first_index
    for name, value in conditions:
        if value is None:
            conditions_param.append(Condition(name, "", True))
            conditions_literal.append(Condition(name, dialect.literal(value), True))
            continue
        placeholder = dialect.placeholder(index)
        index += 1
        params.append(SqlParam(placeholder, value, name))
        conditions_param.append(Condition(name, placeholder, False))
        conditions_literal.append(Condition(name, dialect.literal(value), False))
    return conditions_param, conditions_literal, params


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


def _compare_original_columns(
    table: Table, change: PendingChange, changed: Sequence[str]
) -> list[tuple[str, object]]:
    """Original values of the *unchanged* columns, for the compare-all mode.

    Three kinds of column are skipped, each for a concrete reason:

    * columns this statement writes — re-checking the value it is about to replace could
      never match, so the guard would break every UPDATE;
    * the row identity columns — they are already in the WHERE clause, so repeating them
      would emit a duplicated condition;
    * computed columns — their value is the server's, not the editor's, and comparing it
      would make the guard fail whenever a computed expression is not deterministic.

    The rowversion column is skipped too, because it is added separately as the preferred
    guard when the table has one.
    """
    before = change.before or {}
    rowversion = table.rowversion_column
    identity = set(table.identity_columns)
    return [
        (column.name, before[column.name])
        for column in table.columns
        if column.name in before
        and column.name not in changed
        and not column.is_computed
        and column.name not in identity
        and (rowversion is None or column.name != rowversion.name)
    ]


def build_insert(
    dialect: SqlDialect,
    table: Table,
    values: Mapping[str, object],
    *,
    row_key: RowKey | None = None,
    identity_insert: bool = False,
) -> SqlStatement:
    """Build an INSERT for ``values`` (server-managed columns must be omitted).

    ``identity_insert`` is the explicitly-warned opt-in that allows writing an identity
    column explicitly; without it an identity column is rejected (S-3).
    """
    if not values:
        raise ValueError("INSERT needs at least one column")
    names = list(values.keys())
    for name in names:
        _guard_server_managed(table, name, context="INSERT", allow_identity=identity_insert)
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


def build_update(
    dialect: SqlDialect,
    table: Table,
    change: PendingChange,
    *,
    compare_original: bool = False,
) -> SqlStatement:
    """Build an UPDATE for the changed columns, scoped by row identity (+ rowversion).

    ``compare_original`` adds the original value of every unchanged column to the WHERE
    clause. That is the fallback optimistic-concurrency guard for tables without a
    rowversion column: if anyone else changed a column we did not touch, the statement
    matches 0 rows and the conflict is reported (FR-7.8).
    """
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
    elif compare_original:
        conditions.extend(_compare_original_columns(table, change, changed))
    conditions_param, conditions_literal, condition_params = _bound_conditions(
        dialect, conditions, first_index=len(params)
    )
    params.extend(condition_params)
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


def build_delete(
    dialect: SqlDialect,
    table: Table,
    change: PendingChange,
    *,
    compare_original: bool = False,
) -> SqlStatement:
    """Build a DELETE scoped by row identity (+ rowversion guard when present).

    ``compare_original`` adds every original column value to the WHERE clause, so a row
    somebody else has already changed is detected as a conflict instead of being deleted
    on stale information (FR-7.8).
    """
    _require_target(change, table)
    if change.kind is not ChangeKind.DELETE:
        raise ValueError(f"build_delete needs a DELETE change, got {change.kind.value}")
    if change.key is None or change.before is None:
        raise ValueError("DELETE change is incomplete")
    conditions = _key_conditions(table, change.key)
    rowversion = table.rowversion_column
    if rowversion is not None and rowversion.name in change.before:
        conditions.append((rowversion.name, change.before[rowversion.name]))
    elif compare_original:
        conditions.extend(_compare_original_columns(table, change, ()))
    conditions_param, conditions_literal, params = _bound_conditions(
        dialect, conditions, first_index=0
    )
    sql_parametrized = dialect.delete_sql(table.schema, table.name, conditions_param)
    sql_literal = dialect.delete_sql(table.schema, table.name, conditions_literal)
    return SqlStatement(
        kind=ChangeKind.DELETE,
        table=str(table.ref),
        sql_parametrized=sql_parametrized,
        params=tuple(params),
        sql_literal=sql_literal,
        sql_script=sql_literal + ";",
        row_key=change.key,
    )


def build_select(dialect: SqlDialect, table: Table, spec: FetchSpec) -> SelectStatement:
    """Build a paged, sortable, filterable SELECT over all columns of ``table``.

    When ``spec.after_key`` is set and the order is the table's ascending identity
    columns, the statement switches from ``OFFSET`` to keyset paging: a
    ``(keys) > (@after)`` predicate and ``OFFSET 0``, which stays fast on deep pages and
    does not skip or repeat rows when the table is being written to while paging.
    """
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
    after_key: list[tuple[str, str]] = []
    if spec.after_key and _keyset_usable(table, spec):
        for name, value in spec.after_key:
            try:
                table.column(name)
            except KeyError as exc:
                raise ValueError(f"{exc.args[0]}") from exc
            placeholder = dialect.placeholder(index)
            index += 1
            params.append(SqlParam(placeholder, value, name))
            after_key.append((name, placeholder))
    columns = [column.name for column in table.columns]
    sql = dialect.select_rows_sql(
        table.schema,
        table.name,
        columns,
        predicates,
        orders,
        spec.limit,
        spec.offset,
        tuple(after_key),
    )
    return SelectStatement(sql=sql, params=tuple(params))


def _keyset_usable(table: Table, spec: FetchSpec) -> bool:
    """Keyset paging needs an ascending order over exactly the identity columns.

    A user-chosen sort (or a descending one) can reorder rows arbitrarily, so the
    "after" row's key no longer identifies a position; those fall back to OFFSET.
    """
    identity = table.identity_columns
    if not identity:
        return False
    if len(spec.after_key or ()) != len(identity):
        return False
    if [name for name, _ in spec.after_key or ()] != list(identity):
        return False
    return tuple((key.column, key.descending) for key in spec.sort) == tuple(
        (name, False) for name in identity
    )


def build_statements(
    dialect: SqlDialect,
    table: Table,
    changes: Iterable[PendingChange],
    *,
    options: ApplyOptions | None = None,
) -> list[SqlStatement]:
    """Build statements for staged changes in their given order (use sort_for_apply
    first when executing against the database)."""
    settings = options if options is not None else ApplyOptions()
    statements: list[SqlStatement] = []
    for change in changes:
        match change.kind:
            case ChangeKind.INSERT:
                if change.after is None:
                    raise ValueError("INSERT change is incomplete")
                statements.append(
                    build_insert(
                        dialect,
                        table,
                        change.after,
                        row_key=change.key,
                        identity_insert=settings.identity_insert,
                    )
                )
            case ChangeKind.UPDATE:
                statements.append(
                    build_update(
                        dialect,
                        table,
                        change,
                        compare_original=settings.compare_original_values,
                    )
                )
            case ChangeKind.DELETE:
                statements.append(
                    build_delete(
                        dialect,
                        table,
                        change,
                        compare_original=settings.compare_original_values,
                    )
                )
    return statements


# -- "generate SQL for…" builders (M6 / FR-5) -------------------------------
#
# These serve the *ad-hoc* generation actions, not Apply. They take values rather than a
# ChangeSet, always render **literal** text (the point is to copy and paste it), and never
# execute anything — which is what makes the panel safe to explore (S-1, S-5).


def _writable_column_names(
    table: Table, values: Mapping[str, object], *, allow_identity: bool
) -> list[str]:
    """The subset of ``values`` that may legally be written, in table order.

    Server-managed columns are dropped rather than refused here: a "generate SQL for the
    whole table" action is asked for precisely to move data, and an identity or rowversion
    column has a different value in the target environment by definition. The order follows
    the table's ordinals so a generated column list always matches the physical table.
    """
    names: list[str] = []
    for column in table.columns:
        if column.name not in values:
            continue
        if column.is_computed or column.is_rowversion:
            continue
        if column.is_identity and not allow_identity:
            continue
        names.append(column.name)
    return names


def build_row_select(
    dialect: SqlDialect,
    table: Table,
    key: RowKey,
    columns: Sequence[str] | None = None,
) -> str:
    """``SELECT`` the row addressed by ``key`` — the "generate SELECT" action.

    Literal-only: there is nothing to bind, the statement exists to be read and pasted.
    """
    wanted = list(columns) if columns else [column.name for column in table.columns]
    for name in wanted:
        table.column(name)  # raises KeyError for an unknown column
    conditions = _literal_conditions(dialect, table, key)
    return dialect.select_by_key_sql(table.schema, table.name, wanted, conditions)


def _literal_conditions(dialect: SqlDialect, table: Table, key: RowKey) -> list[Condition]:
    """WHERE conditions for a row key, values inlined, NULL handled with ``IS NULL``.

    ``WHERE [col] = NULL`` is never true, so a NULL key part must become ``IS NULL`` — the
    same rule the generated UPDATE/DELETE already follow (see :func:`_condition`).
    """
    if not key:
        raise ValueError("row key needs at least one column")
    conditions: list[Condition] = []
    for name, value in key:
        try:
            table.column(name)
        except KeyError as exc:
            raise ValueError(f"row key column not found: {exc.args[0]}") from exc
        conditions.append(_condition(name, value, dialect.literal(value)))
    return conditions


def build_table_insert(
    dialect: SqlDialect,
    table: Table,
    rows: Sequence[Mapping[str, object]],
    *,
    identity_insert: bool = False,
) -> str:
    """A batched ``INSERT`` of ``rows`` — "insert script for all rows in this table/filter".

    This exists because moving catalog data between environments means pasting hundreds of
    rows into a query window, and a script of individual INSERTs is unreadable and slow to
    run. Columns are unioned across the rows in table order and a value a row does not have
    becomes ``NULL``; a row with no writable column at all is dropped rather than emitted
    as an all-``NULL`` row, which would insert junk instead of saying nothing.
    """
    if not rows:
        return ""
    names: list[str] = []
    for row in rows:
        for name in _writable_column_names(table, row, allow_identity=identity_insert):
            if name not in names:
                names.append(name)
    names.sort(key=lambda name: table.column(name).ordinal)
    if not names:
        return ""
    # A row with nothing writable in it would render as an all-NULL row, inserting junk
    # instead of saying nothing, so it is dropped rather than emitted.
    usable = [
        row for row in rows if _writable_column_names(table, row, allow_identity=identity_insert)
    ]
    if not usable:
        return ""
    rendered = [[dialect.literal(row.get(name)) for name in names] for row in usable]
    return dialect.insert_rows_sql(table.schema, table.name, names, rendered)


def build_row_update(
    dialect: SqlDialect,
    table: Table,
    key: RowKey,
    values: Mapping[str, object],
) -> str:
    """``UPDATE`` one row, setting exactly the columns in ``values`` (FR-5.6).

    Distinct from :func:`build_update`, which renders a *staged diff* and therefore needs
    the "before" values to know what changed. A generated statement has no before — the user
    is asking "write these values to that row" — so the column set comes from ``values``
    directly. Without this, passing the row's own values as both before and after would
    produce "no changed columns".
    """
    if not values:
        raise ValueError("UPDATE needs at least one column to set")
    # Computed and rowversion columns have no client-supplied value (S-3), and the identity
    # guard does not apply to an UPDATE, so the writable set is the only safe one.
    names = _writable_column_names(table, values, allow_identity=False)
    names = [name for name in names if name not in {key_name for key_name, _ in key}]
    if not names:
        raise ValueError("UPDATE needs at least one column to set")
    assignments = [(name, dialect.literal(values[name])) for name in names]
    conditions = _literal_conditions(dialect, table, key)
    return dialect.update_sql(table.schema, table.name, assignments, conditions)


def build_merge(
    dialect: SqlDialect,
    table: Table,
    rows: Sequence[Mapping[str, object]],
    *,
    key_columns: Sequence[str] | None = None,
    identity_insert: bool = False,
) -> str:
    """A ``MERGE`` upsert of ``rows`` matched on the key — the "generate MERGE" action.

    Requires a usable key: MERGE has no meaning without one, so a table with no primary key
    (whose rows the app already treats as read-only, S-4) is refused rather than producing a
    statement that would cross-match rows against each other.
    """
    identity = list(key_columns) if key_columns else list(table.identity_columns)
    if not identity:
        raise ValueError(
            f"MERGE needs a key: {table.ref} has no primary key, so its rows cannot be matched"
        )
    for name in identity:
        table.column(name)
    if not rows:
        return ""
    # All-or-nothing (AGENTS.md): a MERGE source row that lacks a key column would render
    # as NULL, never match the ON clause, and fall through to NOT MATCHED — an INSERT of a
    # NULL key. Refuse the batch instead of silently dropping or corrupting a row.
    missing = [index for index, row in enumerate(rows) if any(name not in row for name in identity)]
    if missing:
        listed = ", ".join(str(index + 1) for index in missing[:5])
        raise ValueError(
            f"MERGE needs every key column ({', '.join(identity)}) on every row; "
            f"row {listed} is missing one"
        )
    names: list[str] = []
    for row in rows:
        for name in _writable_column_names(table, row, allow_identity=identity_insert):
            if name not in names:
                names.append(name)
    names.sort(key=lambda name: table.column(name).ordinal)
    for name in identity:
        if name not in names:
            names.insert(0, name)
    if not names:
        return ""
    rendered = [[dialect.literal(row[name]) for name in names] for row in rows]
    return dialect.merge_sql(table.schema, table.name, identity, names, rendered)


def needs_identity_insert(
    table: Table,
    statements: Sequence[SqlStatement],
    *,
    identity_insert: bool,
) -> bool:
    """Whether ``SET IDENTITY_INSERT`` must bracket these statements.

    ``SET IDENTITY_INSERT`` is not decoration: it is a session-wide server setting that
    locks the table for every other session. It is required only when the caller opted in
    *and* there is an INSERT to run *and* the target table really has an IDENTITY column —
    on a table without one it is a runtime error ("Table does not have the identity
    property"), which would fail an Apply that the generated script shows as valid.

    Both :func:`build_script` and ``MssqlProvider.execute_changes`` ask this question, and
    they must get the same answer: the script shown to the user is the script that runs.
    Note ``Table.identity_columns`` means *row identity* (the PK), which is unrelated.
    """
    if not identity_insert:
        return False
    if not any(c.is_identity for c in table.columns):
        return False
    return any(s.kind is ChangeKind.INSERT for s in statements)


def build_script(
    dialect: SqlDialect,
    table: Table | None,
    statements: Sequence[SqlStatement | str],
    *,
    identity_insert: bool = False,
) -> str:
    """A whole change set as one copy-ready transaction script (FR-5.3).

    ``SqlStatement`` items contribute their *literal* rendering — the same statements
    Apply would run, so the script and the execution cannot drift (FR-5.4). Plain strings
    (the ad-hoc generated statements) pass through untouched.

    ``identity_insert`` only takes effect when there really is an INSERT writing an
    identity value: the ``SET IDENTITY_INSERT`` pair is not decoration, it changes server
    state visible to other sessions, so the script opens it exactly when needed.
    """
    body: list[str] = []
    for item in statements:
        if isinstance(item, str):
            text = item.rstrip().rstrip(";")
            if text:
                body.append(text)
        else:
            body.append(item.sql_literal.rstrip().rstrip(";"))
    if not body:
        return ""
    target = None
    if table is not None and needs_identity_insert(
        table,
        [s for s in statements if not isinstance(s, str)],
        identity_insert=identity_insert,
    ):
        target = (table.schema, table.name)
    return dialect.script_sql(SqlScript(body=tuple(body), identity_insert=target))
