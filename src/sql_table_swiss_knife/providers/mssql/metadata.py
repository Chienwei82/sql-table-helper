"""SQL Server catalog introspection via ``sys.*`` views (DESIGN §5.3 step 3).

Each mapper takes already-fetched plain-dict rows (as produced by
``connection.fetch_all``) and returns pure domain models. Separating row mapping from
SQL text keeps the mapping unit-testable without a server and the queries in one place.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ...domain.catalog import (
    CheckConstraint,
    Column,
    Database,
    ForeignKey,
    IncomingForeignKey,
    PrimaryKey,
    ReferentialAction,
    Table,
    TableKind,
    TableSummary,
    Trigger,
    UniqueConstraint,
)
from ...domain.identifiers import TableRef
from ..errors import MetadataError

__all__ = [
    "REFERENTIAL_ACTIONS",
    "TRIGGER_EVENTS",
    "build_table",
    "check_constraints",
    "column_from_row",
    "database_rows_sql",
    "databases_from_rows",
    "foreign_keys",
    "incoming_foreign_keys",
    "object_kind",
    "referential_action",
    "row_count",
    "table_rows_sql",
    "table_summaries",
    "temporal_flags",
    "triggers_from_rows",
    "unique_and_primary_keys",
]

FOREIGN_KEYS_SQL = """
SELECT fk.name AS constraint_name,
       fkc.constraint_column_id,
       pc.name AS column_name,
       rs.name AS referenced_schema,
       rt.name AS referenced_table,
       rc.name AS referenced_column,
       fk.delete_referential_action_desc AS on_delete,
       fk.update_referential_action_desc AS on_update
  FROM sys.foreign_keys AS fk
  JOIN sys.foreign_key_columns AS fkc ON fkc.constraint_object_id = fk.object_id
  JOIN sys.columns AS pc
    ON pc.object_id = fk.parent_object_id AND pc.column_id = fkc.parent_column_id
  JOIN sys.objects AS rt ON rt.object_id = fk.referenced_object_id
  JOIN sys.schemas AS rs ON rs.schema_id = rt.schema_id
  JOIN sys.columns AS rc
    ON rc.object_id = fk.referenced_object_id AND rc.column_id = fkc.referenced_column_id
 WHERE fk.parent_object_id = OBJECT_ID(?)
 ORDER BY fk.name, fkc.constraint_column_id
"""

INCOMING_FOREIGN_KEYS_SQL = """
SELECT fk.name AS constraint_name,
       ps.name AS referencing_schema,
       pt.name AS referencing_table,
       fkc.constraint_column_id,
       pc.name AS column_name,
       c.name AS referenced_column,
       fk.delete_referential_action_desc AS on_delete,
       fk.update_referential_action_desc AS on_update
  FROM sys.foreign_keys AS fk
  JOIN sys.foreign_key_columns AS fkc ON fkc.constraint_object_id = fk.object_id
  JOIN sys.objects AS pt ON pt.object_id = fk.parent_object_id
  JOIN sys.schemas AS ps ON ps.schema_id = pt.schema_id
  JOIN sys.columns AS pc
    ON pc.object_id = fk.parent_object_id AND pc.column_id = fkc.parent_column_id
  JOIN sys.columns AS c
    ON c.object_id = fk.referenced_object_id AND c.column_id = fkc.referenced_column_id
 WHERE fk.referenced_object_id = OBJECT_ID(?)
 ORDER BY pt.name, fk.name, fkc.constraint_column_id
"""

CHECK_CONSTRAINTS_SQL = """
SELECT cc.name AS constraint_name,
       cc.definition
  FROM sys.check_constraints AS cc
 WHERE cc.parent_object_id = OBJECT_ID(?)
 ORDER BY cc.name
"""

TRIGGERS_SQL = """
SELECT tr.name AS trigger_name,
       tr.is_disabled,
       tr.is_instead_of_trigger,
       te.type AS event_type
  FROM sys.triggers AS tr
  JOIN sys.trigger_events AS te ON te.object_id = tr.object_id
 WHERE tr.parent_id = OBJECT_ID(?)
   AND tr.parent_class = 1
   AND te.type IN (1, 2, 3)
 ORDER BY tr.name, te.type
"""

TEMPORAL_SQL = """
SELECT t.temporal_type,
       hs.name AS history_schema,
       ht.name AS history_table
  FROM sys.tables AS t
  LEFT JOIN sys.tables AS ht ON ht.object_id = t.history_table_id
  LEFT JOIN sys.schemas AS hs ON hs.schema_id = ht.schema_id
 WHERE t.object_id = OBJECT_ID(?)
