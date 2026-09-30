# PROGRESS — sql-table-swiss-knife

Milestone status. **Update this file at the end of every milestone** and commit it with
the milestone's work (see DESIGN.md §15 for the workflow).

Legend: ✅ done · 🔶 partial · ⛔ not started · 🚧 in progress

---

## Current state

**Milestone 3 complete** — the main UI structure: connection manager, searchable table
picker, table editor placeholder, theming and the command palette.

| Milestone | Scope | Status |
|---|---|---|
| M1 | Project skeleton & quality gates | ✅ |
| M2 | Provider abstraction & SQL Server read path | ✅ |
| M3 | Connection manager & table picker | ✅ |
| M4 | Read-only grid & inspector | ⛔ |
| M5 | Edit & stage changes | ⛔ |
| M6 | Apply pipeline (transaction) | ⛔ |
| M7 | Clipboard | ⛔ |
| M8 | Hardening & release | ⛔ |

Quality gates at the time of writing: `ruff check` ✅ · `ruff format --check` ✅ ·
`mypy --strict` ✅ · `pytest` **305 passed, 14 skipped** (the skips are the live suite).

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

---

## M3 — Main UI structure ✅

### New layer: `services/`

The TUI never touches a provider or a driver; it goes through two services that own the
session (DESIGN §2).

- `services/connection.py` — `ConnectionService`: profile CRUD, `test_connection`,
  `connect`, `disconnect`, and the observable state the UI renders
  (`state`, `session`, `last_error`, `is_busy`). One connection exists per session:
  connecting again closes the previous one first. Passwords are read from the
  `SecretStore` on demand and never persisted here (S-sec-1).
  `duplicate_profile()` copies a profile under a new name **and a new secret reference**,
  so a copy never shares the original credential slot.
- `services/catalog.py` — `CatalogService`: databases, tables/views and table metadata,
  cached per database and dropped on reconnect. The pure helpers
  `filter_summaries()` (FR-2.1) and `group_by_schema()` (FR-2.2) do the searching and
  grouping in memory, which is why typing in the picker never waits on the server.
- `storage/settings.py` — `Settings`/`SettingsStore` for `settings.toml`
  (DESIGN §13.2): atomic write, mode 0600, unknown keys ignored, typed values validated,
  a malformed file reported instead of silently ignored.

### Screens

| Screen | What it does |
|---|---|
| `ConnectionsScreen` | profile list with add / edit / duplicate / delete / test / connect, and an empty state that explains itself |
| `ProfileEditScreen` (modal) | the full profile form; the password field is write-only and goes straight to the keyring |
| `DatabasePickerScreen` (modal) | FR-1.4: lists the server's databases, with a live filter; connects first if needed |
| `ConfirmScreen` (modal) | yes/no guard before a destructive action |
| `TableBrowserScreen` | search-as-you-type over a schema-grouped tree, `f5` collapses the groups |
| `TableEditorScreen` | M3 placeholder: object identity, badges and the metadata read; the grid lands in M4 |

All of them share `AppScreen`, which supplies the chrome (header / status line / key
hints), the navigation helpers and — most importantly — the async contract:

- **Every DB call runs in a Textual worker.** `run_task()` / `load_tables()` schedule a
  coroutine, flip the status line to a spinner *before* the first `await`, and turn any
  exception into a status-line error **and** a toast. The UI never blocks on a driver.
- **Generation guard.** Each screen bumps a generation counter when its subject changes;
  a worker result from an older generation is dropped instead of flashing stale data
  (FR-3.9).
- **Four explicit states.** `loading` (spinner) / `ready` / `error` / `disconnected`, kept
  in sync between the header classes and the status line (DESIGN §9.3).

### Layout & chrome

- `AppHeader` — title on the left, connection identity and state on the right
  (`● connected · localhost:1433/CatalogDB`, `◐ connecting…`, `✖ connection error`).
- `StatusLine` — spinner plus the last outcome, styled with the theme's error/warning
  roles.
- `KeyHints` — a **context-sensitive** footer: the hints are recomputed from the screen's
  state (empty profile list → "n new profile"; connected → "b change database"), so the
  footer never advertises something that does not apply.

### Table picker badges (FR-2.3)

| Glyph | Meaning | Role in the palette |
|---|---|---|
| `🔑` | has a primary key | `$pk` |
| `🔗` | has foreign keys | `$fk` |
| `⚡` | has triggers | `$warning` |
| `⚠` | **no** primary key → read-only rows (S-4) | `$warning` |
| `👁` | view → read-only | `$nullable` |

The glyph carries the meaning and the colour only reinforces it, so the rows read
correctly in all three themes and for any colour-vision difference (FR-3.7).

### Theming (FR-3.7)

