# SPEC — sql-table-swiss-knife

Status: **Draft v0.1 — awaiting review**
Companion document: [DESIGN.md](DESIGN.md) (architecture, tech choices, verification report).

---

## 1. Product summary

`sql-table-swiss-knife` is a Python 3.14 terminal (TUI) application for **viewing and editing
rows of database tables that have no CRUD UI** — primarily catalog/lookup tables. Two goals:

1. Let the user edit catalog tables **without writing SQL**.
2. When useful, show **the exact SQL the app would run (or ran)** so it can be reviewed and copied.

First target DBMS: **Microsoft SQL Server**. PostgreSQL, MySQL and SQLite come later; therefore
every DBMS-specific piece (connection handling, metadata queries, SQL generation details, literal
syntax) must be isolated behind a provider/dialect abstraction from day one (see DESIGN.md §5–6).

## 2. Scope

### 2.1 In scope (v1)

- Saved connection profiles; test connection; SQL authentication and Windows integrated auth.
- Pick a database; list tables and views grouped by schema.
- Editable data grid: keyboard-first navigation, mouse support, color themes.
- Clipboard: copy cell/row/selection as TSV/CSV/JSON; paste a cell value; paste multi-line
  TSV/CSV blocks (Excel-style) to fill cells or append new rows.
- Staged change tracking (insert/update/delete) with explicit **Apply** inside a single transaction.
- SQL preview panel: parameterized statement (as sent) and literal copy-ready version.
- Column/table metadata inspector (columns, PK, FKs, unique/check constraints, triggers).
- Safety confirmations, read-only protections, fetch limits.
- Secrets in OS keyring or prompted per session; never in plain-text files, never in logs.

### 2.2 Out of scope (v1)

- Arbitrary SQL console / free-form query execution.
- Any DDL (CREATE/ALTER/DROP), schema designer, index management.
- Stored procedure / function editing or execution.
- User/permission administration, backup/restore.
- File-based bulk import (CSV/Excel *file* import may come later; clipboard paste is in scope).
- PostgreSQL, MySQL, SQLite support (only the abstraction must be ready for them).
- Multi-table joins, updatable JOIN views, ORM features.

## 3. Verified environment facts

Verified on the development machine (2026-09-30); evidence and full comparison in DESIGN.md §10:

