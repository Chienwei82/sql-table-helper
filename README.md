# sql-table-swiss-knife

A Python 3.14 terminal (TUI) application for **viewing and editing rows of database tables
that have no CRUD UI** — catalog/lookup tables. It stages edits in memory, shows the exact
SQL they will run (parameterized and copy-ready literal), and applies them in a single
transaction. First target DBMS: Microsoft SQL Server.

Status: **Milestone 3** — the main UI: the connection manager, the searchable table
picker, three switchable themes and a command palette. See [PROGRESS.md](PROGRESS.md) for
the full status and [SPEC.md](SPEC.md) / [DESIGN.md](DESIGN.md) for requirements and
design.

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
| `f5` | tables | collapse/expand the schema groups |
| `enter` | tables | open the selected table |
| `ctrl+p` | anywhere | command palette (fuzzy search over the current screen's actions) |
| `ctrl+t` | anywhere | cycle the theme (persisted in `settings.toml`) |
| `esc` | anywhere | back one level |
| `ctrl+q` | anywhere | quit (the connection is closed first) |

Table rows carry glyph badges that survive every theme and colour-vision difference:
`🔑` has a primary key, `🔗` has foreign keys, `⚡` has triggers, `⚠` has **no** primary
key (rows are read-only, S-4) and `👁` is a view.

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
to be reworked or dropped once the TUI inspector (M4) exists.

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
