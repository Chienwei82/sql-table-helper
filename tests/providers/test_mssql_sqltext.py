"""Guardrails on the catalog SQL text (the views the milestone requires are in use)."""

import pytest

from sql_table_swiss_knife.providers.mssql import metadata as md
from sql_table_swiss_knife.providers.mssql.provider import OBJECT_LOOKUP_SQL, SERVER_INFO_SQL

#: Every sys.* view M2 must introspect through.
REQUIRED_VIEWS = {
    "sys.tables",
    "sys.columns",
    "sys.types",
    "sys.indexes",
    "sys.index_columns",
    "sys.foreign_keys",
    "sys.foreign_key_columns",
    "sys.check_constraints",
    "sys.default_constraints",
    "sys.triggers",
    "sys.computed_columns",
    "sys.identity_columns",
}

ALL_SQL = "\n".join(
    [
        md.COLUMNS_SQL,
        md.KEYS_SQL,
        md.FOREIGN_KEYS_SQL,
        md.INCOMING_FOREIGN_KEYS_SQL,
        md.CHECK_CONSTRAINTS_SQL,
        md.TRIGGERS_SQL,
        md.TEMPORAL_SQL,
        md.database_rows_sql(),
        md.table_rows_sql(),
        OBJECT_LOOKUP_SQL,
    ]
)


@pytest.mark.parametrize("view", sorted(REQUIRED_VIEWS))
def test_required_catalog_view_is_used(view: str) -> None:
    assert view in ALL_SQL


def test_incoming_foreign_keys_query_reads_the_referenced_side() -> None:
    assert "fk.referenced_object_id = OBJECT_ID(?)" in md.INCOMING_FOREIGN_KEYS_SQL


def test_outgoing_foreign_keys_query_reads_the_parent_side() -> None:
    assert "fk.parent_object_id = OBJECT_ID(?)" in md.FOREIGN_KEYS_SQL


def test_queries_are_scoped_to_one_object_via_object_id() -> None:
    for sql in (
        md.COLUMNS_SQL,
        md.KEYS_SQL,
        md.FOREIGN_KEYS_SQL,
        md.CHECK_CONSTRAINTS_SQL,
        md.TRIGGERS_SQL,
    ):
        assert "OBJECT_ID(?)" in sql


def test_schema_filter_uses_a_bound_parameter() -> None:
    assert "s.name = ?" in md.table_rows_sql("dbo")
    assert "?" not in md.table_rows_sql(None)


def test_trigger_events_are_restricted_to_dml() -> None:
    assert "te.type IN (1, 2, 3)" in md.TRIGGERS_SQL


def test_row_counts_come_from_partition_stats_not_a_scan() -> None:
    assert "sys.dm_db_partition_stats" in md.table_rows_sql()
    assert "COUNT_BIG" not in md.table_rows_sql()


def test_server_info_reads_product_version() -> None:
    assert "SERVERPROPERTY" in SERVER_INFO_SQL
    assert "DB_NAME()" in SERVER_INFO_SQL