| Fact | Result |
|---|---|
| Python | 3.14.4 installed; project targets `requires-python = ">=3.14"` |
| uv | 0.12.5 installed |
| pyodbc | 5.3.0 ships **cp314 wheels for all platforms incl. free-threaded cp314t**; wheel installs on 3.14.4 (import requires a system ODBC driver manager) |
| mssql-python | 1.15.0 ships cp314 wheels, imports cleanly on 3.14.4 (bundles its ODBC layer); no cp314t wheels; 1.7.0 had a cp313/cp314 packaging regression (GitHub issue #588) |
| textual / rich | 8.2.8 / 15.0.0 install and import on 3.14.4; `Pilot`, `run_test()`, `copy_to_clipboard()` (OSC 52) and `Paste` events confirmed present |
| pytest stack | pytest 9.1.1 + pytest-asyncio 1.4.0 run on 3.14.4 |

## 4. Functional requirements

Requirement IDs (FR-x.y) are stable references for tests and review.

### FR-1 Connection manager

- **FR-1.1 Profiles.** Create, edit, rename, duplicate, delete named connection profiles. A
  profile stores: name, host, port, ODBC driver name (or driver alias), default database,
  authentication mode, connection options (encrypt, trust server certificate, connect timeout),
  and a *secret reference* — never a secret itself.
- **FR-1.2 Authentication modes.** (a) SQL authentication (username + password),
  (b) Windows integrated authentication (no password stored or prompted; Windows only).
- **FR-1.3 Test connection.** Explicit "Test" action per profile returning success (server
  version, round-trip latency) or a friendly error. Errors must never include credentials.
- **FR-1.4 Database picker.** After connecting, list databases on the server; connect to the
  profile's default database or let the user pick another.
- **FR-1.5 Object listing.** List tables and views of the current database (name, schema, kind).
- **FR-1.6 Secret handling.** Passwords are read from the OS keyring when available; otherwise
  prompted per session and kept only in memory. An optional "save password" action is offered
  only when a keyring backend is functional. Plain-text password storage is never possible.

### FR-2 Table picker

- **FR-2.1 Search-as-you-type filter.** A single filter input narrows the list instantly
  (case-insensitive substring match over `schema.table` and view names).
- **FR-2.2 Schema grouping.** Results grouped under collapsible schema headers (`dbo`, …).
  Grouping stays visible while filtering (empty groups collapse).
- **FR-2.3 Kind indicators.** Table vs. view visibly distinguished; badge when the table has a
  primary key (read/write eligibility hint, see S-4).
- **FR-2.4 Navigation.** Fully keyboard-driven (up/down, PageUp/PageDown, Enter to open, Esc to
  go back); mouse click/scroll supported. Opening a table pushes the grid screen.

### FR-3 Editable data grid

- **FR-3.1 Rendering.** Frozen column headers; horizontal + vertical scrolling; auto column
  widths (content-based, resizable); NULL rendered distinctly (e.g. italic `NULL`); binary as
  `0x…` with length; monospace alignment.
- **FR-3.2 Navigation (keyboard-first).** Arrow keys / hjkl, Tab/Shift-Tab, PageUp/PageDown,
  Home/End, jump to first/last row; cell cursor distinct from selection.
- **FR-3.3 Selection.** Single cell, extended ranges (Shift+arrows), full rows, full columns;
  mouse click and drag; the selection drives copy and paste-fill.
- **FR-3.4 Mouse support.** Click to move the cursor, drag to select, wheel to scroll,
  double-click to edit.
- **FR-3.5 Cell editing.** Enter or double-click opens an inline editor; Enter commits the edit
  *to the staging area* (not the DB); Esc cancels; typed values are parsed/validated against the
  column's data type and precision before being staged; invalid input shows an inline error and
  blocks staging.
- **FR-3.6 Read-only cells.** Identity, computed, and rowversion columns are never editable
  (styled read-only). Cells in rows without a usable key are read-only (see S-4).
- **FR-3.7 Cell/row state styling.** Distinct visual states: unchanged, edited (staged update),
  staged insert (new row), staged delete, validation error, read-only. Minimum **3 built-in
  themes** plus a user theme file (Textual CSS). State must never be signalled by color alone —
  glyphs/markers accompany color so themes stay safe for color-vision differences.
- **FR-3.8 Row loading.** Configurable fetch limit (default 1000, S-8) with explicit "fetch
  more"; status bar shows the fetched count and whether more rows exist. When the table has a PK
  the default order is PK order; otherwise a stable deterministic order.
- **FR-3.9 Responsiveness.** All DB calls run off the UI event loop (FR-10); a spinner/status
  shows while loading; stale results from an earlier table or connection are discarded.

### FR-4 Clipboard (copy & paste)

- **FR-4.1 Copy scope.** Copy the current cell value, the current row, or the whole selection.
- **FR-4.2 Copy formats.** TSV (default — pastes directly into Excel/Sheets), CSV (RFC 4180
  quoting), JSON (array of objects; a single scalar for one cell). NULL has a configurable
  textual representation on copy (default: empty; alternative: literal `NULL`).
- **FR-4.3 Copy mechanism.** Primary: Textual `copy_to_clipboard()` (OSC 52 — works over SSH);
  optional fallback to system clipboard tooling when OSC 52 is unavailable.
- **FR-4.4 Paste a cell value.** Paste clipboard text into the focused cell (staged, subject to
  FR-3.5/FR-3.6 validation).
- **FR-4.5 Paste to fill.** Pasting a multi-cell block (TSV/CSV) onto a selection of matching
  shape fills the cells; a single value fills the whole selection.
- **FR-4.6 Paste to insert rows.** Pasting multi-line TSV/CSV at the end of the grid stages new
  rows. If the first pasted line matches column names (Excel header row), map by header;
  otherwise map positionally to visible/editable columns. Each value is type-validated per
  column; per-cell failures are collected and reported — nothing is staged half-parsed.
- **FR-4.7 Paste source.** Terminal bracketed paste (Textual `Paste` event) is the primary path
  — pasting from Excel into the terminal delivers the TSV block. Optional system-clipboard read
  fallback for terminals without bracketed paste.
- **FR-4.8 SQL copy.** The SQL preview supports one-key copy of the parameterized or literal
  form of a statement, or of the whole transaction script.

### FR-5 SQL preview panel

- **FR-5.1 Per-change statements.** For every pending insert/update/delete show the generated
  `INSERT` / `UPDATE` / `DELETE`.
- **FR-5.2 Two renderings.** (a) *Parameterized* — exactly what is sent (`… VALUES (@p0, @p1)`
  plus a parameter-value legend); (b) *Literal* — copy-ready SQL with values inlined and escaped
  (SQL Server syntax: `N'…'`, ISO-8601 dates, `0x…` binaries, `NULL`).
- **FR-5.3 Transaction script.** A combined view of all statements, optionally wrapped in
  `BEGIN TRAN … COMMIT` / `ROLLBACK`, for manual execution elsewhere.
- **FR-5.4 Truthfulness.** The preview is rendered from the *same* statement objects the
  executor uses — preview and execution can never diverge.
- **FR-5.5 After Apply.** Applied statements remain visible as "ran (success)" with timing,
  fulfilling the "show the SQL that ran" goal.

### FR-6 Metadata inspector

- **FR-6.1 Table-level panel** for the selected table: schema/name/kind, columns, primary key,
  foreign keys (local + referenced columns, referenced table, on-delete/on-update actions),
  unique constraints, check constraints, triggers (name, events, instead-of/after, enabled),
  approximate row count when cheaply available.
- **FR-6.2 Column detail** for the selected column: name, data type, length/precision/scale,
  nullability, default constraint, `is_identity` (seed/increment), `is_computed`,
  `is_rowversion`, PK membership, collation, FK membership, ordinal position.
- **FR-6.3 Access.** Inspector toggles from the grid (table + focused column) and from the
  table picker (without loading rows).

### FR-7 Change tracking and Apply

- **FR-7.1 Staging.** Every cell edit, pasted row insert, and marked delete is staged **in
  memory only** as a pending insert/update/delete. Nothing is sent to the DB until Apply.
- **FR-7.2 Visibility.** Pending changes are always countable at a glance (badge/status bar)
  and inspectable in the SQL preview; grid states follow FR-3.7.
- **FR-7.3 Revert.** Revert a single staged change (cell, row, or change-list entry) and
  discard all staged changes; original fetched values are restored in the grid.
- **FR-7.4 Explicit Apply only.** No autosave, no save-on-blur, no background flush. Apply is a
  deliberate user action (button or binding).
- **FR-7.5 Confirmation.** Apply opens a dialog summarising counts (N inserts / N updates /
  N deletes) with the SQL preview and requires explicit confirmation.
- **FR-7.6 Single transaction.** Apply executes all pending statements inside one transaction:
  all succeed → commit; any failure → rollback of everything, with an error report identifying
  the failing statement (FR-5 linkage) and the DB error. Partial application never happens.
- **FR-7.7 Post-apply.** On success: clear staging, refresh the affected rows (re-fetch),
  show "applied" with timings. On failure: keep staging intact so the user can fix or discard.
- **FR-7.8 Conflict detection.** If an UPDATE/DELETE matches 0 rows (row changed/deleted by
  someone else since fetch), treat as a conflict: fail the transaction with a clear message
  (optimistic concurrency; strengthened by rowversion when present — DESIGN §7).

### FR-8 Safety features

> Note: the prompt's "Safety features (see below)" list did not arrive with the requirements;
> the set below is proposed. See OQ-8 — please confirm or amend.

- **S-1** Explicit apply only (FR-7.4); the app never writes without a user command.
- **S-2** Confirmation dialog for every Apply; an extra "type `DELETE` to confirm" step when an
  Apply contains more than 50 deletes (threshold configurable).
- **S-3** Identity, computed, and rowversion columns are never editable (FR-3.6) — prevents
  corrupting server-managed values.
- **S-4** Rows that have no primary key (or usable unique key) are read-only: UPDATE/DELETE
  cannot be safely scoped, so the app refuses rather than risk a cross-row update.
- **S-5** No DDL, no arbitrary SQL execution, no procedure calls (scope §2.2) — only generated
  INSERT/UPDATE/DELETE against the table being viewed.
- **S-6** Injection safety: values are always bound parameters; identifiers are taken only from
  loaded metadata, validated (`^[A-Za-z_@$#][\w@$#]*$` or quoted-identifier rules) and quoted
  by the dialect — user text never lands in SQL text.
- **S-7** All-or-nothing transaction with rollback on any error (FR-7.6).
- **S-8** Fetch limit (default 1000 rows) prevents accidental full-table loads; "fetch more" is
  explicit; very large tables show an advisory.
- **S-9** Read-only mode: global toggle (and per-profile option) that makes Apply unavailable
  and renders all cells read-only — safe for shared/production catalogs.
- **S-10** Undo of staging before Apply (FR-7.3); Discard-all always one keystroke away.
- **S-11** "Copy script instead of applying" — the user can take the generated transaction
  script and run it through their own change-management process.

### FR-9 Security

- **S-sec-1** Passwords are never stored in plain text: OS keyring (Windows Credential
  Manager / macOS Keychain / Secret Service) keyed by profile name; if no keyring backend is
  functional, the password is prompted per session and held only in memory.
- **S-sec-2** Profile files (TOML) contain host/port/user/database/options and a `secret_ref`
  — never a password, never a full connection string with credentials.
- **S-sec-3** Connection strings and secrets are never logged. A redaction filter scrubs
  `password=`, `PWD=`, `pwd=`, `UID=`/`User Id` pairs and anything built from a connection
  string before any log record is written; there are unit tests asserting this (DESIGN §8).
- **S-sec-4** Error dialogs and tracebacks shown in the TUI pass through the same redaction.
- **S-sec-5** Windows integrated authentication is supported (no secret involved at all);
  SQL authentication uses keyring or prompt per S-sec-1.
- **S-sec-6** SQL preview contains data values (by design, for copying) but never credentials.
- **S-sec-7** Log files live in the per-user config directory with user-only permissions
  (0600) where the OS supports it.

### FR-10 Responsiveness (async rules)

- The Textual event loop must never execute a blocking DB call: all provider methods run in
  worker threads and are awaited (DESIGN §5.1).
- Long actions show progress (spinner + status text) within 100 ms.
- Every screen shows connection state; losing the connection mid-session surfaces a
  reconnect path rather than a crash.

## 5. Non-functional requirements

- **NFR-1 Quality gates:** `ruff check` + `ruff format --check` clean, `mypy --strict` clean,
  pytest green — enforced in CI from M1.
- **NFR-2 Performance:** app startup < 1 s; local UI reactions < 50 ms; any DB call shows
  progress within 100 ms; grid scrolling stays smooth with the 1000-row default page.
- **NFR-3 Platforms:** Linux, macOS, Windows (integrated auth is Windows-only by nature).
- **NFR-4 Python:** 3.14+ only (`requires-python = ">=3.14"`), modern typing (`X | Y`,
  `TypeAlias`, `Self`, `dataclass(slots=True)`).
- **NFR-5 Testability:** every milestone ships automated tests; core logic (staging, SQL
  generation, clipboard parsing, redaction) is tested without a database via `FakeProvider`.
- **NFR-6 Accessibility/usability:** color-vision-safe state markers (FR-3.7), full keyboard
  operation, help overlay listing all bindings (`?`).
- **NFR-7 Documentation:** README (install, quick start, keybindings), this SPEC/DESIGN pair,
  `docs/add-a-database.md`.

## 6. Milestones

### M1 — Project skeleton & quality gates

- uv project (`pyproject.toml`, `uv.lock`, Python 3.14), src layout, console entry point.
- ruff + mypy strict + pytest/pytest-asyncio configured; CI script (`uv run make check`-style).
- Runnable smoke app: Textual window with title, version, quit binding.
- Domain dataclasses + identifier validation with unit tests.
- Logging with redaction filter + tests proving connection strings/passwords never appear.
- Profile store (TOML, no secrets) + SecretStore (keyring with in-memory fallback) + tests.

### M2 — Provider abstraction & SQL Server read path

- `DatabaseProvider`/`SqlDialect` Protocols, `FakeProvider` for tests, provider registry.
- mssql provider: connection string builder, test connection, list databases/tables/views,
  full `TableMetadata` (columns/PK/FK/unique/check/trigger) via `sys.*` catalog queries.
- Row fetch with limit/paging and deterministic order.
- Provider contract tests run against `FakeProvider` (always) and SQL Server (env-gated:
  `SWISSKNIFE_TEST_DB_URL`), skipped otherwise.
- Report: driver wheel verification recorded in DESIGN §10 (done).

### M3 — Connection manager & table picker

- Home screen: profile list, create/edit/duplicate/delete, Test Connection, database picker.
- Table picker: search-as-you-type, schema grouping, table/view badges, keyboard + mouse.
- Picker works against a real server (manual) and against FakeProvider (Pilot tests).
- Connection state visible in the UI; clean disconnect on quit.

### M4 — Read-only grid & inspector

- Data grid renders fetched rows: frozen headers, scrolling, selection, mouse, themes (3 built-in).
- Fetch limit + fetch-more + status bar counts.
- Table picker → grid navigation with metadata loading; inspector panel for table and focused
  column (all FR-6 fields).
- Pilot tests: navigation, selection, scrolling, inspector contents; theming smoke tests.

### M5 — Edit & stage changes

- Cell editing with type validation (FR-3.5), read-only cell rules (S-3/S-4), staged states
  in the grid (FR-3.7), pending badge counts.
- Insert new row / mark row for delete; revert single change and discard-all (FR-7.3).
- `ChangeSet` domain logic with thorough unit tests (collapse/dedupe rules, DESIGN §7.1).
- SQL preview panel v1: parameterized + literal renderings per pending change (FR-5.1/5.2),
  copy actions (FR-5.4/5.5) — generated from `ChangeSet.statements()`.
- No Apply yet: preview only; "Apply" button disabled with tooltip "lands in M6".

### M6 — Apply pipeline (transaction)

- Confirmation dialog (FR-7.5) → single-transaction execution → commit/rollback (FR-7.6),
  error report with failing statement (FR-7.6) and optimistic-conflict handling (FR-7.8).
- Post-apply refresh + "ran (success)" presentation (FR-7.7, FR-5.5).
- Read-only mode toggle (S-9); large-delete confirmation (S-2).
- Tests: FakeProvider asserts begin/execute/commit & begin/execute/rollback sequences;
  Pilot tests for dialog flows; env-gated live transaction test.

### M7 — Clipboard

- Copy cell/row/selection as TSV/CSV/JSON (FR-4.1–4.3) via `copy_to_clipboard`.
- Paste cell value; paste block to fill selection; paste multi-line TSV/CSV to append rows with
  header detection and per-cell validation report (FR-4.4–4.7).
- Clipboard parser module (`services/clipboard.py`) with a dedicated unit-test battery
  (quotes, embedded tabs/newlines, CRLF, BOM, ragged rows, header vs positional mapping).
- Pilot tests for paste flows and copy actions.

### M8 — Hardening & release

- FK-aware conveniences: jump from a cell to its referenced row; display FK/trigger/check info
  in the inspector (FR-6 complete); approximate row counts.
- Help overlay (`?`), keybinding reference, README + `docs/add-a-database.md`.
- Packaging: `uv tool install` / pipx-ready; `--version`/`--live` flags; log rotation; error
  UX pass (disconnect mid-session, DB down, wrong driver name).
- Full E2E Pilot suite (connect → pick → edit → preview → apply → verify) against
  FakeProvider; optional live E2E env-gated; CI badge.

## 7. Open questions

Each item: question — **recommended default** (my pick if you don't care).

- **OQ-1 SQL Server driver: pyodbc vs mssql-python?** — **Default: pyodbc 5.3.0+**
  (evidence in DESIGN §10: broadest cp314 wheel matrix incl. free-threaded builds, mature
  PEP 249 implementation, de-facto ecosystem standard). mssql-python 1.15.0 also works on 3.14
  and is pip-only (bundles its ODBC layer) but had a cp313/cp314 packaging regression (1.7.0,
  issue #588) and no cp314t wheels. Because the provider abstraction hides the driver, the
  profile could later expose `driver = "pyodbc" | "mssql-python"` — do you want that option in
  v1? **Default: pyodbc only in v1, seam kept open.**
- **OQ-2 CLI/package naming?** Package `sql_table_swiss_knife`, console script
  `sql-table-swiss-knife`. **Default: also add short alias `stsk`.** (Repo folder is currently
  `sql-table-helper` — rename it? **Default: rename to `sql-table-swiss-knife`.**)
- **OQ-3 Models: stdlib dataclasses vs pydantic?** — **Default: stdlib frozen dataclasses**
  (zero deps, fast, mypy-strict-friendly; validation is hand-written where needed). Pydantic
  adds runtime weight and a version churn surface we don't need for internal models. Profiles
  are validated at load with explicit checks instead. Switch if you want JSON-schema export
  or heavy config validation later.
- **OQ-4 Protocol vs ABC for the provider interface?** — **Default: `typing.Protocol`**
  (structural typing keeps FakeProvider and future providers decoupled, no inheritance
  constraints). An ABC would give nicer shared error-handling helpers; we can add an
  `@runtime_checkable` protocol + a thin `BaseProvider` helper class without forcing
  inheritance.
- **OQ-5 Row identity when there is no PK?** — **Default: read-only rows (S-4)**, with a
  fallback single-column non-null UNIQUE constraint accepted as identity. Do *not* fall back
  to "match all columns" updates (fragile with duplicates/NULLs). Table-level badge tells the
  user upfront.
- **OQ-6 Editable views?** — **Default: views are read-only in v1.** Updatable views
  (INSTEAD OF triggers, multi-table semantics) add real complexity; if a view has an
  INSTEAD OF trigger we can enable editing later behind a capability flag.
- **OQ-7 Fetch limit / large tables?** — **Default: 1000 rows per page (S-8)**, explicit
  "fetch more", advisory banner above 50k estimated rows. Keyset paging is a stretch goal; is
  that enough for your catalogs, or do you regularly work with tables where offset paging
  hurts?
- **OQ-8 Safety feature set** — the "Safety features (see below)" list referenced in the
  requirements did not arrive. **Default: adopt S-1…S-11 as specified in FR-8.** Please
  confirm or send your list to merge.
- **OQ-9 Apply transaction isolation** — **Default: session default (READ COMMITTED)** with
  optimistic conflict detection via rowcount + rowversion guard. Explicit
  `SET TRANSACTION ISOLATION LEVEL SNAPSHOT` / `READ COMMITTED SNAPSHOT` is attractive but
  requires DB-level configuration — opt-in per profile? **Default: no per-session isolation
  switching in v1.**
- **OQ-10 Live test database** — integration tests need a real SQL Server.
  **Default: `tests/live/docker-compose.yml` with SQL Server 2022 (Developer), tests gated by
  `SWISSKNIFE_TEST_DB_URL`, skipped when absent.** Tell me if you have a preferred instance
  instead, or whether live tests should simply stay disabled for now.
- **OQ-11 Supported SQL Server versions** — **Default: SQL Server 2016+ and Azure SQL**
  (metadata queries and syntax avoid 2014-and-older edge cases; ODBC Driver 18 recommended,
  Driver 17 tolerated).

## 8. Assumptions

1. The "Safety features (see below)" list in the prompt was truncated; FR-8 proposes a
   replacement set (OQ-8).
2. "Catalog/lookup tables" means mostly small-to-medium tables (≤ a few hundred thousand
   rows); no streaming of million-row results in v1 (fetch limits apply).
3. Single active connection/session at a time in v1 (one grid workspace connected to one
   database).
4. Multi-user conflicts are rare; optimistic detection (FR-7.8) is sufficient — no locking
   UI in v1.
5. English-only UI; terminal supports UTF-8 + truecolor (Textual degrades gracefully).
6. The user runs the app in a normal terminal (Windows Terminal / iTerm / xterm-*) with
   bracketed paste enabled for the paste flows; OSC 52 for copy (FR-4.3/FR-4.7).
7. Values edited in cells are scalars (strings, numbers, dates, bytes, NULL) — no
   XML/JSON document editing UX in v1 (they are editable as text where the type allows).
