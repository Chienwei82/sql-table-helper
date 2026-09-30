# sql-table-swiss-knife

A Python 3.14 terminal (TUI) application for **viewing and editing rows of database tables
that have no CRUD UI** — catalog/lookup tables. It stages edits in memory, shows the exact
SQL they will run (parameterized and copy-ready literal), and applies them in a single
transaction. First target DBMS: Microsoft SQL Server.

Status: **Milestone 7 — copy & paste.** The data grid, the schema inspector, staged
edits with a live SQL preview, and now the clipboard: copy a cell/row/column/selection as
TSV, CSV or JSON (OSC 52, so it works over SSH), paste an Excel block with a Paste Preview
that shows the column mapping, the type conversions, the UPDATE/INSERT split and every
per-cell error *before* anything is staged, and import/export CSV/JSON through the same
pipeline. See [PROGRESS.md](PROGRESS.md) for the full status and [SPEC.md](SPEC.md) /
[DESIGN.md](DESIGN.md) for requirements and design.

> This project is managed with [**uv**](https://docs.astral.sh/uv/) — `uv sync`,
> `uv add`, `uv run`. Do not `pip install` into the venv; there is no `requirements.txt`.

## Development

```bash
uv sync                     # install runtime + dev deps (Python 3.14)
uv run pytest               # unit + provider + TUI tests (live tests skip themselves)
uv run pytest -m live       # integration tests (need the docker server, see below)
uv run ruff check .         # lint
uv run ruff format --check .# format check
uv run mypy                 # strict type check
uv run sql-table-swiss-knife --version
uv run sql-table-swiss-knife   # launch the TUI (quit: ctrl+q)
```

## Using the TUI

| Key | Where | Action |
|---|---|---|
| `n` / `e` / `d` / `x` | connections | new / edit / duplicate / delete a profile |
| `t` | connections | test the profile (no session left behind) |
| `enter` | connections | connect — the database picker appears if the profile has no default |
| `b` | connections | switch database on the live session |
| `/` | tables | search-as-you-type over `schema.table` |
| `f6` | tables | collapse/expand the schema groups |
| `enter` | tables | open the selected table (grid + inspector) |
| `f2` | table | show/hide the inspector panel |
| `f3` | table | show/hide the **SQL panel** (F3) |
| `v` | table | cycle the SQL rendering: parameterized → literal → script |
| `y` | table | copy the SQL — the whole script, or the selected statement |
| `ctrl+c` | table | copy the cell / row / column / selection as the current format |
| `b` | table | cycle the copy scope: cell → row → column → selection |
| `p` | table | cycle the copy format TSV → CSV → JSON (remembered in settings.toml) |
| `ctrl+v` | table | paste (terminal bracketed paste is the primary path) |
| `i` / `o` | table | import a CSV/JSON file / export the rows on screen |
| `g` | table | "generate SQL for…" this row or the current filter |
| `m` | table | fetch the next page of rows (1000-row default page) |
| `r` | table | reload metadata and rows |
| arrows / `enter` | table | move the cell cursor (the inspector follows) / explain the cell |
| `ctrl+p` | anywhere | command palette (fuzzy search over the current screen's actions) |
| `ctrl+t` | anywhere | cycle the theme (persisted in `settings.toml`) |
| `esc` | anywhere | back one level |
| `ctrl+q` | anywhere | quit (the connection is closed first) |

Table rows carry glyph badges that survive every theme and colour-vision difference:
`🔑` has a primary key, `🔗` has foreign keys, `⚡` has triggers, `⚠` has **no** primary
key (rows are read-only, S-4) and `👁` is a view.

The same convention runs through the table workspace:

- **Grid headers** show the column name, its badges and the exact type — e.g.
  `Code 🔑✱ char(2)`. `🔒` marks a read-only column (identity, computed, rowversion).
- **The inspector panel** (`F2`) has four sections: TABLE SUMMARY (schema.name, rows, PK,
  "referenced by N tables"), a WARNINGS banner coloured by severity (`⛔` / `⚠` / `•`,
  disabled triggers dimmed), the COLUMN LIST with per-column badges
  (`🔑` PK with its ordinal in a composite key, `🔗` FK → target, `#` identity, `ƒ` computed,
  `⏱` rowversion, `∅` nullable / `✱` required, `D` default, `U` unique, `✓` check) and the
  COLUMN DETAIL of the focused column (exact type, nullability, default expression,
  identity seed/increment, computed definition, check text, unique index names, FK
  referential actions, collation).
- **Risk first.** An `INSTEAD OF` trigger says your INSERT/UPDATE/DELETE may not do what
  you expect; a table without a key says row identity is ambiguous; incoming foreign keys
  say deleting rows may fail or cascade, with the referential actions spelled out. CHECK and
  UNIQUE risks are marked "will be verified by the database" — the app never evaluates them
  itself.

## Copy & paste

**Copy** puts the cell, the whole row, the whole column or the selected rectangle on the
clipboard as TSV (default — pastes straight into Excel and Sheets), CSV (RFC 4180 quoting)
or JSON, with `NULL` written as empty text or as the literal word (`copy_null_repr`). The
scope is explicit (`b`) because "copy this row" and "copy the INSERT for this row" are
different requests and neither should be guessed at.

The mechanism is a chain: **pyperclip** if the optional extra is installed, otherwise the
platform's own command (`pbcopy`, `wl-copy`/`xclip`/`xsel`, `clip.exe`), and finally the
**OSC 52** escape sequence — which is what keeps copying working over SSH and inside tmux,
where no system clipboard exists. The status line names the mechanism that took the text,
and a failed copy is reported rather than assumed.

**Paste** is read from the terminal's *bracketed paste*, so a block copied from Excel
arrives intact as one event. It is parsed (BOM/CRLF normalized, RFC 4180 quoting so a cell
may contain tabs and newlines, JSON arrays of objects accepted) and then **planned**:

- **one value** → the focused cell, newlines and all;
- **a rectangular block over a selection** → that selection, one value filling all of it;
- **anything with several rows** → rows: `UPDATE` where the primary key matches a loaded
  row, `INSERT` where it does not.

A **Paste Preview** dialog then shows the column mapping (by header name when the block
carries one, positional otherwise), the converted value next to the pasted text
(`31.01.2026 → 2026-01-31`, `1.234,56 → 1234.56`), the UPDATE/INSERT split and every
per-cell validation error. **Nothing is staged until that dialog is confirmed**, and a plan
with a single bad cell is refused outright — there is no "paste the valid rows only".

Import from and export to **CSV/JSON files** go through exactly the same parser, converter
and preview, so a file can never be validated differently from a paste — and an import never
writes to the database.

The knobs live in `settings.toml`:

```toml
copy_format = "tsv"                # tsv | csv | json
copy_null_repr = ""                # what NULL is copied as
paste_null_token = "NULL"          # what means SQL NULL on paste ("" disables it)
paste_null_as_literal = false      # true: the word "NULL" is pasted as text
paste_number_locale = "en"         # en (1,234.56) | de (1.234,56)
paste_date_format = "iso"          # iso | dmy | mdy
paste_max_rows = 5000              # refuse a bigger block instead of staging it
clipboard_read_fallback = false    # allow ctrl+v to read the system clipboard
```

## The SQL panel

`F3` opens a panel under the grid showing the SQL for every pending change, in apply order,
with SQL syntax highlighting. It is the point of the tool: the app exists so you do not have
to *write* SQL, and this is where you can still *see* it.

**Three renderings, one key (`v`).** They are three views of the same statement objects, so
they can never disagree about what the app would do:

| Mode | Shows | Use it to |
|---|---|---|
| **Parameterized** | `UPDATE … SET [Name] = @p0 …` plus a legend (`@p0 = N'Germany'`) | see exactly what is sent to the server |
| **Literal** | the same statement with values inlined and escaped | paste into SSMS, another tool, a ticket |
| **Script** (default) | every statement in `BEGIN TRANSACTION` / `BEGIN TRY … END TRY BEGIN CATCH … END CATCH` / `COMMIT`, with `SET IDENTITY_INSERT` when a script needs it | run the change set by hand, all-or-nothing |

The script rendering is what the panel opens on, because it is the one that is safe to run
without thinking. It sets `XACT_ABORT ON`, rolls back and re-`THROW`s in the `CATCH`, and
guards the rollback with `IF @@TRANCOUNT > 0` so an error *before* the transaction opens
cannot be masked by a second one.

**Copy (`y`).** With no statement selected you get the whole script; with one selected you
get that statement (in parameterized mode, together with its parameter values — a bare
`@p0` is not something you can paste).

**Nothing is executed from here.** The panel is preview-only and says so in its header.
`SET`/`IDENTITY_INSERT` handling and every "generate" action produce *text*; the only thing
in the app that writes is Apply (`ctrl+s`), behind a confirmation.

**"Generate SQL for…" (`g`)** produces a statement for the focused row or for the current
filter, for the cases where you need SQL that is not a pending change:

- `SELECT` / `INSERT` / `UPDATE` / `DELETE` for the focused row;
- `MERGE` — an upsert matched on the primary key, so moving a row between environments
  updates the row that is already there instead of colliding with the key;
- `INSERT script for all rows in this table/filter` — one batched multi-row `INSERT` for
  every loaded row, which is how you move a catalog table to another environment. Computed,
  rowversion and identity columns are left out: the target environment keeps its own.

Only the statements that can be *correct* for the current row are offered, and the reason
for each omission is shown (no key → no `UPDATE`/`DELETE`; no primary key → no `MERGE`).

### Escaping

All of it lives in one place — `SqlDialect.literal()` — and is covered by
`tests/unit/test_dialect_escaping.py`:

- strings → `N'…'` with `'` doubled (the `N` prefix is what survives a non-Unicode
  collation), newlines and control characters kept verbatim;
- `None` → `NULL`, `bool` → `1`/`0`, `bytes` → `0x…`, `Decimal` → plain digits (never
  `1E+3`), dates/times → ISO-8601, `UUID` → its text form;
- an integer outside the `bigint` range and a string containing a NUL are **refused**
  rather than rendered: a literal SQL Server would misread is worse than a clear error.

## Themes

Three built-ins — `default-dark`, `light` and `high-contrast` — switchable at runtime with
`ctrl+t` or from the command palette, and the choice is persisted in `settings.toml`:

```toml
theme = "high-contrast"
fetch_limit = 1000          # other settings from DESIGN §13.2 are read here too
```

Every theme declares the same *semantic* palette (`$pk`, `$fk`, `$identity`, `$computed`,
`$nullable`, `$error`, `$warning`, `$pending`), so the UI never hard-codes a colour.

## Connection profiles

Profiles live in the platformdirs config dir (override with `SWISSKNIFE_CONFIG_DIR`) as
`profiles.toml`. **Passwords are never written to that file** — they live in the OS keyring
(keyring service `sql-table-swiss-knife`, account = `secret_ref`) or are prompted per
session when no keyring backend is available:

```toml
[[profile]]
name = "local"
provider = "mssql"
host = "localhost"
port = 1433
database = "SwissKnifeSample"
auth = "sql"                  # or "integrated" for Windows integrated auth
username = "sa"
secret_ref = "local@localhost"   # keyring account, NOT the password
[profile.options]
encrypt = true
trust_server_certificate = true
driver = "ODBC Driver 18 for SQL Server"
```

A `password`/`pwd` key in this file is rejected at load time.

## Sample SQL Server

A dockerized SQL Server 2022 (Developer) with a catalog-like sample database lives in
`tests/live`:

```bash
cd tests/live
docker compose up -d          # starts SQL Server, then loads init/01_sample_catalog.sql
docker compose down -v        # wipe
```

Connection: `localhost,1433`, user `sa`, password `SwissKnife!2022_Test`
(override with `MSSQL_SA_PASSWORD`), database `SwissKnifeSample`.

Running pyodbc on Linux additionally needs `unixODBC` and the Microsoft ODBC Driver 18.

## `inspect` — verify introspection from the shell

A temporary Milestone-2 command that prints the introspected metadata as Rich tables,
so you can check the `sys.*` layer without launching the TUI:

```bash
uv run sql-table-swiss-knife inspect <profile> dbo.Region     # full metadata
uv run sql-table-swiss-knife inspect <profile> x --list       # tables + row counts
uv run sql-table-swiss-knife inspect <profile> x --databases  # databases
```

It resolves the password from the keyring, prompting if needed. This command is expected
to be reworked or dropped: the TUI inspector shows the same metadata interactively.

## Layout

- `src/sql_table_swiss_knife/domain/` — pure models (no I/O, no driver types)
- `src/sql_table_swiss_knife/providers/` — `DatabaseProvider` + `SqlDialect` protocols,
  SQL generation, plugin registry, and `mssql/` (the SQL Server provider: connection
  string, error mapping, `sys.*` metadata)
- `src/sql_table_swiss_knife/services/` — the layer the TUI talks to: connection
  lifecycle (`services/connection.py`) and catalog reads (`services/catalog.py`)
- `src/sql_table_swiss_knife/storage/` — `profiles.toml` (no secrets), `settings.toml`
  and the secret stores
- `src/sql_table_swiss_knife/tui/` — Textual app: `screens/`, `widgets/`, `theme.py`
  (themes) and `commands.py` (command palette)
- `tests/tui/__snapshots__/` — SVG snapshot tests of the main screens
  (regenerate with `uv run pytest tests/tui/test_snapshots.py --snapshot-update`)
- `tests/live/docker-compose.yml` — sample SQL Server 2022