`tui/theme.py` defines three built-in themes — `default-dark`, `light`,
`high-contrast` — each declaring the **same semantic palette**: `pk`, `fk`, `identity`,
`computed`, `nullable`, `error`, `warning`, `pending`. Widgets reference roles
(`$pk`, `$muted`), never literal colours. `ctrl+t` cycles the themes, the command palette
lists them individually, and the choice is written to `settings.toml` and restored on the
next start. A broken settings file falls back to the default with a warning toast instead
of blocking startup.

### Command palette (Ctrl+P)

`tui/commands.py` plugs into Textual's palette with two content sources:

- **the active screen's actions**, derived from its `BINDINGS` — so the palette, the
  footer and the keyboard can never drift apart;
- **app-wide actions** — switch theme (×3), help, disconnect, go to connections, quit.

Textual's own fuzzy matcher ranks the hits, and coroutine actions are scheduled on the
message pump instead of being silently dropped.

### Domain change

`TableSummary` gained `has_foreign_keys` (the `🔗` badge needs it): the mssql listing
query now also checks `sys.foreign_keys`, and `Table.summary` propagates the trigger flag
and the row estimate so the fake and real providers agree.

### Tests

| File | What it covers |
|---|---|
| `tests/unit/storage/test_settings.py` | defaults, validation, atomic 0600 write, forward compatibility |
| `tests/unit/services/test_connection.py` | profile CRUD, connect/disconnect, session state, secret handling, error mapping |
| `tests/unit/services/test_catalog.py` | listing + caching, plus the pure filter/group helpers |
| `tests/unit/tui/test_badges.py` | badge rules and glyph-only rendering (no palette) |
| `tests/unit/tui/test_theme.py` | the three themes, the shared semantic palette, persistence |
| `tests/tui/test_connections.py` | Pilot: list, test, connect, duplicate, delete+confirm, profile form |
| `tests/tui/test_table_browser.py` | Pilot: grouping, badges, filtering, navigation, F5, offline path |
| `tests/tui/test_themes_and_palette.py` | Pilot: theme cycling + persistence, Ctrl+P content |
| `tests/tui/test_snapshots.py` | **13 SVG snapshots** of every main screen, in all three themes |
| `tests/tui/conftest.py` | fully injected app factory (temp stores, `FakeProvider`, sample catalog) |

Every TUI test runs against `FakeProvider` with temporary profile/settings stores and an
in-memory secret store: no database, no keyring, no touching the developer's config.

## Known gaps / follow-ups

- **The live suite has not been executed against a real server yet** — this machine has no
  Docker and no `libodbc.so.2`, so `import pyodbc` fails and the live tests skip. They are
  written to run, but the first real run is still pending. The provider path *is* covered
  end-to-end by `tests/providers/test_mssql_provider.py` against a fake driver.
- **The TUI has not been exercised against a real SQL Server** either: M3 was verified with
  `FakeProvider` and snapshot tests only. The manual pass against the docker server
  (connect → pick a database → browse tables → open one) is still outstanding.
- `MssqlProvider.execute_changes()` raises `NotImplementedError` — the Apply pipeline is M6.
- `MssqlProvider.fetch_rows()` ignores `FetchSpec.filters` and `sort` (M2 scope: verify
  introspection; filtering/sorting proper lands with the data service in M4).
- **`services/` now exists (connection + catalog), but `data.py`, `changes.py`,
  `preview.py` and `clipboard.py` do not** — they arrive with the grid (M4/M5) and the
  apply pipeline (M6).
- **User theme *files* are not supported yet**: the three built-ins are Textual `Theme`
  objects (which is what the installed Textual version offers), and a fourth theme can be
  added as another entry in `tui/theme.py`. Loading a `.tcss` theme from the config dir,
  which DESIGN §9.4 mentions, is still open.
- `infra/logging.py` (redacting filter + `user_message()`) is not written yet; the
  sanitizing helpers in `providers/mssql/` cover the M2 surfaces, but DESIGN §8.2's
  logging-level filter is still open.
- `import-linter` (DESIGN §2 dependency rules) is not wired into CI yet.

## Next milestone — M4: read-only grid & inspector

- `services/data.py`: fetch rows (limit, paging, deterministic order) and a metadata cache
  shared with the table picker.
- The grid widget itself: frozen headers, scrolling, selection, cell cursor, with the
  three themes wired to the semantic palette (the `$identity`/`$computed`/`$read-only`
  roles already exist).
- `TableEditorScreen` grows into the workspace: `schema.table` in the header, the row
  panel, the inspector (FR-6 table + column detail) and the status bar.
- Status bar counts ("rows 1–1000 • more?") and the fetch-more action (FR-3.8, S-8).
- Reuse the milestone's screenshot harness: add grid snapshots to
  `tests/tui/test_snapshots.py` as soon as the widget exists.
