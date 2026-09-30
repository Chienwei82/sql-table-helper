"""T-SQL (Microsoft SQL Server) dialect implementation."""

import math
from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from ...domain.rows import FilterOp

__all__ = ["TSqlDialect"]


class TSqlDialect:
    """Statement generation for Microsoft SQL Server (T-SQL)."""

    name: str = "tsql"

    def quote_ident(self, ident: str) -> str:
        if not ident:
            raise ValueError("identifier must not be empty")
        if "\x00" in ident:
            raise ValueError("identifier must not contain NUL characters")
        return "[" + ident.replace("]", "]]") + "]"

    def quote_qualified(self, schema: str | None, name: str) -> str:
        quoted_name = self.quote_ident(name)
        if schema is None:
            return quoted_name
        return f"{self.quote_ident(schema)}.{quoted_name}"

    def placeholder(self, index: int) -> str:
        if index < 0:
            raise ValueError(f"placeholder index must be >= 0, got {index}")
        return f"@p{index}"

    def literal(self, value: object) -> str:
        """Render a copy-ready T-SQL literal (display only — execution uses parameters)."""
        if value is None:
            return "NULL"
        # bool before int: bool is a subclass of int.
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("NaN/Infinity cannot be rendered as a SQL literal")
            return repr(value)
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError("non-finite Decimal cannot be rendered as a SQL literal")
            return format(value, "f")
        if isinstance(value, str):
            return "N'" + value.replace("'", "''") + "'"
        if isinstance(value, bytes | bytearray | memoryview):
            return "0x" + bytes(value).hex().upper()
        # datetime is a subclass of date — check it first.
        if isinstance(value, datetime):
            return "'" + value.isoformat(sep=" ") + "'"
        if isinstance(value, date):
            return "'" + value.isoformat() + "'"
        if isinstance(value, time):
            return "'" + value.isoformat() + "'"
        if isinstance(value, UUID):
            return f"'{value}'"
        raise TypeError(f"unsupported literal type: {type(value).__name__}")

    def insert_sql(
        self,
        schema: str,
        table: str,
        columns: Sequence[str],
        values: Sequence[str],
        output_columns: Sequence[str] = (),
    ) -> str:
        if not columns:
            raise ValueError("INSERT needs at least one column")
        if len(columns) != len(values):
            raise ValueError("INSERT columns and values must have the same length")
        column_sql = ", ".join(self.quote_ident(column) for column in columns)
        output_sql = ""
        if output_columns:
            rendered = ", ".join(
                f"INSERTED.{self.quote_ident(column)}" for column in output_columns
            )
            output_sql = f" OUTPUT {rendered}"
        value_sql = ", ".join(values)
        qualified = self.quote_qualified(schema, table)
        return f"INSERT INTO {qualified} ({column_sql}){output_sql} VALUES ({value_sql})"

    def update_sql(
        self,
        schema: str,
        table: str,
        assignments: Sequence[tuple[str, str]],
        conditions: Sequence[tuple[str, str]],
    ) -> str:
        if not assignments:
            raise ValueError("UPDATE needs at least one assignment")
        if not conditions:
            raise ValueError("UPDATE requires WHERE conditions (never update unscoped)")
        set_sql = ", ".join(
            f"{self.quote_ident(column)} = {rendered}" for column, rendered in assignments
        )
        qualified = self.quote_qualified(schema, table)
        return f"UPDATE {qualified} SET {set_sql} WHERE {self._conditions(conditions)}"

    def delete_sql(
        self,
        schema: str,
        table: str,
        conditions: Sequence[tuple[str, str]],
    ) -> str:
        if not conditions:
            raise ValueError("DELETE requires WHERE conditions (never delete unscoped)")
        qualified = self.quote_qualified(schema, table)
        return f"DELETE FROM {qualified} WHERE {self._conditions(conditions)}"

    def predicate_sql(self, column: str, op: FilterOp, placeholder: str | None = None) -> str:
        quoted = self.quote_ident(column)
        if not op.needs_value:
            return f"{quoted} {op.value}"
        if placeholder is None:
            raise ValueError(f"filter operator {op.value!r} requires a placeholder")
        return f"{quoted} {op.value} {placeholder}"

    def select_rows_sql(
        self,
        schema: str,
        table: str,
        columns: Sequence[str],
        predicates: Sequence[str],
        orders: Sequence[tuple[str, bool]],
        limit: int,
        offset: int,
    ) -> str:
        if not columns:
            raise ValueError("SELECT needs at least one column")
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset}")
        column_sql = ", ".join(self.quote_ident(column) for column in columns)
        qualified = self.quote_qualified(schema, table)
        sql = f"SELECT {column_sql} FROM {qualified}"
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        # T-SQL requires ORDER BY before OFFSET/FETCH.
        order_sql = ", ".join(
            f"{self.quote_ident(column)}{' DESC' if descending else ''}"
            for column, descending in orders
        )
        sql += f" ORDER BY {order_sql or '(SELECT NULL)'}"
        sql += f" OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY"
        return sql

    def begin_transaction(self) -> str:
        return "BEGIN TRANSACTION"

    def commit_transaction(self) -> str:
        return "COMMIT TRANSACTION"

    def rollback_transaction(self) -> str:
        return "ROLLBACK TRANSACTION"

    def _conditions(self, conditions: Sequence[tuple[str, str]]) -> str:
        return " AND ".join(
            f"{self.quote_ident(column)} = {rendered}" for column, rendered in conditions
        )