"""

Row = Mapping[str, Any]

#: ``sys.foreign_keys`` referential action codes → domain enum.
REFERENTIAL_ACTIONS: dict[str, ReferentialAction] = {
    "NO_ACTION": ReferentialAction.NO_ACTION,
    "CASCADE": ReferentialAction.CASCADE,
    "SET_NULL": ReferentialAction.SET_NULL,
    "SET_DEFAULT": ReferentialAction.SET_DEFAULT,
}

#: ``sys.trigger_events.type`` values we catalog (1=INSERT, 2=UPDATE, 3=DELETE).
TRIGGER_EVENTS: dict[int, str] = {1: "INSERT", 2: "UPDATE", 3: "DELETE"}

#: ``sys.objects.type`` values we expose.
_KIND_BY_OBJECT_TYPE = {"U": TableKind.BASE_TABLE, "V": TableKind.VIEW}

#: Data types whose ``max_length`` is a real character/byte length.
_LENGTH_TYPES = frozenset(
    {"char", "nchar", "varchar", "nvarchar", "binary", "varbinary", "text", "ntext", "image"}
)

#: Types whose (precision, scale) pair is meaningful.
_PRECISION_TYPES = frozenset({"decimal", "numeric", "datetime2", "datetimeoffset", "time"})

#: ``sys.tables.temporal_type`` codes.
TEMPORAL_NONE = 0
TEMPORAL_HISTORY = 1
TEMPORAL_SYSTEM_VERSIONED = 2

#: ``max_length`` value meaning "MAX".
_MAX_LENGTH = -1


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    number = _as_int(value)
    return bool(number)


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def database_rows_sql() -> str:
    """SQL listing online, writable user databases and marking the current one."""
    return """
SELECT d.name AS database_name,
       DB_NAME() AS current_database,
       d.database_id AS database_id
  FROM sys.databases AS d
 WHERE d.state = 0
   AND d.database_id > 4
   AND d.is_read_only = 0
 ORDER BY d.name
"""


def table_rows_sql(schema: str | None = None) -> str:
    """SQL listing base tables and views with row estimate, PK flag and trigger flag.

    ``sys.dm_db_partition_stats`` supplies the cheap approximate row count (no scan).
    When ``schema`` is given, a single ``?`` parameter is expected first.
    """
    where = "WHERE o.is_ms_shipped = 0"
    if schema is not None:
        where += "\n   AND s.name = ?"
    return f"""
SELECT s.name AS schema_name,
       o.name AS table_name,
       o.type AS object_type,
       CASE WHEN EXISTS (SELECT 1
                           FROM sys.indexes AS i
                          WHERE i.object_id = o.object_id
                            AND i.is_primary_key = 1) THEN 1 ELSE 0 END AS has_primary_key,
       ISNULL(rp.row_count, 0) AS approximate_rows,
       CASE WHEN EXISTS (SELECT 1
                           FROM sys.triggers AS t
                          WHERE t.parent_id = o.object_id) THEN 1 ELSE 0 END AS has_triggers,
       CASE WHEN EXISTS (SELECT 1
                           FROM sys.foreign_keys AS fk
                          WHERE fk.parent_object_id = o.object_id)
           THEN 1 ELSE 0 END AS has_foreign_keys
  FROM sys.objects AS o
  JOIN sys.schemas AS s ON s.schema_id = o.schema_id
  LEFT JOIN (SELECT object_id, SUM(row_count) AS row_count
               FROM sys.dm_db_partition_stats
              WHERE index_id IN (0, 1)
              GROUP BY object_id) AS rp ON rp.object_id = o.object_id
  {where}
   AND o.type IN ('U', 'V')
 ORDER BY s.name, o.name
"""


COLUMNS_SQL = """
SELECT c.name AS column_name,
       c.column_id,
       t.name AS data_type,
       c.max_length,
       c.precision,
       c.scale,
       c.is_nullable,
       c.is_identity,
       c.is_computed,
       c.is_rowversion,
       c.collation_name,
       dc.definition AS default_definition,
       ic.seed_value,
       ic.increment_value,
       cc.definition AS computed_definition,
       cc.is_persisted AS computed_persisted
  FROM sys.columns AS c
  JOIN sys.types AS t ON t.user_type_id = c.user_type_id
  LEFT JOIN sys.default_constraints AS dc ON dc.object_id = c.default_object_id
  LEFT JOIN sys.identity_columns AS ic
         ON ic.object_id = c.object_id AND ic.column_id = c.column_id
  LEFT JOIN sys.computed_columns AS cc
         ON cc.object_id = c.object_id AND cc.column_id = c.column_id
 WHERE c.object_id = OBJECT_ID(?)
 ORDER BY c.column_id
