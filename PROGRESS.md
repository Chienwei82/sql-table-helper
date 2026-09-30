# PROGRESS — sql-table-swiss-knife

Milestone status. **Update this file at the end of every milestone** and commit it with
the milestone's work (see DESIGN.md §15 for the workflow).

Legend: ✅ done · 🔶 partial · ⛔ not started · 🚧 in progress

---

## Current state

**Milestone 2 complete** — the SQL Server provider's metadata and connection layer.

| Milestone | Scope | Status |
|---|---|---|
| M1 | Project skeleton & quality gates | ✅ |
| M2 | Provider abstraction & SQL Server read path | ✅ |
| M3 | Connection manager & table picker | ⛔ |
| M4 | Read-only grid & inspector | ⛔ |
| M5 | Edit & stage changes | ⛔ |
| M6 | Apply pipeline (transaction) | ⛔ |
| M7 | Clipboard | ⛔ |
| M8 | Hardening & release | ⛔ |

Quality gates at the time of writing: `ruff check` ✅ · `ruff format --check` ✅ ·
`mypy --strict` ✅ · `pytest` **206 passed, 14 skipped** (the skips are the live suite).

---

## M1 — Project skeleton & quality gates ✅

- uv-managed `pyproject.toml` (`requires-python = ">=3.14"`), src layout, console entry
  points `sql-table-swiss-knife` and `stsk`.
- ruff + mypy strict + pytest/pytest-asyncio configured.
- Runnable Textual app: title banner, version, quit binding.
- Domain dataclasses + identifier validation with unit tests.
- Provider protocol (`DatabaseProvider`, `SqlDialect`), `FakeProvider`, provider registry.
- Profile store (TOML, no secrets) and SecretStore (keyring + in-memory fallback).
- T-SQL dialect with quoting, literals, INSERT/UPDATE/DELETE and paging SQL.

## M2 — SQL Server metadata & connection layer ✅

### Connection profiles (`storage/`)
- `ProfileStore` — TOML file under the platformdirs config dir (override with
  `SWISSKNIFE_CONFIG_DIR`), atomic write, mode `0600`, sorted by name, `get`/`upsert`/
  `delete`/round-trip.
- **No passwords in the file, ever.** A `password`/`pwd`/`secret_ref`-adjacent secret key
  in the TOML is rejected loudly at load time; only `secret_ref` (a keyring account name)
  is persisted. Covered by `tests/unit/storage/test_profiles.py`.
- `SecretStore` protocol + `KeyringSecretStore` (service `sql-table-swiss-knife`) and
  `EphemeralSecretStore` fallback when no OS keyring backend works
  (`keyring.backends.fail.Keyring`, priority 0). `default_secret_store()` picks one;
  `resolve_password()` tries the store first, then prompts, then gives up.
- **SQL auth and Windows integrated auth** are both supported (`AuthMode.SQL` /
  `AuthMode.INTEGRATED`); **TrustServerCertificate**, `Encrypt`, connect timeout, driver
  name and application intent are per-profile options.

### Connection layer (`providers/mssql/`)
- `build_connection_string()` — the only function that builds a full ODBC string; braces
  values containing `;{}`, honours every option, rejects a password for integrated auth
  and a missing password for SQL auth (`AuthError`).
- `sanitize_connection_string()` and `sanitize_driver_message()` — the only sanctioned
  ways to render a connection string / driver message; passwords and UIDs are masked.
- `SingleThreadRunner` — one dedicated worker thread per connection (pyodbc connections
  are not thread-safe, DESIGN §5.1), with driver-error translation at the boundary.
- `errors.py` — `map_pyodbc_error()`: login failures → `AuthError`, timeouts/08xxx →
  `ConnectError`, integrity violations (SQLSTATE `23xxx` or vendor 515/547/2601/2627/
  2714) → `QueryError` with the constraint name extracted, missing objects →
  `MetadataError`. Passwords never appear in a mapped message.
- `connect` / `test_connection` / `disconnect` on the provider.

### Metadata (`providers/mssql/metadata.py` + `provider.py`)
`get_table_metadata()` reads the catalog through **`sys.*`** views and captures:

