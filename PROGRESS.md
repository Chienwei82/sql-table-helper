# PROGRESS — sql-table-swiss-knife

Milestone status. **Update this file at the end of every milestone** and commit it with
the milestone's work (see DESIGN.md §15 for the workflow).

Legend: ✅ done · 🔶 partial · ⛔ not started · 🚧 in progress

---

## Current state

**Milestone 6 complete** — the SQL panel: the SQL for every pending change, in apply order,
in three renderings (parameterized / literal / script), with syntax highlighting, three
copy targets, "generate SQL for…" on any row or the current filter, and a hard dry-run
posture — the panel produces text and never executes anything.

| Milestone | Scope | Status |
|---|---|---|
| M1 | Project skeleton & quality gates | ✅ |
| M2 | Provider abstraction & SQL Server read path | ✅ |
| M3 | Connection manager & table picker | ✅ |
| M4 | Read-only grid & inspector | ✅ |
| M5 | Edit & stage changes | ✅ |
| M6 | The SQL panel (FR-5) | ✅ |
| M7 | Clipboard | ⛔ |
| M8 | Hardening & release | ⛔ |

Quality gates at the time of writing: `ruff check` ✅ · `ruff format --check` ✅ ·
`mypy --strict` ✅ · `pytest` **658 passed, 14 skipped** (the skips are the live suite).
The SVG snapshot set predates this milestone and is out of sync with the tests (see *Known
gaps*); the 99 new tests are ordinary pytest/Pilot tests, not snapshots.

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

## M4 — Read-only grid & inspector ✅

### Data service (`services/data.py`)

- `DataService.fetch()` returns an immutable `RowWindow` (rows + limit + `has_more` +
  total), never a bare page: the grid, the header and the status bar all read the same
  object, so "rows 1-1000 • more?" can never disagree with what is on screen.
- Default page size 1000 (S-8/OQ-7), `fetch_more()` appends the next page reusing the
  current sort, and asking for more when there is none is a no-op, not an error.
- Deterministic order: the identity columns (PK, else a single-column NOT NULL UNIQUE per
  OQ-5) ascending; a keyless table falls back to its first column and is read-only anyway.
- `status_text()` renders the FR-3.8 wording (`rows 1-5`, `• more?`, `of N`).

### Inspector model (`services/inspector.py`)

Pure functions over the metadata model — no I/O, no Textual — so every decision the panel
makes is unit-testable (NFR-5):