"""

KEYS_SQL = """
SELECT i.name AS index_name,
       i.is_primary_key,
       ic.key_ordinal,
       c.name AS column_name
  FROM sys.indexes AS i
  JOIN sys.index_columns AS ic
    ON ic.object_id = i.object_id AND ic.index_id = i.index_id
  JOIN sys.columns AS c
    ON c.object_id = ic.object_id AND c.column_id = ic.column_id
 WHERE i.object_id = OBJECT_ID(?)
   AND (i.is_primary_key = 1 OR i.is_unique_constraint = 1)
   AND ic.is_included_column = 0
 ORDER BY i.is_primary_key DESC, i.name, ic.key_ordinal
"""


def referential_action(raw: object) -> ReferentialAction:
    """Map a ``sys.foreign_keys`` action code or description onto the domain enum."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        actions = list(REFERENTIAL_ACTIONS.values())
        return actions[raw] if 0 <= raw < len(actions) else ReferentialAction.NO_ACTION
    text = (_as_str(raw) or "NO ACTION").strip().upper().replace(" ", "_")
    return REFERENTIAL_ACTIONS.get(text, ReferentialAction.NO_ACTION)


def _type_name(raw: object) -> str:
    """Normalize a ``sys.types`` name to the friendly SQL Server spelling."""
    text = (_as_str(raw) or "").strip()
    return {"timestamp": "rowversion"}.get(text.lower(), text)


def object_kind(raw: object) -> TableKind | None:
    """Map a ``sys.objects.type`` code to a domain kind, ``None`` for unsupported types."""
    return _KIND_BY_OBJECT_TYPE.get((_as_str(raw) or "").strip())


def row_count(raw: object) -> int | None:
    """Normalize a ``sys.dm_db_partition_stats.row_count`` value."""
    count = _as_int(raw)
    return count if count is not None else None


def _max_length(data_type: str, raw: object) -> int | None:
    """Return a character length for sized types, ``None`` for unsized/MAX ones."""
    lowered = data_type.lower()
    if lowered not in _LENGTH_TYPES:
        return None
    length = _as_int(raw)
    if length is None or length == _MAX_LENGTH:
        return None
    if lowered in {"nchar", "nvarchar", "ntext"}:
        length = length // 2  # sys.columns reports bytes for Unicode types
    return length


def _precision_scale(
    data_type: str, precision: object, scale: object
) -> tuple[int | None, int | None]:
    if data_type.lower() not in _PRECISION_TYPES:
        return None, None
    return _as_int(precision), _as_int(scale)


def column_from_row(
    row: Row, *, is_primary_key: bool = False, is_foreign_key: bool = False
) -> Column:
    """Map one ``sys.columns`` row onto a :class:`Column`."""
    data_type = _type_name(row.get("data_type"))
    max_length = _max_length(data_type, row.get("max_length"))
    precision, scale = _precision_scale(data_type, row.get("precision"), row.get("scale"))
    is_computed = _as_bool(row.get("is_computed"))
    is_identity = _as_bool(row.get("is_identity"))
    is_rowversion = _as_bool(row.get("is_rowversion")) or data_type.lower() == "rowversion"
    computed_definition = _as_str(row.get("computed_definition"))

    return Column(
        name=_as_str(row.get("column_name")) or "",
        ordinal=_as_int(row.get("column_id")) or 0,
        data_type=data_type,
        max_length=max_length,
        precision=precision,
        scale=scale,
        nullable=_as_bool(row.get("is_nullable")),
        default_definition=_as_str(row.get("default_definition")),
        is_identity=is_identity,
        identity_seed=_as_int(row.get("seed_value")) if is_identity else None,
        identity_increment=_as_int(row.get("increment_value")) if is_identity else None,
        is_computed=is_computed,
        computed_definition=computed_definition,
        is_rowversion=is_rowversion,
        is_primary_key=is_primary_key,
        collation=_as_str(row.get("collation_name")),
        is_foreign_key=is_foreign_key,
        computed_persisted=(
            _as_bool(row.get("computed_persisted")) if is_computed and computed_definition else None
        ),
    )


def databases_from_rows(rows: Iterable[Row]) -> list[Database]:
    """Map ``sys.databases`` rows onto :class:`Database` objects, marking the current one."""
    materialized = list(rows)
    current = _as_str(materialized[0].get("current_database")) if materialized else None
    databases: list[Database] = []
    for row in materialized:
        name = _as_str(row.get("database_name")) or ""
        databases.append(Database(name=name, is_current=name == current))
    return databases


