"""T-SQL (Microsoft SQL Server) dialect implementation."""

import math
import re
from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from ...domain.rows import FilterOp
from ..dialect import Condition, ErrorFacts, SqlScript

__all__ = ["TSqlDialect"]


#: ``… constraint 'PK_Region'`` — the name is the most actionable part of the message.
_CONSTRAINT_RE = re.compile(r"constraint\s+'([^']+)'", re.IGNORECASE)
#: ``… column 'Name' …`` / ``the column 'Name'`` — points at the offending column.
_COLUMN_RE = re.compile(r"column\s+'([^']+)'", re.IGNORECASE)

#: Wording for each recognizable failure class, so the UI need not show raw vendor text.
_KIND_HINTS: tuple[tuple[str, str, str], ...] = (
    ("foreign key", "fk", "referenced row is missing or still in use"),
    ("cannot insert the value null", "not-null", "the column does not accept NULL"),
    ("duplicate key", "unique", "another row already has this value"),
    ("violation of unique", "unique", "another row already has this value"),
    ("string or binary data would be truncated", "truncation", "the value is too long"),
    ("conversion failed", "conversion", "the value is not valid for the column type"),
    ("is not a valid value", "conversion", "the value is not valid for the column type"),
    ("check constraint", "check", "a CHECK constraint rejected the value"),
)


#: ``bigint`` range — the widest integer SQL Server parses, so the widest integer literal.
_BIGINT_MIN = -(2**63)
_BIGINT_MAX = 2**63 - 1


