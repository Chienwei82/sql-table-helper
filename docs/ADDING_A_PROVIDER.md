# How to add a new DBMS provider

A provider is the **only** thing in this codebase that talks to a database. Adding one
means implementing two protocols and registering a factory; no screen, service or
domain type has to change.

This guide uses PostgreSQL as the running example, because it differs from SQL Server in
exactly the ways that matter: double-quoted identifiers, `RETURNING` instead of
`OUTPUT`, and `SERIAL` instead of `IDENTITY`.

## What you implement

Two protocols, in `providers/base.py` and `providers/dialect.py`:

| Protocol | Answers | Talks to the DB? |
|---|---|---|
| `DatabaseProvider` | how do I connect, list, fetch, write? | **yes** — this is your driver |
| `SqlDialect` | how do I *spell* identifiers, placeholders, literals and statements? | no — pure string work |

The split matters. `SqlDialect` is pure, so the whole SQL surface of your DBMS is unit
testable with no server and no driver installed. If your dialect methods need a live
connection, the design is wrong.

## Step 1 — the dialect (start here, it's the fun part)

```python
# src/sql_table_swiss_knife/providers/postgres/dialect.py
"""PostgreSQL dialect."""

from ..dialect import Condition, SqlScript

__all__ = ["PostgresDialect"]


class PostgresDialect:
    name: str = "postgres"

    def quote_ident(self, ident: str) -> str:
        if not ident:
            raise ValueError("identifier must not be empty")
        return '"' + ident.replace('"', '""') + '"'

    def quote_qualified(self, schema: str | None, name: str) -> str:
        if schema is None:
            return self.quote_ident(name)
        return f"{self.quote_ident(schema)}.{self.quote_ident(name)}"

    def placeholder(self, index: int) -> str:
        # Postgres numbers parameters from 1; the shared sqlgen passes 0-based indexes.
        if index < 0:
            raise ValueError(f"placeholder index must be >= 0, got {index}")
        return f"${index + 1}"

    def literal(self, value: object) -> str:
        # The single seam where a Python value becomes SQL text. Handle None, bool,
        # numbers, Decimal, date/time, UUID, bytes (as \x hex) and str escaping.
        ...
```

Then the statement shapes. All identifier arguments arrive **unquoted**; you quote
them. Rendered values arrive as strings (placeholders or literals) so the
parameterized and copy-ready renderings share one composition path:

```python
    def select_by_key_sql(self, schema, table, columns, conditions) -> str: ...
    def insert_rows_sql(self, schema, table, columns, rows) -> str: ...
    def merge_sql(self, schema, table, key_columns, columns, rows) -> str: ...
    def script_sql(self, script: SqlScript) -> str: ...
```


## Step 2 — the provider (the I/O)

```python
# src/sql_table_swiss_knife/providers/postgres/provider.py
class PostgresProvider:
    """PostgreSQL provider: async psycopg over a thread pool."""

    def __init__(self) -> None:
        self._dialect = PostgresDialect()

    @property
    def name(self) -> str:
        return "postgres"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_schemas=True,
            supports_integrated_auth=False,  # no SSPI equivalent
            supports_rowversion=False,  # no rowversion; fall back to value compare
            supports_output_clause=True,  # RETURNING
        )

    @property
    def dialect(self) -> SqlDialect:
        return self._dialect

    async def connect(self, profile, password=None) -> ActiveConnection: ...
    async def disconnect(self, conn) -> None: ...
    async def list_databases(self, conn) -> list[Database]: ...
    async def list_tables(self, conn, schema=None) -> list[TableSummary]: ...
    async def get_table_metadata(self, conn, schema, name) -> Table: ...
    async def fetch_rows(self, conn, table, spec: FetchSpec) -> RowPage: ...
    async def execute_changes(self, conn, table, changes, options=None) -> ExecuteResult: ...
    def quote_identifier(self, name: str) -> str:
        return self._dialect.quote_ident(name)
```

Four things to get right:

- **Never block the event loop.** Every method is `async`, but your driver is sync.
  Run it in a thread (`asyncio.to_thread`) or use the async driver. The UI freezes if
  you call a blocking driver from an `async def`.
- **Map your errors.** Raise `ConnectError`, `AuthError`, `QueryError` or
  `MetadataError` from `providers/errors.py`, never a raw driver exception — the UI
  matches on these types to decide between a toast, a reconnect prompt and a full
  screen.