| Captured | Source |
|---|---|
| exact type with length / precision / scale | `sys.types`, `sys.columns` |
| nullability, collation | `sys.columns` |
| identity seed + increment | `sys.identity_columns` |
| computed definition + persisted flag | `sys.computed_columns` |
| rowversion columns | `sys.columns` (`is_rowversion`, `timestamp` → `rowversion`) |
| defaults | `sys.default_constraints` |
| PK and UNIQUE columns (ordered) | `sys.indexes`, `sys.index_columns` |
| FKs **outgoing** with referential actions | `sys.foreign_keys`, `sys.foreign_key_columns` |
| FKs **incoming** (referenced *by* other tables) | same views, `referenced_object_id` side |
| check constraint definitions | `sys.check_constraints` |
| triggers: AFTER/INSTEAD OF, INSERT/UPDATE/DELETE, enabled/disabled | `sys.triggers`, `sys.trigger_events` |
| **system-versioned (temporal) tables** and history-table pairing | `sys.tables.temporal_type` |
| approximate row counts (no scan) | `sys.dm_db_partition_stats` |
| tables with no primary key | implied by an absent PK; surfaced via `Table.primary_key is None`, `identity_columns == ()`, `updatable is False` |

Unicode `max_length` is reported in bytes by SQL Server and is halved for `nchar`/
`nvarchar`/`ntext`; `-1` (`MAX`) is reported as "no length".

### Listing
- `list_databases()` — online, writable user databases, current one flagged.
- `list_tables()` — schema, name, kind (table/view), PK flag, approximate row count,
  has-triggers, optionally filtered by schema.
- `fetch_rows()` — one page, ordered by the table's identity columns, `has_more` via
  `limit + 1` fetch.

### Registry
- `mssql` is a built-in provider, resolved lazily through `BUILTIN_PROVIDERS` so importing
  the registry never imports the driver.

### CLI (temporary, for verification)
```
sql-table-swiss-knife inspect <profile> <schema.table>   # metadata as Rich tables
sql-table-swiss-knife inspect <profile> x --list         # table listing
sql-table-swiss-knife inspect <profile> x --databases    # database listing
```
`inspect` is explicitly a developer aid for M2 and is expected to be reworked or removed
once the TUI inspector (M4) exists.

### Tests
| File | What it covers |
|---|---|
| `tests/unit/storage/test_profiles.py` | TOML round-trip, 0600 mode, secret-key rejection, validation |
| `tests/unit/storage/test_secrets.py` | keyring wrapper, ephemeral fallback, password resolution |
| `tests/providers/test_mssql_metadata.py` | the `sys.*` row → domain mappers, in detail |
| `tests/providers/test_mssql_sqltext.py` | guardrails: the required `sys.*` views are in use |
| `tests/providers/test_mssql_connection.py` | connection string, sanitizing, error mapping |
| `tests/providers/test_mssql_provider.py` | full provider path against a **fake pyodbc driver** |
| `tests/unit/test_cli.py` | `inspect` argument parsing and Rich rendering |
| `tests/live/test_mssql_live.py` | **live** integration tests against the docker server |

The live suite is marked `live` and is **skipped** unless
`SWISSKNIFE_TEST_DB_URL` points at a reachable server:

```bash
cd tests/live && docker compose up -d
export SWISSKNIFE_TEST_DB_URL="mssql://sa:SwissKnife%212022_Test@localhost:1433/SwissKnifeSample"
uv run pytest -m live
```

---

## Known gaps / follow-ups

- **The live suite has not been executed against a real server yet** — this machine has no
  Docker and no `libodbc.so.2`, so `import pyodbc` fails and the live tests skip. They are
  written to run, but the first real run is still pending. The provider path *is* covered
  end-to-end by `tests/providers/test_mssql_provider.py` against a fake driver.
- `MssqlProvider.execute_changes()` raises `NotImplementedError` — the Apply pipeline is M6.
- `MssqlProvider.fetch_rows()` ignores `FetchSpec.filters` and `sort` (M2 scope: verify
  introspection; filtering/sorting proper lands with the data service in M4).
- `services/` layer (connection manager, catalog service) is still missing — M3.
- `infra/logging.py` (redacting filter + `user_message()`) is not written yet; the
  sanitizing helpers in `providers/mssql/` cover the M2 surfaces, but DESIGN §8.2's
  logging-level filter is still open.
- `import-linter` (DESIGN §2 dependency rules) is not wired into CI yet.

## Next milestone — M3: connection manager & table picker

- Home screen: profile list, create/edit/duplicate/delete, "Test connection".
- Database picker.
- Table picker: search-as-you-type, schema grouping, table/view badges, keyboard + mouse.
- Verified against `FakeProvider` (Pilot tests) and manually against a real server.