def _check_row_widths(rows: Sequence[Sequence[str]], width: int) -> None:
    """Every rendered row must carry exactly one value per column.

    A batched INSERT/MERGE is the one place where a mis-rendered value would not fail on
    its own — it would land in the neighbouring column. Checking the width here means a
    batch is either entirely well-formed or refused, never quietly misaligned.
    """
    for row in rows:
        if len(row) != width:
            raise ValueError(f"every row must have {width} values, got {len(row)}")


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

    def escape_like(self, text: str) -> str:
        return escape_like(text)

    def inspect_error(self, message: str) -> ErrorFacts:
        constraint = _CONSTRAINT_RE.search(message)
        column = _COLUMN_RE.search(message)
        lowered = message.lower()
        kind, hint = "error", ""
        for needle, name, explanation in _KIND_HINTS:
            if needle in lowered:
                kind, hint = name, explanation
                break
        return ErrorFacts(
            kind=kind,
            hint=hint,
            constraint=constraint.group(1) if constraint is not None else None,
            column=column.group(1) if column is not None else None,
        )

    def literal(self, value: object) -> str:
        """Render a copy-ready T-SQL literal (display only — execution uses parameters).

        This is the *only* place a Python value becomes SQL text (S-6). It is therefore
        also where the awkward values are handled, rather than by the callers:

        * ``None`` → ``NULL``, ``bool`` → ``1``/``0`` (checked before ``int``, which
          ``bool`` subclasses);
        * ``str`` → ``N'…'`` with ``'`` doubled. The ``N`` prefix is what makes a literal
          survive a non-Unicode column collation, which is the whole point of copying SQL
          between environments. Newlines, tabs and other control characters are kept
          verbatim — they are legal inside a T-SQL string literal and rewriting them would
          change the value;
        * ``int`` beyond ``bigint`` is refused rather than rendered: a number SQL Server
          cannot parse is worse than a clear error at generation time;
        * ``bytes`` → ``0x…`` (upper-case hex), ``Decimal`` → plain digits, ``float`` via
          ``repr`` (finite only), dates/times → ISO-8601, ``UUID`` → its text form.
        """
        if value is None:
            return "NULL"
        # bool before int: bool is a subclass of int.
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            if not _BIGINT_MIN <= value <= _BIGINT_MAX:
                raise ValueError(
                    f"integer {value} does not fit a SQL Server bigint "
                    f"({_BIGINT_MIN}..{_BIGINT_MAX}) and cannot be a literal"
                )
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
            if "\x00" in value:
                # A NUL cannot survive a T-SQL string literal: the server truncates at it,
                # so the copied script would silently insert a different value.
                raise ValueError("string contains a NUL character and cannot be a literal")
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
        conditions: Sequence[Condition],
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
        conditions: Sequence[Condition],
    ) -> str:
        if not conditions:
            raise ValueError("DELETE requires WHERE conditions (never delete unscoped)")
        qualified = self.quote_qualified(schema, table)
        return f"DELETE FROM {qualified} WHERE {self._conditions(conditions)}"

    def select_by_key_sql(
        self,
        schema: str,
        table: str,
        columns: Sequence[str],
        conditions: Sequence[Condition],
    ) -> str:
        if not columns:
            raise ValueError("SELECT needs at least one column")
        if not conditions:
            raise ValueError("SELECT by key needs at least one condition")
        column_sql = ", ".join(self.quote_ident(column) for column in columns)
        qualified = self.quote_qualified(schema, table)
        return f"SELECT {column_sql} FROM {qualified} WHERE {self._conditions(conditions)}"

    def insert_rows_sql(
        self,
        schema: str,
        table: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[str]],
    ) -> str:
        """A single multi-row INSERT — how a table's catalog data is moved as one batch.

        Batching matters for the "INSERT script for all rows" action: thousands of separate
        INSERTs are slow to run and slow to read, and one statement keeps the copy/paste
        output usable. Every row must have the same width, so a rendered value can never
        silently end up next to the wrong column.
        """
        if not columns:
            raise ValueError("INSERT needs at least one column")
        if not rows:
            return ""
        _check_row_widths(rows, len(columns))
        column_sql = ", ".join(self.quote_ident(column) for column in columns)
        qualified = self.quote_qualified(schema, table)
        values_sql = ",\n    ".join("(" + ", ".join(row) + ")" for row in rows)
        return f"INSERT INTO {qualified} ({column_sql})\nVALUES\n    {values_sql}"

    def merge_sql(
        self,
        schema: str,
        table: str,
        key_columns: Sequence[str],
        columns: Sequence[str],
        rows: Sequence[Sequence[str]],
    ) -> str:
        """A MERGE that inserts new rows and updates matching ones, keyed by ``key_columns``.

        MERGE is the upsert the "generate MERGE" action exists for: moving a row between
        environments should update the row that is already there rather than collide with
        the primary key. The terminating ``;`` is mandatory after MERGE, so it is part of
        the returned text instead of being left to the caller. With only key columns to
        write there is nothing to UPDATE, and the ``WHEN MATCHED`` clause is omitted — a
        MERGE that matches and does nothing is an error in SQL Server.
        """
        if not columns:
            raise ValueError("MERGE needs at least one column")
        if not rows:
            return ""
        if not key_columns:
            raise ValueError("MERGE needs at least one key column")
        unknown = [name for name in key_columns if name not in columns]
        if unknown:
            raise ValueError(f"MERGE key columns must be written too: {unknown}")
        _check_row_widths(rows, len(columns))
        qualified = self.quote_qualified(schema, table)
        source_columns = ", ".join(
            f"{index} AS {self.quote_ident(name)}" for index, name in enumerate(columns)
        )
        source_values = ",\n    ".join("(" + ", ".join(row) + ")" for row in rows)
        updates = [name for name in columns if name not in key_columns]
        insert_columns = ", ".join(self.quote_ident(name) for name in columns)
        insert_values = ", ".join(f"source.{self.quote_ident(name)}" for name in columns)
        matched = (
            "WHEN MATCHED THEN\n    UPDATE SET "
            + ", ".join(
                f"target.{self.quote_ident(name)} = source.{self.quote_ident(name)}"
                for name in updates
            )
            + "\n"
            if updates
            else ""
        )
        return (
            f"MERGE INTO {qualified} AS target\n"
            f"USING (VALUES\n    {source_values}\n) AS source({source_columns})\n"
            f"ON {self._join_predicate(key_columns)}\n"
            f"{matched}"
            "WHEN NOT MATCHED BY TARGET THEN\n"
            f"    INSERT ({insert_columns}) VALUES ({insert_values});"
        )

    def _join_predicate(self, columns: Sequence[str]) -> str:
        return " AND ".join(
            f"target.{self.quote_ident(name)} = source.{self.quote_ident(name)}" for name in columns
        )

    def identity_insert_sql(self, schema: str, table: str, *, enabled: bool) -> str:
        qualified = self.quote_qualified(schema, table)
        return f"SET IDENTITY_INSERT {qualified} {'ON' if enabled else 'OFF'}"

    def script_sql(self, script: SqlScript) -> str:
        """Wrap statements in ``BEGIN TRANSACTION`` / ``TRY…CATCH`` / ``COMMIT``.

        The all-or-nothing envelope FR-5.3 and S-7 ask for, explicit rather than implicit:

        * ``SET XACT_ABORT ON`` makes a *runtime* error abort the whole transaction instead
          of leaving it open and committable;
        * the ``CATCH`` rolls back and re-``THROW``s, so the failure is reported with its
          original error rather than swallowed;
        * ``IF @@TRANCOUNT > 0`` guards the rollback, because a statement can fail *before*
          the transaction opens (a syntax error, a bad ``SET``) and a bare ROLLBACK would
          then replace the real error with "no corresponding BEGIN TRANSACTION";
        * ``SET IDENTITY_INSERT`` is opened and closed inside the transaction, so an abort
          cannot leave the table locked for other writers.
        """
        if not script.body:
            return ""
        lines = ["SET XACT_ABORT ON;", "BEGIN TRANSACTION;", "BEGIN TRY;"]
        if script.identity_insert is not None:
            schema, table = script.identity_insert
            lines.append(f"    {self.identity_insert_sql(schema, table, enabled=True)};")
        indent = "    " if script.identity_insert is not None else ""
        for statement in script.body:
            lines.append(f"{indent}{statement.rstrip().rstrip(';')};")
        if script.identity_insert is not None:
            schema, table = script.identity_insert
            lines.append(f"    {self.identity_insert_sql(schema, table, enabled=False)};")
        lines.append(f"{indent}COMMIT TRANSACTION;")
        lines.extend(
            (
                "END TRY",
                "BEGIN CATCH",
                "    IF @@TRANCOUNT > 0",
                "        ROLLBACK TRANSACTION;",
                "    THROW;",
                "END CATCH;",
            )
        )
        return "\n".join(lines)

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
        after_key: Sequence[tuple[str, str]] = (),
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
        all_predicates = list(predicates)
        if after_key:
            # Keyset paging: continue *after* the last row of the previous page. Faster
            # and stable against concurrent inserts, unlike OFFSET on a deep page.
            all_predicates.append(
                self.keyset_predicate_sql(
                    [column for column, _ in after_key], [rendered for _, rendered in after_key]
                )
            )
            offset = 0
        if all_predicates:
            sql += " WHERE " + " AND ".join(all_predicates)
        # T-SQL requires ORDER BY before OFFSET/FETCH.
        order_sql = ", ".join(
            f"{self.quote_ident(column)}{' DESC' if descending else ''}"
            for column, descending in orders
        )
        sql += f" ORDER BY {order_sql or '(SELECT NULL)'}"
        sql += f" OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY"
        return sql

    def keyset_predicate_sql(self, key_columns: Sequence[str], placeholders: Sequence[str]) -> str:
        """Row-value comparison ``(a, b) > (@p0, @p1)`` for multi-column keys."""
        if not key_columns or len(key_columns) != len(placeholders):
            raise ValueError("keyset predicate needs matching key columns and placeholders")
        left = "(" + ", ".join(self.quote_ident(column) for column in key_columns) + ")"
        right = "(" + ", ".join(placeholders) + ")"
        return f"{left} > {right}"

    def begin_transaction(self) -> str:
        return "BEGIN TRANSACTION"

    def commit_transaction(self) -> str:
        return "COMMIT TRANSACTION"

    def rollback_transaction(self) -> str:
        return "ROLLBACK TRANSACTION"

    def _conditions(self, conditions: Sequence[Condition]) -> str:
        return " AND ".join(
            f"{self.quote_ident(condition.column)} IS NULL"
            if condition.is_null
            else f"{self.quote_ident(condition.column)} = {condition.rendered}"
            for condition in conditions
        )


def escape_like(text: str) -> str:
    """Escape LIKE wildcards so a typed ``%`` matches a literal percent sign.

    SQL Server escapes a LIKE metacharacter with brackets, so ``%`` becomes ``[%]``.
    Building the result in a single pass matters: escaping ``[`` first and ``]`` after
    would re-escape the brackets this function just introduced.
    """
    return "".join("[" + char + "]" if char in ("%", "_", "[", "]") else char for char in text)