- **Report capabilities honestly.** `supports_rowversion=False` makes the safety
  policy fall back to comparing original values in the `WHERE` clause, which is slower
  but correct. Claiming a capability you do not have turns into wrong writes.
- **Transactions are yours.** `execute_changes` must be all-or-nothing and must return
  an `ExecuteResult` with `committed`, per-statement `StatementResult`s, a
  `failed_index` on rollback, `inserted_keys` from `RETURNING`, and a `RowConflict` for
  every UPDATE/DELETE that matched zero rows.

### `get_table_metadata` is where the work is

It must return a `Table` with: columns (name, ordinal, type, length, precision, scale,
nullable, default, identity, computed), the primary key, unique and check constraints,
triggers **with their enabled state**, foreign keys, approximate row count, and
**incoming** foreign keys. Those are what the inspector renders and what the safety
policy reads — an empty `incoming_foreign_keys` means a delete is shown as safe when it
would actually cascade.

## Step 3 — register it

In-tree, add to `BUILTIN_PROVIDERS` in `providers/__init__.py`:

```python
def _postgres_factory() -> DatabaseProvider:
    from .postgres import PostgresProvider  # lazy: the driver import must not be eager

    return PostgresProvider()


BUILTIN_PROVIDERS: dict[str, ProviderFactory] = {
    "mssql": _mssql_factory,
    "postgres": _postgres_factory,
}
```

Keep the import inside the factory. Importing the driver eagerly would make `import
sql_table_swiss_knife` fail for everyone who has not installed psycopg, and the mssql
provider sets that precedent on purpose.

## Step 4 — ship it as a plugin (no fork)

Third-party providers are discovered through the

## Step 5 — test it

Mirror the mssql test layout. The valuable tests, in order:

1. **Dialect unit tests, no server.** Quoting (including the escaping case: a column
   named `we"ird`), placeholders, `literal()` for every value type, and the shape of
   each generated statement. These are the tests that catch an injection or a
   misaligned batch.
2. **Batch width.** A multi-row INSERT whose rows do not all have the same number of
   values must raise, never land values in neighbouring columns. `mssql/dialect.py`
   shows the guard; copy the idea.
3. **Metadata mapping**, against fixture rows: a table with a composite PK, a generated
   column, a disabled trigger and two incoming FKs. One fixture with one of everything
   catches more than five realistic fixtures.
4. **Error mapping**: one test per driver error you translate, asserting the raised
   `providers.errors` type.
5. **A live test** in `tests/live/`, marked `@pytest.mark.live`, so the whole suite still
   runs without a server.

`tests/fakes.py::FakeProvider` is a complete reference implementation of the protocol
against in-memory data. If your provider is harder to write than that file, the seam is
probably in the wrong place.

## Checklist

- [ ] `SqlDialect` implemented, pure, no driver import
- [ ] `literal()` handles `None`, `bool`, `Decimal`, `date`/`datetime`/`time`, `UUID`,
      `bytes`, and escapes quotes in `str`
- [ ] `quote_ident` escapes the closing quote character
- [ ] `script_sql` guarantees all-or-nothing, or documents why it cannot
- [ ] `ProviderCapabilities` reflects reality, not aspiration
- [ ] driver errors mapped to `providers.errors` types
- [ ] no blocking driver call on the event loop
- [ ] rowcount == 0 on UPDATE/DELETE reported as a `RowConflict`
- [ ] identity/generated values returned in `inserted_keys`
- [ ] registered in `BUILTIN_PROVIDERS` (lazy import) or as an entry point
- [ ] dialect unit tests, metadata mapping test, error mapping tests
- [ ] `mypy` and `ruff` clean

`sql_table_swiss_knife.providers` entry point group, so you can support a DBMS without
forking this repository:

```toml
# your package's pyproject.toml
[project.entry-points."sql_table_swiss_knife.providers"]
postgres = "your_package:PostgresProvider"
```

`load_entry_point_providers()` picks it up at startup. Name collisions with a built-in
raise, which surfaces the packaging conflict immediately rather than silently shadowing
SQL Server with your provider.

`script_sql` is the one with a hard contract: **a statement that fails must leave
nothing applied.** A dialect that has real transactions should emit `BEGIN` / `COMMIT`
with an exception block that `ROLLBACK`s and re-raises. A dialect without them must
say so in a comment rather than pretend.