def table_summaries(rows: Iterable[Row]) -> list[TableSummary]:
    """Map ``sys.objects`` + ``sys.dm_db_partition_stats`` rows onto table summaries."""
    summaries: list[TableSummary] = []
    for row in rows:
        kind = _KIND_BY_OBJECT_TYPE.get((_as_str(row.get("object_type")) or "").strip())
        if kind is None:
            continue
        row_count = _as_int(row.get("approximate_rows"))
        summaries.append(
            TableSummary(
                schema=_as_str(row.get("schema_name")) or "",
                name=_as_str(row.get("table_name")) or "",
                kind=kind,
                has_primary_key=_as_bool(row.get("has_primary_key")),
                approximate_row_count=row_count if row_count is not None else None,
                has_triggers=_as_bool(row.get("has_triggers")),
                has_foreign_keys=_as_bool(row.get("has_foreign_keys")),
            )
        )
    return summaries


def unique_and_primary_keys(
    rows: Iterable[Row],
) -> tuple[PrimaryKey | None, tuple[UniqueConstraint, ...]]:
    """Map ``sys.indexes`` rows onto the primary key and UNIQUE constraints."""
    pk_columns: list[str] = []
    pk_name: str | None = None
    uniques: dict[str, list[str]] = {}
    for row in rows:
        column = _as_str(row.get("column_name"))
        if not column:
            continue
        if _as_bool(row.get("is_primary_key")):
            pk_name = _as_str(row.get("index_name")) or pk_name
            pk_columns.append(column)
        else:
            name = _as_str(row.get("index_name")) or ""
            uniques.setdefault(name, []).append(column)
    primary_key = PrimaryKey(pk_name, tuple(pk_columns)) if pk_columns else None
    unique_constraints = tuple(
        UniqueConstraint(name, tuple(columns)) for name, columns in uniques.items() if columns
    )
    return primary_key, unique_constraints


def foreign_keys(rows: Iterable[Row]) -> tuple[ForeignKey, ...]:
    """Map ``sys.foreign_keys`` rows (outgoing edges) onto domain foreign keys."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = _as_str(row.get("constraint_name"))
        column = _as_str(row.get("column_name"))
        referenced_column = _as_str(row.get("referenced_column"))
        if not name or not column or not referenced_column:
            continue
        entry = grouped.setdefault(
            name,
            {
                "columns": [],
                "referenced_columns": [],
                "referenced_schema": _as_str(row.get("referenced_schema")) or "dbo",
                "referenced_table": _as_str(row.get("referenced_table")) or "",
                "on_delete": referential_action(row.get("on_delete")),
                "on_update": referential_action(row.get("on_update")),
            },
        )
        entry["columns"].append(column)
        entry["referenced_columns"].append(referenced_column)

    result: list[ForeignKey] = []
    for name in sorted(grouped):
        entry = grouped[name]
        if not entry["referenced_table"]:
            continue
        result.append(
            ForeignKey(
                name=name,
                columns=tuple(entry["columns"]),
                referenced_schema=entry["referenced_schema"],
                referenced_table=entry["referenced_table"],
                referenced_columns=tuple(entry["referenced_columns"]),
                on_delete=entry["on_delete"],
                on_update=entry["on_update"],
            )
        )
    return tuple(result)


def incoming_foreign_keys(rows: Iterable[Row]) -> tuple[IncomingForeignKey, ...]:
    """Map ``sys.foreign_keys`` rows on the *referenced* side onto incoming edges."""
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        name = _as_str(row.get("constraint_name"))
        schema = _as_str(row.get("referencing_schema"))
        table = _as_str(row.get("referencing_table"))
        column = _as_str(row.get("column_name"))
        referenced_column = _as_str(row.get("referenced_column"))
        if not name or not schema or not table or not column or not referenced_column:
            continue
        key = (schema, table, name)
        entry = grouped.setdefault(
            key,
            {
                "columns": [],
                "referenced_columns": [],
                "on_delete": referential_action(row.get("on_delete")),
                "on_update": referential_action(row.get("on_update")),
            },
        )
        entry["columns"].append(column)
        entry["referenced_columns"].append(referenced_column)

    result = [
        IncomingForeignKey(
            name=name,
            schema=schema,
            table=table,
            columns=tuple(entry["columns"]),
            referenced_columns=tuple(entry["referenced_columns"]),
            on_delete=entry["on_delete"],
            on_update=entry["on_update"],
        )
        for (schema, table, name), entry in sorted(grouped.items())
    ]
    return tuple(result)


def check_constraints(rows: Iterable[Row]) -> tuple[CheckConstraint, ...]:
    """Map ``sys.check_constraints`` rows onto domain check constraints."""
    constraints = [
        CheckConstraint(
            name=_as_str(row.get("constraint_name")) or "",
            definition=_as_str(row.get("definition")) or "",
        )
        for row in rows
        if _as_str(row.get("constraint_name")) and _as_str(row.get("definition"))
    ]
    return tuple(constraints)


def triggers_from_rows(rows: Iterable[Row]) -> tuple[Trigger, ...]:
    """Map ``sys.triggers`` + ``sys.trigger_events`` rows onto domain triggers.

    Events are collected per trigger (``te.type`` codes 1/2/3 = INSERT/UPDATE/DELETE) and
    the firing mode comes from ``is_instead_of_trigger``.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = _as_str(row.get("trigger_name"))
        if not name:
            continue
        entry = grouped.setdefault(
            name,
            {
                "events": [],
                "instead_of": _as_bool(row.get("is_instead_of_trigger")),
                "enabled": not _as_bool(row.get("is_disabled")),
            },
        )
        event = TRIGGER_EVENTS.get(_as_int(row.get("event_type")) or 0)
        if event and event not in entry["events"]:
            entry["events"].append(event)

    triggers: list[Trigger] = []
    for name in sorted(grouped):
        entry = grouped[name]
        if not entry["events"]:
            continue
        firing = "INSTEAD OF" if entry["instead_of"] else "AFTER"
        events = tuple(event for event in TRIGGER_EVENTS.values() if event in entry["events"])
        triggers.append(Trigger(name, events, firing, bool(entry["enabled"])))
    return tuple(triggers)