- `build_warnings(table)` → severity-ordered `Warning` list (`ERROR` > `WARNING` > `INFO`),
  deterministic in (severity, code):
  - **Triggers**: `INSTEAD OF` is an *error* ("your INSERT/UPDATE/DELETE may not do what
    you expect"), `AFTER`/`FOR` is a warning naming the events, **disabled triggers are
    listed but `dimmed`** and say they will not fire;
  - **No primary key**: an error when no UNIQUE key can stand in ("row identity is
    ambiguous; updates/deletes are blocked until you confirm them", S-4), a warning when a
    single-column NOT NULL UNIQUE does;
  - **Temporal**: system-versioned tables warn that rows are versioned into their history
    table; a history table is informational;
  - **Server-managed columns**: computed and rowversion columns are listed by name;
  - **Collations**: columns with an explicit collation are a warning (they can reject
    values on write);
  - **CHECK constraints**: shown with their expression, explicitly *not* evaluated by the
    app;
  - **Incoming FKs**: "referenced by N tables — deleting rows may fail or cascade to N
    tables", with both referential actions listed per FK; two FKs from the same table count
    once;
  - **Large tables** above 50 000 rows get the S-8 advisory.
- `column_badges()` / `header_label()` — `🔑` PK (with the ordinal in a composite key), `🔗`
  FK → target table.column, `#` identity, `ƒ` computed, `⏱` rowversion, `∅` nullable /
  `✱` required, `D` default, `U` unique, `✓` check, `🔒` read-only in headers.
- `format_data_type()` renders the *exact* type: `nvarchar`/`nchar` lengths are halved
  (sys reports bytes), `decimal(p,s)`, `float(p)`, `char/varchar/binary(n)`.
- `table_summary_rows()` and `column_detail()` — the two fact lists (FR-6.1/FR-6.2).
- `InspectorService` holds only *which table* and *which column is focused*; everything else
  is re-derived on demand, so a reload can never leave a stale detail on screen, and an
  unknown column name degrades to "no detail" instead of raising.

### Live cell validation (`services/validation.py`)

Ready for M5's editor, tested now as pure functions:

- Parses the types the milestone names — `int`/`bigint`/`smallint`/`tinyint` (with the
  declared range), `decimal`/`numeric`/`money`/`float`, `bit` (0/1/true/false), `date`,
  `datetime*`, `time`, `uniqueidentifier` (real GUID parse), `binary`/`varbinary` — and
  passes unknown types through as text rather than refusing to type.
- NULL is rejected on NOT NULL columns with "is NOT NULL — NULL is rejected", accepted (and
  announced) on nullable ones.
- Counters while typing: `3/100 characters`, `6/10 digits, 2/2 scale`; exceeding the length,
  the precision or the scale **blocks** staging with an explicit message.
- Server-managed columns refuse edits outright ("read-only (S-3)").
- UNIQUE, CHECK and FK risks are reported as warnings/notes ending in
  *"will be verified by the database"* — the app never pretends to know the answer.

### TUI (`tui/widgets/data_grid.py`, `tui/widgets/inspector.py`, `tui/screens/table_editor.py`)

- **Split view**: `DataGrid` on the left, `InspectorPanel` on the right; `F2` collapses and
  re-expands the panel and the footer hint follows the state.
- **Grid**: frozen header row, virtualized scrolling, cell cursor; headers carry
  name + badges + compact type; `NULL` renders as upper-case `NULL`, binary as `0x…` with a
  byte count; keyless tables render dimmed (`-readonly`); `CellState` (unchanged / modified /
  new / deleted / invalid / read-only) with its glyph map is in place for M5.
- **Inspector panel**: four sections (TABLE SUMMARY, WARNINGS, COLUMN LIST, COLUMN DETAIL)
  rendered as one Rich `Text`; severities map to semantic theme roles (`$error`, `$warning`,
  `$muted`), never literal colours; disabled triggers render dim.
- **The two halves are one surface**: moving the grid cursor re-focuses the inspector's
  column, so the detail always describes the cell under the cursor (FR-6.3).
- **Warning strip above the grid** keeps a collapsed panel honest: `⛔ 2 critical · ⚠ 3
  cautions — see the inspector (F2)`.
- `enter` on a cell explains itself (read-only reason, or "cell editing lands in Milestone 5")
  rather than doing nothing silently.

### Tests

| File | What it covers |
|---|---|
| `tests/unit/services/test_inspector.py` | **56 tests** over the warning generator: every kind, severities, ordering, disabled triggers, no-PK (error vs warning), temporal, collations, CHECK, incoming-FK cascade counts/notes, badges, exact type formatting, detail rows, summary rows, cell formatting |
| `tests/unit/services/test_inspector_service.py` | focused-column state, stale-column tolerance, replacing the table clears everything |
| `tests/unit/services/test_validation.py` | **42 tests**: parsing per type, invalid input messages, NULL rules, length/precision/scale counters and blockers, read-only columns, "verified by the database" risks |
| `tests/unit/services/test_data.py` | page size, identity ordering, fetch-more append/no-op, status wording |
| `tests/tui/test_table_editor.py` | **20 Pilot tests**: split view, F2 + hints, the four inspector sections, detail follows the cursor, warning strip, header badges/types, cell rendering, read-only explanation, paging |
| `tests/tui/test_snapshots.py` | 4 new snapshots (split view, read-only table, collapsed inspector, `INSTEAD OF` view) — 16 total |

Every TUI test runs against `FakeProvider` with temporary profile/settings stores and an
in-memory secret store: no database, no keyring, no touching the developer's config.

## Known gaps / follow-ups

- **The live suite has not been executed against a real server yet** — this machine has no
  Docker and no `libodbc.so.2`, so `import pyodbc` fails and the live tests skip. They are
  written to run, but the first real run is still pending. The provider path *is* covered
  end-to-end by `tests/providers/test_mssql_provider.py` against a fake driver.
- **The TUI has not been exercised against a real SQL Server** either: M4 was verified with
  `FakeProvider` and snapshot tests only. The manual pass against the docker server
  (connect → pick a database → browse tables → open one → read the inspector) is still
  outstanding.
- `MssqlProvider.execute_changes()` raises `NotImplementedError` — the Apply pipeline is M6.
- `MssqlProvider.fetch_rows()` still ignores `FetchSpec.filters` and `sort`: the M4 data
  service sends a deterministic order and the provider honours it in its own SQL, but a
  user-driven sort/filter UI has not landed.
- **The grid is read-only.** Cell editing, staging, the pending-change badge and the SQL
  preview are M5; the validation rules they need (`services/validation.py`) and the cell
  state model (`CellState`) are already in place and tested.
- **`services/changes.py`, `preview.py` and `clipboard.py` do not exist yet** — they arrive
  with M5 (staging + preview) and M6/M7.
- **User theme *files* are not supported yet**: the three built-ins are Textual `Theme`
  objects (which is what the installed Textual version offers), and a fourth theme can be
  added as another entry in `tui/theme.py`. Loading a `.tcss` theme from the config dir,
  which DESIGN §9.4 mentions, is still open.
- `infra/logging.py` (redacting filter + `user_message()`) is not written yet; the
  sanitizing helpers in `providers/mssql/` cover the M2 surfaces, but DESIGN §8.2's
  logging-level filter is still open.
- `import-linter` (DESIGN §2 dependency rules) is not wired into CI yet.

## M6 — The SQL panel ✅

The app's thesis is "edit catalog tables *without writing SQL*"; this milestone is where you
can still *see* the SQL, exactly as it would run (FR-5).

### The panel (`F3`, `tui/widgets/sql_panel.py`)

- **Statements in apply order**, one row per pending change plus a "whole script" row.
- **Three renderings, one key (`v`)** — parameterized (with a parameter legend), literal
  (values inlined and escaped) and script (the transaction envelope). The **script is the
  default** because it is the rendering that is safe to run by hand.
- **Syntax highlighting** via Rich `Syntax`, with the theme picked from the active Textual
  theme so it stays readable on all three (`tui/widgets/sql_panel.py:syntax_theme`).
- **Copy (`y`)** — whole script, or the selected statement. In parameterized mode the copy
  carries the parameter legend, because `@p0` on its own is not pasteable. "Copy script"
  always copies the *runnable* script, whatever is displayed: a fragment of a transaction
  looks runnable and is not.
- **Dry-run.** The panel's header says `preview only — nothing is executed from here`, and it
  is enforced structurally: the module has no I/O at all. A Pilot test asserts the fake
  provider's `executed` list is unchanged after generating SQL.

### "Generate SQL for…" (`g`, `tui/screens/sql_action.py`)

`SELECT` / `INSERT` / `UPDATE` / `DELETE` for the focused row, `MERGE` (upsert by PK) and
`INSERT script for all rows in this table/filter` — the last one being how you move a catalog
table between environments: one batched multi-row `INSERT` instead of hundreds of statements.

Only actions that can produce a *correct* statement are offered, and **the reason for every
omission is shown** (no key → no UPDATE/DELETE; no primary key → no MERGE; no rows loaded).

### Escaping, centralized and tested

`SqlDialect.literal()` is the single seam where a Python value becomes SQL text (S-6), and
this milestone hardened it for the inputs that break naive implementations. The batched
INSERT/MERGE and the script envelope are new dialect methods, so a second DBMS gets them
behind the same seam.

`tests/unit/test_dialect_escaping.py` (58 tests) covers: doubled quotes (asserted by
**round-trip**, not by counting quotes), newlines/CR/tab/control characters, unicode, NUL
(**refused** — the server would truncate it), binary as `0x…`, `bigint` boundaries
(out-of-range **refused**), `Decimal` in exponent form, NaN/Infinity, leap days and year 1,
tz-aware datetimes, and `datetime`-vs-`date` isinstance ordering.

### Tests

- `tests/unit/test_dialect_escaping.py` — 58 tests, the escaping battery above.
- `tests/unit/services/test_sqlpreview.py` — 27 tests: the three renderings carry the same
  values, the script is all-or-nothing, `IDENTITY_INSERT` is only emitted when it is *legal*,
  and every generated action either produces a statement or refuses with a reason.
- `tests/tui/test_sql_panel.py` — 14 Pilot tests: F3, the mode cycle, each rendering's actual
  text, the three copy targets, the action picker, and the dry-run property.

## Known gaps left behind

- **No snapshot for the panel.** The SVG snapshot set was already out of sync with the tests
  before this milestone (9 mismatches that the plugin reports without failing), and
  regenerating it churned six unrelated files, so I left it alone and relied on the Pilot
  tests, which assert the panel's real content rather than its pixels. Worth a deliberate
  `--snapshot-update` pass and a review of the diff.
- **`clipboard.py` (M7) still does not exist.** `y` uses Textual's `copy_to_clipboard`; there
  is no TSV/CSV copy of a row or selection yet.
- **Applied statements do not linger as "ran (success)"** (FR-5.5, the post-Apply half). The
  panel shows *pending* changes; keeping the last Apply's SQL on screen is next.
- `import-linter` (DESIGN §2 dependency rules) is not wired into CI yet.


## M7 — Copy & paste ✅

Copy a cell, a row, a column or a selected rectangle; paste an Excel block with a preview
that tells you what it will do **before** anything is staged; import and export CSV/JSON
through the same pipeline.

### The clipboard chain (`infra/clipboard.py`)

- **pyperclip** → **platform tool** (`pbcopy`, `wl-copy`/`xclip`/`xsel`, `clip.exe`) →
  **OSC 52** (Textual's `copy_to_clipboard`). Native first because a system clipboard is
  where the user looks next; OSC 52 last because it is the only one that works over SSH and
  inside tmux. The outcome names the mechanism that took the text, and a failed copy is
  reported rather than assumed.
- **Reading is opt-in** (`clipboard_read_fallback`): OSC 52 cannot read, and pulling from
  someone's clipboard is more invasive than pushing to it.

### The pure core (`services/clipboard.py`)

Everything the feature *decides* is a plain function over plain values, so "what will this
paste do?" is answerable without a terminal and cannot drift from what the dialog shows.

- `parse_block` — delimiter family detected from the payload (tab → TSV, comma/quote → CSV,
  `[` → JSON, else one cell), BOM and CRLF normalized, **RFC 4180 quoting** so a cell may
  contain tabs and newlines, ragged rows padded, header rows detected from the table's own
  column names.
- `normalize_text` — the locale layer: the `NULL` token (configurable, optionally literal),
  `1.234,56 → 1234.56` for `de`, `31.01.2026 → 2026-01-31` for `dmy`, all *before* the
  validation that decides acceptability.
- `plan_paste` — column mapping (by header name, else positional from the anchor, skipping
  identity/computed columns — S-3), per-cell conversion through the **same**
  `validate_input` the cell editor uses, and the **UPDATE vs INSERT split** per row: a row
  carrying the primary key updates the matching row, anything else inserts.
- `encode_block` — TSV/CSV/JSON out, with the configured NULL representation; copy and paste
  round-trip through the same quoting.

### Paste Preview (`tui/screens/paste_preview.py`)

Four sections, one screen: the column mapping and what stays untouched, the converted value
next to the pasted text, the UPDATE/INSERT split, and every per-cell error. A plan with one
bad cell is **refused** — there is no "stage the valid rows" button, because a half-pasted
block is the failure this milestone exists to prevent. Big blocks are planned in a worker
thread, so a 5000-row paste cannot freeze the UI.

### Import / export (`services/transfer.py`)

A file is a paste whose source you chose: the same parser, the same converter, the same
preview. CSV is written with a UTF-8 BOM (Excel reads UTF-8 CSV only with one; TSV/JSON get
none, where it would corrupt the first column name).

### Tests

| File | What it covers |
|---|---|
| `tests/unit/services/test_clipboard.py` | **65 tests**: format detection, BOM/CRLF, quoted tabs/newlines, ragged rows, header-vs-data, JSON shapes, malformed JSON, encoding round-trips, NULL token, `de`/`dmy`/`mdy` conversion, mode choice, fill/fill-broadcast, UPDATE-vs-INSERT, positional identity skipping, per-cell errors, the row limit |
| `tests/unit/services/test_paste_staging.py` | **6 tests**: a confirmed plan stages updates and inserts; one bad cell stages nothing; a keyless row is skipped with a warning; nothing reaches the database (S-1) |
| `tests/unit/test_clipboard_io.py` | **18 tests**: backend order, each fallback, the SSH case, the per-platform command table, read-fallback gating, the OSC 52 bytes |
| `tests/unit/services/test_transfer.py` | **18 tests**: export shapes, the NULL representation shared with the clipboard, BOM rules, an Excel-written CSV planning like a paste, missing/binary files |
| `tests/tui/test_clipboard.py` | **20 Pilot tests**: bracketed paste → preview → stage, the preview's wording, cancel, cell/multiline paste, a refused paste, copy scopes, the format cycle and its persistence, export to file, import through the preview, a 500-row paste |

## Known gaps left behind

- **Selection is keyboard-only** (`shift+arrows`). Mouse-drag selection is still not wired
  into the grid, so `ctrl+c` with the *selection* scope needs the keyboard.
- **No live pass.** The clipboard flows are covered by unit tests and Pilot tests against
  `FakeProvider`; nothing has been driven against a real terminal, a real clipboard or a real
  SQL Server. In particular, the OSC 52 path is asserted on the bytes it emits, not on a
  terminal accepting them.
- **`infra/logging.py`** (redacting filter) and **`import-linter`** are still open (M8).

## Next milestone — M8: hardening & release

- Mouse-drag selection in the grid, so `ctrl+c` can copy a rectangle without the keyboard.
- Excel *file* (.xlsx) import — still not in scope (SPEC §5); CSV/JSON file import is.