def temporal_flags(rows: Sequence[Row]) -> tuple[bool, bool, str | None, str | None]:
    """Return ``(is_system_versioned, is_history_table, history_schema, history_table)``."""
    for row in rows:
        temporal_type = _as_int(row.get("temporal_type")) or TEMPORAL_NONE
        history_schema = _as_str(row.get("history_schema"))
        history_table = _as_str(row.get("history_table"))
        if temporal_type == TEMPORAL_SYSTEM_VERSIONED and history_schema and history_table:
            return True, False, history_schema, history_table
        if temporal_type == TEMPORAL_HISTORY:
            return False, True, history_schema, history_table
    return False, False, None, None


def build_table(
    *,
    schema: str,
    name: str,
    kind: TableKind,
    column_rows: Sequence[Row],
    key_rows: Sequence[Row] = (),
    fk_rows: Sequence[Row] = (),
    incoming_fk_rows: Sequence[Row] = (),
    check_rows: Sequence[Row] = (),
    trigger_rows: Sequence[Row] = (),
    temporal_rows: Sequence[Row] = (),
    approximate_row_count: int | None = None,
) -> Table:
    """Assemble a full :class:`Table` from already-fetched catalog rows.

    Column flags (``is_primary_key`` / ``is_foreign_key``) are cross-filled from the key
    and foreign-key rows so the UI and the SQL builder can rely on them.
    """
    primary_key, unique_constraints = unique_and_primary_keys(key_rows)
    foreign_keys_out = foreign_keys(fk_rows)
    incoming = incoming_foreign_keys(incoming_fk_rows)

    pk_columns = set(primary_key.columns) if primary_key else set()
    fk_columns = {column for fk in foreign_keys_out for column in fk.columns}
    columns = tuple(
        column_from_row(
            row,
            is_primary_key=(_as_str(row.get("column_name")) or "") in pk_columns,
            is_foreign_key=(_as_str(row.get("column_name")) or "") in fk_columns,
        )
        for row in column_rows
    )
    if not columns:
        raise MetadataError(
            f"no columns found for {TableRef(schema=schema, name=name)} — "
            "does the object exist and does the login have permission to read sys.* views?"
        )

    is_versioned, is_history, history_schema, history_table = temporal_flags(temporal_rows)
    return Table(
        schema=schema,
        name=name,
        kind=kind,
        columns=columns,
        primary_key=primary_key,
        foreign_keys=foreign_keys_out,
        unique_constraints=unique_constraints,
        check_constraints=check_constraints(check_rows),
        triggers=triggers_from_rows(trigger_rows),
        approximate_row_count=approximate_row_count,
        incoming_foreign_keys=incoming,
        is_system_versioned=is_versioned,
        is_history_table=is_history,
        history_schema=history_schema,
        history_table=history_table,
    )
