# DESIGN — sql-table-swiss-knife

Status: **v0.2 — M1 + M2 implemented** (see [PROGRESS.md](PROGRESS.md) for live status)
Companion document: [SPEC.md](SPEC.md) (requirements, milestones, open questions).

> **Tooling: this project uses `uv`.** Every command below is a `uv` command — do not
> `pip install` into the venv or add a `requirements.txt`. `uv` owns the environment,
> the lockfile (`uv.lock`) and the dependency declarations in `pyproject.toml`.

---

## 1. Design principles

1. **The TUI never blocks.** Every DB call, however short, runs off the Textual event loop.
2. **DBMS-specific code lives behind one seam.** Domain models, services and the TUI contain no
   driver imports and no dialect-specific SQL strings; a new DBMS = one new package + one
   registry entry.
3. **One source of truth for SQL.** Preview and execution render the *same* statement objects.
4. **Staged, explicit, transactional.** Nothing touches the database until an explicit Apply;
   Apply is all-or-nothing in one transaction.
5. **Secrets never leave the secret store.** Not into profiles, logs, previews, errors, or
   tracebacks.
6. **Every milestone stays runnable and tested.**

## 2. Architecture — layers

```
┌─────────────────────────────────────────────────────────────────┐
│ tui/            Textual App, Screens, Widgets, viewmodels       │
│                 (pure presentation; talks only to services)     │
├─────────────────────────────────────────────────────────────────┤
│ services/       ConnectionService  CatalogService  DataService  │
│                 ChangeService (staging + Apply)  SqlPreviewSvc  │
│                 ClipboardService  InspectorService              │
├───────────────────────────┬─────────────────────────────────────┤
│ providers/                │ storage/                            │
│  DatabaseProvider Protocol│  profile store (TOML, no secrets)   │
│  + SqlDialect             │  SecretStore (keyring | prompt)     │
│  mssql/ (pyodbc)  [future │  config paths (platformdirs)        │
│  postgres/ mysql/ sqlite/]│                                     │
├───────────────────────────┴─────────────────────────────────────┤
│ domain/         frozen dataclasses: Column, Table, PK, FK,      │
│                 Trigger, Row, PendingChange, ChangeSet…         │
│                 (no I/O, no driver types, importable anywhere)  │
├─────────────────────────────────────────────────────────────────┤
│ infra/          logging + redaction, errors, worker helpers     │
└─────────────────────────────────────────────────────────────────┘
```

Dependency rules (enforced by review + import-linter in CI):

- `domain` imports nothing from this project.
- `tui → services → providers → domain`; `tui` never imports `providers` directly (it receives
  services). `storage` is used only by services.
- Driver modules (`pyodbc`, later `psycopg`, …) are imported **only** inside `providers/<dbms>/`.
- SQL text is produced only by `providers/sqlgen.py` + the provider's `SqlDialect`; services and
  the TUI pass identifiers/values and receive statement objects.

## 3. Folder layout

```
sql-table-swiss-knife/
├── SPEC.md / DESIGN.md / README.md / PROGRESS.md   # PROGRESS = milestone status
├── pyproject.toml            # uv-managed; metadata, deps, ruff/mypy/pytest config
├── uv.lock
├── .python-version           # 3.14
├── src/
│   └── sql_table_swiss_knife/
│       ├── __init__.py
│       ├── __main__.py       # python -m sql_table_swiss_knife
│       ├── cli.py            # entry point: run app, --version, --help
│       ├── domain/
│       │   ├── identifiers.py# SchemaName/TableName/ColumnName (validated value objects)
│       │   ├── catalog.py    # Database, TableSummary, TableMetadata, Column, PK, FK,
│       │   │                 # UniqueConstraint, CheckConstraint, Trigger
│       │   ├── connection.py # ConnectionProfile, AuthMode, ConnectionOptions, ConnectionResult
│       │   ├── rows.py       # Row, RowKey, RowPage, CellValue
│       │   └── changes.py    # ChangeKind, PendingChange, ChangeSet, ApplyResult
│       ├── providers/
│       │   ├── __init__.py   # get_provider(name) registry
│       │   ├── base.py       # DatabaseProvider Protocol, ProviderCapabilities, errors
│       │   ├── dialect.py    # SqlDialect Protocol + literal rendering helpers
│       │   ├── sqlgen.py     # dialect-agnostic INSERT/UPDATE/DELETE builder
│       │   └── mssql/
│       │       ├── provider.py   # pyodbc-backed implementation (async wrappers)
│       │       ├── dialect.py    # TSqlDialect: [brackets], N'' literals, OUTPUT clause
│       │       ├── metadata.py   # sys.* / information_schema catalog queries
│       │       └── errors.py     # SQLSTATE/vendor error → provider error mapping
│       ├── services/
│       │   ├── connection.py # profile lifecycle, connect/test/disconnect
│       │   ├── catalog.py    # databases, tables/views, metadata cache
│       │   ├── data.py       # fetch rows, fetch-more, refresh
│       │   ├── changes.py    # staging operations + Apply orchestration
│       │   ├── preview.py    # statement → parameterized / literal renderings
│       │   └── clipboard.py  # TSV/CSV/JSON encode & parse, paste planning
│       ├── storage/
│       │   ├── profiles.py   # TOML profile persistence (secret_ref only)
│       │   ├── secrets.py    # SecretStore: KeyringSecretStore | EphemeralSecretStore
│       │   └── paths.py      # platformdirs config/data/log locations
│       ├── tui/
│       │   ├── app.py        # SwissKnifeApp, screen stack, global bindings, themes
│       │   ├── screens/      # home (profiles), profile_edit, database_picker,
│       │   │                 # table_picker, grid (workspace)
│       │   ├── widgets/      # data_grid, sql_preview, inspector, pending_badge, status
│       │   └── themes/       # *.tcss: default, dark-contrast, light + user override
│       └── infra/
│           ├── logging.py    # setup + RedactingFilter (DESIGN §8)
│           └── errors.py     # AppError hierarchy, user-facing message mapping
├── tests/
│   ├── conftest.py           # fixtures: FakeProvider, profile store, Pilot harness
│   ├── unit/                 # domain, sqlgen, dialect, changes, clipboard, redaction
│   ├── providers/            # provider contract tests; live tests (env-gated)
│   └── tui/                  # Textual Pilot end-to-end flows
└── docs/
    └── add-a-database.md     # checklist for new DBMS providers
```

Entry point: console script `sql-table-swiss-knife` (short alias `stsk` — OQ-2) →
`sql_table_swiss_knife.cli:main`.

## 4. Domain model

All models are `@dataclass(frozen=True, slots=True)` (OQ-3), stdlib-only, no I/O, no driver
types. Names are value objects; validation happens at construction (e.g. non-empty, no NUL).

### 4.1 Connection

```python
class AuthMode(Enum):
    SQL = "sql"  # username + password (secret via SecretStore)
    INTEGRATED = "integrated"  # Windows integrated auth; no secret


@dataclass(frozen=True, slots=True)
class ConnectionOptions:
    encrypt: bool = True
    trust_server_certificate: bool = False
    connect_timeout_s: int = 5
    driver: str = "ODBC Driver 18 for SQL Server"  # provider-specific default
    application_intent: str | None = None  # e.g. "ReadOnly" (hint, optional)


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    name: str  # display name; also the keyring account
    provider: str  # registry key: "mssql" | "postgres" | ...
    host: str
    port: int = 1433
    database: str | None = None  # default database; None = ask/pick at connect
    auth: AuthMode = AuthMode.SQL
    username: str | None = None  # None for INTEGRATED
    secret_ref: str | None = None  # keyring account ref; NEVER the password
    options: ConnectionOptions = ConnectionOptions()


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    server_version: str
    server_name: str
    database: str
    latency_ms: int
    auth_used: AuthMode
```

### 4.2 Catalog

```python
class TableKind(Enum):
    BASE_TABLE = "table"
    VIEW = "view"


class ReferentialAction(Enum):  # FK ON DELETE / ON UPDATE
    NO_ACTION = "NO ACTION"
    CASCADE = "CASCADE"
    SET_NULL = "SET NULL"
    SET_DEFAULT = "SET DEFAULT"


@dataclass(frozen=True, slots=True)
class Database:
    name: str
    is_current: bool = False


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    ordinal: int
    data_type: str  # provider type name: "nvarchar", "decimal", "int"…
    max_length: int | None  # bytes (sys) or chars; None = not applicable/AX
    precision: int | None
    scale: int | None
    nullable: bool
    default_definition: str | None  # raw DEFAULT expression text (parentheses stripped)
    is_identity: bool
    identity_seed: int | None = None
    identity_increment: int | None = None
    is_computed: bool = False
    computed_definition: str | None = None
    is_rowversion: bool = False
    is_primary_key: bool = False
    collation: str | None = None
    is_foreign_key: bool = False
```

### 4.3 Keys, constraints, triggers

```python
@dataclass(frozen=True, slots=True)
class PrimaryKey:
    name: str | None  # unnamed PKs possible
    columns: tuple[str, ...]  # ordered


@dataclass(frozen=True, slots=True)
class ForeignKey:
    name: str
    columns: tuple[str, ...]  # local columns, ordered
    referenced_schema: str
    referenced_table: str
    referenced_columns: tuple[str, ...]
    on_delete: ReferentialAction
    on_update: ReferentialAction


@dataclass(frozen=True, slots=True)
class UniqueConstraint:
    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CheckConstraint:
    name: str
    definition: str  # raw expression, display only (never executed)


@dataclass(frozen=True, slots=True)
class Trigger:
    name: str
    events: tuple[str, ...]  # ("INSERT", "UPDATE", "DELETE")
    firing: str  # "AFTER" | "INSTEAD OF"
    enabled: bool


@dataclass(frozen=True, slots=True)
class TableSummary:  # what the picker needs before full metadata load
    schema: str
    name: str
    kind: TableKind
    has_primary_key: bool


@dataclass(frozen=True, slots=True)
class TableMetadata:
    schema: str
    name: str
    kind: TableKind
    columns: tuple[Column, ...]
    primary_key: PrimaryKey | None
    foreign_keys: tuple[ForeignKey, ...]
    unique_constraints: tuple[UniqueConstraint, ...]
    check_constraints: tuple[CheckConstraint, ...]
    triggers: tuple[Trigger, ...]
    approximate_row_count: int | None = None  # cheap estimate; None if unknown/n/a

    def column(self, name: str) -> Column: ...  # KeyError → AppError
    @property
    def updatable(self) -> bool: ...  # see S-4: key present, base table
```

`TableMetadata.updatable` is `True` only for base tables with a PK or a single-column
non-nullable unique constraint usable as a row identity (see §7). Views are read-only in v1
(OQ-6).

## 5. Provider abstraction

Two interfaces keep DBMS-specifics behind one seam: `DatabaseProvider` (connect, introspect,
read, write) and `SqlDialect` (how SQL text is built and literals rendered). Both are
`typing.Protocol`s (OQ-4) — implementations are plain classes.

```python
# providers/base.py
@runtime_checkable
class DatabaseProvider(Protocol):
    """All methods are async; implementations run their sync driver in a worker thread."""

    @property
    def capabilities(self) -> ProviderCapabilities: ...
    @property
    def dialect(self) -> SqlDialect: ...

    async def test_connection(
        self, profile: ConnectionProfile, password: str | None
    ) -> ConnectionResult: ...
    async def connect(
        self, profile: ConnectionProfile, password: str | None
    ) -> "ActiveConnection": ...
    async def close(self, conn: "ActiveConnection") -> None: ...
    async def list_databases(self, conn: "ActiveConnection") -> list[Database]: ...

    async def list_tables(
        self, conn: "ActiveConnection", schema: str | None = None
    ) -> list[TableSummary]: ...
    async def get_table_metadata(
        self, conn: "ActiveConnection", schema: str, name: str
    ) -> TableMetadata: ...

    async def fetch_rows(
        self,
        conn: "ActiveConnection",
        table: TableRef,
        *,
        limit: int,
        after_key: RowKey | None,
        order_by_pk: bool,
    ) -> RowPage: ...
    async def apply_changes(
        self, conn: "ActiveConnection", changes: ChangeSet, *, statements: list[SqlStatement]
    ) -> ApplyResult: ...


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    supports_schemas: bool
    supports_integrated_auth: bool
    supports_rowversion: bool
    supports_output_clause: bool  # INSERT ... OUTPUT (fetch generated keys)
    read_only_views: bool = True


# providers/dialect.py
@runtime_checkable
class SqlDialect(Protocol):
    name: str

    def quote_ident(self, ident: str) -> str: ...  # [bracket] / "double"
    def placeholder(self, index: int) -> str: ...  # @p0, %s, ?
    def literal(self, value: SqlLiteralValue) -> str: ...  # N'...', TRUE, 0x..
    def limit_clause(self, limit: int, offset: int | None) -> str: ...


# providers/sqlgen.py — dialect-agnostic builder producing statement objects
@dataclass(frozen=True, slots=True)
class SqlStatement:
    kind: ChangeKind  # INSERT | UPDATE | DELETE
    table: TableRef
    sql_parametrized: str  # "... VALUES (@p0, @p1)" — sent to the driver
    params: tuple[SqlParam, ...]  # name, python value, column (for the legend)
    sql_literal: str  # copy-ready form with escaped literals
    sql_script: str  # literal form + ';' (for the full script)
    row_key: RowKey | None


def build_insert(dialect, meta, values) -> SqlStatement: ...
def build_update(dialect, meta, key, old_values, new_values) -> SqlStatement: ...
def build_delete(dialect, meta, key, old_values) -> SqlStatement: ...
```

`ActiveConnection` is an opaque handle owned by the provider (driver connection + profile +
generation token); services never inspect it.

### 5.1 Connection lifecycle & threading

- pyodbc is synchronous and its connections are not safe to share across threads. Design:
  **one provider-owned connection per active session, always used from the same dedicated
  worker thread**, serialized by an `asyncio.Lock` at the service layer (the TUI is
  single-user; contention is rare). `asyncio.to_thread()` (or Textual
  `run_worker(thread=True)`) performs the offload so the event loop never blocks.
- Fetch operations create a short-lived read cursor on the same connection; Apply runs on the
  same connection so the transaction cannot interleave with unrelated work.
- Cancellation: cooperative — a generation token is checked after each await; superseded
  results are dropped (driver-level query cancellation is out of scope for v1).
- Disconnect/screen change closes the connection deterministically in `on_unmount`.

### 5.2 Errors

```python
class ProviderError(AppError): ...


class ConnectError(ProviderError): ...  # network/driver/DNS


class AuthError(ConnectError): ...  # login failed → re-prompt (no raw message dump)


class MetadataError(ProviderError): ...


class QueryError(ProviderError):  # runtime SQL error
    sqlstate: str | None
    vendor_code: int | None  # e.g. SQL Server error number
    statement: SqlStatement | None  # which statement failed (preview linkage)


class ApplyError(QueryError):
    rolled_back: bool  # always True in v1 (all-or-nothing)
    failed_index: int  # index into the statement list
```

`mssql/errors.py` maps `pyodbc.Error`: login failures → `AuthError`, timeouts (`HYT00`) →
`ConnectError`, constraint violations (`23xxx`) → `QueryError` with the constraint name parsed
from the message. User-facing text is sanitized: message + statement, **never** the connection
string or password.

### 5.3 Adding a new DBMS (docs/add-a-database.md checklist)

1. Create `providers/<dbms>/` with `provider.py`, `dialect.py`, `metadata.py`, `errors.py`.
2. Implement `DatabaseProvider` + `SqlDialect`; declare `ProviderCapabilities`.
3. Map catalog introspection to the domain models (e.g. `pg_catalog` vs `sys.*`).
4. Register in `providers/__init__.py`: `PROVIDERS = {"mssql": ..., "postgres": ...}`.
5. Run the shared **provider contract test suite** (`tests/providers/test_contract.py`) — it
   asserts DBMS-independent semantics (metadata completeness, fetch paging, transactional
   apply).
6. Add dialect literal tests + a live, env-gated integration job.

No changes in `domain/`, `services/`, or `tui/` should be required.

## 6. SQL generation

All DML text is built by `providers/sqlgen.py` through the active `SqlDialect`. Rules:

- **INSERT** — explicit column list (never `SELECT *` style); omitted columns (identity,
  computed, rowversion, columns with defaults the user didn't fill) rely on server defaults.
  With a rowversion column, `OUTPUT INSERTED.[<pk>]` (capability `supports_output_clause`)
  returns the generated key so the grid can key the new row; otherwise the row is re-fetched
  after commit.
- **UPDATE** — sets only *changed* columns; `WHERE` = full row identity (PK columns, or the
  fallback unique constraint) bound as parameters. If the table has a rowversion column it is
  added to the `WHERE` as `AND [rv] = @old_rv` → SQL Server raises the conflict via rowcount 0.
- **DELETE** — `WHERE` = row identity (+ rowversion guard as above).
- **Views** — no DML generated (read-only, OQ-6).
- **Ordering inside Apply** — statements are executed DELETE → UPDATE → INSERT, each group in
  the user's staging order (child-row deletes first typically satisfies FKs). Known v1
  limitation: complex self-referencing FK graphs may require the user to order changes; the
  error message on FK violation names the constraint.

Parameterized vs literal (FR-5) come from the same `SqlStatement`:

```sql
-- as sent (parameterized):
UPDATE [dbo].[Country] SET [Name] = @p0 WHERE [Code] = @p1 AND [RowVer] = @p2
-- copy-ready (literal):
UPDATE [dbo].[Country] SET [Name] = N'Germany' WHERE [Code] = N'DE' AND [RowVer] = 0x00000000000007D3
```

Literal rendering (TSqlDialect): strings → `N'…'` with `''` escaping; `None` → `NULL`;
datetimes → ISO-8601 with explicit style; `Decimal` → plain number; `bool` → `1/0`;
`bytes` → `0x…`; `uuid` → `'…'`. The literal renderer is display-only — execution always
uses parameters.

Fetch paging: `SELECT … FROM t ORDER BY <pk> OFFSET @o ROWS FETCH NEXT @n ROWS ONLY` when a
PK exists (keyset via `WHERE pk > @after_key` in a later milestone if offset gets slow);
otherwise `TOP (@n)` with a deterministic tie-broken order.

## 7. Change tracking

### 7.1 Domain model

```python
class ChangeKind(Enum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


RowKey = tuple[tuple[str, object], ...]  # (("Code","DE"),) — ordered identity columns


@dataclass(frozen=True, slots=True)
class PendingChange:
    kind: ChangeKind
    key: RowKey | None  # None for INSERT (assigned on apply)
    table: TableRef
    before: Mapping[str, object] | None  # fetched original values (UPDATE/DELETE)
    after: Mapping[str, object] | None  # staged values (INSERT/UPDATE)


@dataclass(slots=True)
class ChangeSet:  # ordered, deduplicated staging area
    changes: list[PendingChange]  # append order; one entry per touched row

    def stage_cell(self, row, column, new_value) -> None: ...
    def stage_insert(self, values) -> RowKey: ...
    def stage_delete(self, row) -> None: ...
    def revert(self, key) -> None: ...  # one change/row
    def clear(self) -> None: ...
    @property
    def counts(self) -> dict[ChangeKind, int]: ...
    def statements(self, meta, dialect) -> list[SqlStatement]: ...  # pure, preview == execute
```

Grid rows keep `original` + `staged` value maps; `ChangeSet` is the authoritative list shown
in the pending badge and SQL preview. Editing a cell twice collapses to one `UPDATE`;
editing a staged INSERT row updates the INSERT; deleting an unstaged row after editing it
reverts to the original; deleting a previously-existing staged row yields DELETE (update
against it is dropped).

### 7.2 Apply algorithm (ChangeService.apply)

1. Snapshot the ChangeSet; build `statements = changeset.statements(...)`.
2. Show confirmation dialog with counts + preview (FR-7.5); abort leaves staging untouched.
3. Acquire the connection lock; `BEGIN TRAN` (autocommit off on the driver connection).
4. Execute statements in order (§6); on any error → `ROLLBACK`, raise `ApplyError` with
   `failed_index`; staging preserved; UI highlights the failing statement in the preview.
5. On success → `COMMIT`, record `ApplyResult(statements, timings)` for FR-5.5, clear
   staging, schedule a refresh of affected rows (or full re-fetch when identity reassignment
   happened).
6. Optimistic concurrency (FR-7.8): rowcount ≠ 1 on UPDATE/DELETE (or SQL Server RAISERROR
   from an UPDATE with rowversion guard) → treated as conflict → rollback with a "row changed
   by someone else — refresh and retry" message.

## 8. Security design

### 8.1 Secret storage

```python
class SecretStore(Protocol):
    def get(self, ref: str) -> str | None: ...
    def set(self, ref: str, secret: str) -> None: ...
    def delete(self, ref: str) -> None: ...
    @property
    def persistent(self) -> bool: ...  # False → "prompt each session" mode
```

- `KeyringSecretStore` — `keyring` package; service name `sql-table-swiss-knife`, account =
  `secret_ref` (profile name + host). Used when `keyring.get_keyring()` is functional
  (backend != fail backend).
- `EphemeralSecretStore` — in-memory dict for the session when no keyring exists (headless
  Linux without a wallet, first run). Password prompted on connect; never written anywhere.
- Profile save: "Remember password?" is only offered when `persistent`; choosing No stores
  `secret_ref` anyway (for prompt lookup) but with no secret.
- Integrated auth: no secret object exists at all; connection string uses
  `Trusted_Connection=yes` / `Authentication=ActiveDirectoryIntegrated` (per options).

### 8.2 Redaction (infra/logging.py)

- One `RedactingFilter` on every handler; applies regexes to the *formatted message and args*:
  `(?i)(password|pwd|pwd=)=[^;\s]+`, `(?i)uid=[^;]+`, `Trusted_Connection=[^;]+`, plus a
  dynamic mask for the actual password string of the active session (belt and braces).
- A `sanitize_connection_string()` helper is the only function allowed to see a full
  connection string; it returns `Driver=…;Server=…;Database=…;UID=…;PWD=***`.
- Exceptions crossing into the TUI pass through `infra.errors.user_message()` which redacts
  the same patterns; raw tracebacks go only to the log file (already redacted).
- Tests: unit tests feed known-secret strings through logging, exceptions, and the preview
  pipeline and assert the secret never appears in captured output (SPEC S-sec-3).

### 8.3 Files

`profiles.toml` example (config dir via platformdirs, mode 0600):

```toml
[[profile]]
name = "catalog-prod"
provider = "mssql"
host = "sql01.corp.local"
port = 1433
database = "CatalogDB"
auth = "integrated"          # or "sql" + username + secret_ref
secret_ref = "catalog-prod@sql01.corp.local"
[profile.options]
encrypt = true
trust_server_certificate = false
driver = "ODBC Driver 18 for SQL Server"
```

## 9. TUI design

### 9.1 Screen stack

```
HomeScreen (connection manager)
  → ProfileEditScreen (modal)         create/edit profile, Test Connection
  → DatabasePickerScreen (modal)      if profile has no default database / change DB
  → TablePickerScreen                 search-as-you-type + schema groups
  → GridScreen (workspace)            the main editing surface
```

`GridScreen` layout (Textual CSS dock/panel):

```
┌──────────────────────────────────────────────────────────────┐
│ header: profile › db › schema.table   [RW|RO] ⚙?  pending: 3 │
├───────────────────────────────────────┬──────────────────────┤
│                                       │ right panel (tabs)   │
│        DataGrid (virtualized)         │  SQL preview         │
│        frozen header row              │  Inspector (table/   │
│        staged rows styled FR-3.7      │  column)             │
│                                       │                      │
├───────────────────────────────────────┴──────────────────────┤
│ status: rows 1–1000 • more? • conn OK • last apply 12:04:11  │
└──────────────────────────────────────────────────────────────┘
```

### 9.2 Keybindings (v1)

| Keys | Action |
|---|---|
| `?` | help / keybinding reference |
| `q`, `ctrl+c` | quit (confirm if pending changes exist) |
| `ctrl+k` / `f4` | command palette / table picker |
| arrows, `hjkl`, `tab` | navigate cells |
| `shift+arrows`, `v` (toggle), mouse drag | selection |
| `enter`, `dblclick` | edit cell (staged) |
| `ins`, `ctrl+ins` | stage new row / stage delete row |
| `ctrl+z` | revert focused change; `ctrl+shift+z`? → discard all (with confirm) |
| `ctrl+c` (no selection conflict: `y` in visual) | copy cell/row/selection as TSV |
| `ctrl+v` | paste (cell / fill / insert rows per FR-4) |
| `p` | cycle copy format TSV→CSV→JSON |
| `ctrl+enter` / `a` | apply (confirmation dialog) |
| `ctrl+shift+x` | discard all staged changes (confirm) |
| `i` | toggle inspector; `s` toggle SQL preview panel |
| `r` | refresh rows (keeps staging; conflicts flagged) |
| `F5` | read-only mode toggle (S-9) |

(Exact bindings finalized in M1's help overlay; the table is the working contract for Pilot
tests.)

### 9.3 Workers & reactivity

- All service calls run in Textual workers: `@work(thread=True)` for provider calls, async
  workers for orchestration. Messages flow back via `call_from_thread` / posted events.
- Each screen owns a `generation` int; worker results tagged with the generation are dropped
  if stale (navigated away / re-fetched) — prevents wrong-table flashes (FR-3.9).
- UI states are explicit: `loading` (spinner), `ready`, `error(message, retry_action)`,
  `disconnected(reconnect_action)`.

### 9.4 Theming & the grid widget

- `DataGrid` wraps Textual's virtualized `DataTable` with a staged-value overlay model:
  display = staged ?? original; style class per state (FR-3.7) + glyph markers
  (`~` edited, `+` new, `×` delete, `🔒` read-only).
- Themes are Textual CSS files in `tui/themes/`; 3 built-ins (`default-dark`, `high-contrast`,
  `light`); user override loaded from the config dir. State colors are declared as CSS
  variables per theme.

### 9.5 Clipboard flows

- **Copy:** encode via `services/clipboard.py` → `App.copy_to_clipboard(text)` (OSC 52).
  Optional fallback: if the terminal ignores OSC 52 (detected by config flag, not by probe),
  offer "show in popup to copy manually".
- **Paste:** Textual `Paste` event carries the bracketed-paste payload (multi-line TSV from
  Excel arrives intact) → `services/clipboard.plan_paste(...)` → grid applies cell-fill or
  row-insert plan after validation. Configurable fallback reads the system clipboard
  (`pyperclip`, optional extra) when the terminal lacks bracketed paste.

## 10. Tech choices & driver verification report

Your proposed stack was validated on this machine (2026-09-30, Python 3.14.4, uv 0.12.5).

### 10.1 Stack decisions

| Area | Choice | Version verified | Justification |
|---|---|---|---|
| TUI | **Textual** | 8.2.8 (requires ≥3.9,<4) | Mature TUI framework: virtualized `DataTable` (scrolls large tables), screens/modals, CSS theming (FR-3.7), workers (FR-10), mouse + keyboard, `Pilot` test harness built in. Import + `run_test`/`copy_to_clipboard`/`Paste` verified on 3.14.4. |
| Rich text | **Rich** | 15.0.0 | Textual's rendering foundation; also used standalone for pretty SQL/error rendering in logs and popups. |
| Project mgmt | **uv** | 0.12.5 | Already installed; fast locked builds (`uv.lock`), `uv run` for reproducible CI, `.python-version` pins 3.14. |
| Tests | **pytest + pytest-asyncio + Textual Pilot** | 9.1.1 / 1.4.0 verified | pytest-asyncio drives async services; `App.run_test()` + Pilot gives deterministic TUI tests without a real terminal. |
| Lint/types | **ruff + mypy --strict** | via uv | One fast tool for lint/format (ruff) + static safety net (strict mypy). Protocols fit mypy strictly; `py.typed` shipped. |
| Secrets | **keyring** | latest | OS-native credential stores (Credential Manager/Keychain/Secret Service); graceful fallback to prompt-per-session (FR-1.6). |
| Clipboard (optional) | **pyperclip** | optional extra | Only as fallback for reading the system clipboard; primary paths are OSC 52 (copy) and bracketed paste (paste), which are dependency-free. |
| Config | **tomllib** (stdlib) + **platformdirs** | stdlib 3.14 | No YAML/TOML dep; platformdirs gives correct per-OS config dirs. |
| SQL Server driver | **pyodbc** (recommended) | 5.3.0 — see 10.2 | Full cp314 wheel matrix; PEP 249 standard; ecosystem default. |

Deliberately **not** used: SQLAlchemy (we generate a tiny, controlled SQL surface — an ORM or
query builder adds abstraction we'd fight for grid semantics), asyncpg-style native async
drivers (SQL Server has no mainstream native-async Python driver), pydantic (OQ-3).

### 10.2 Driver evaluation: pyodbc vs Microsoft mssql-python — VERIFIED

Evidence gathered 2026-09-30 (PyPI JSON API for release files, GitHub releases/issues, plus a
live install test on this machine's Python 3.14.4):

**pyodbc 5.3.0** (released 2025-10-17, `requires-python >=3.9`, 64 release files)
- Release notes: *"Version 5.3.0 has been released with wheels for versions 3.9 – 3.14"*
  (adds Python 3.14 support incl. wheels, PR #1445 era; issue #1444 was the 3.14-wheel request).
- **18 cp314 wheels**: `win32`, `win_amd64`, `win_arm64`, macOS x86_64 + arm64, manylinux
  x86_64 + aarch64, musllinux x86_64 + aarch64 — **each for both `cp314` and free-threaded
  `cp314t`**.
- Live test here: `uv venv --python 3.14` + `uv pip install pyodbc` → wheel resolved and
  installed. `import pyodbc` failed *on this Linux box only* with
  `libodbc.so.2: cannot open shared object file` — i.e. the wheel is fine; **a system ODBC
  driver manager (unixODBC) + the Microsoft ODBC Driver for SQL Server must be installed** on
  Linux/macOS (on Windows the DM is built into the OS; only "ODBC Driver 18 for SQL Server"
  needs installing).

**mssql-python 1.15.0** (released 2026-09-11, `requires-python >=3.10`, 34 release files)
- **7 cp314 wheels**: macOS universal2, manylinux x86_64/aarch64, musllinux x86_64/aarch64,
  win_amd64, win_arm64. **No `cp314t` (free-threaded) wheels; no 32-bit Windows wheels.**
- Live test here: installs **and imports cleanly** on 3.14.4 with zero system dependencies —
  it vendors its ODBC layer (pulled `mssql-python-odbc 18.6.2.1`), true `pip install` story.
- **Packaging history risk:** v1.7.0 (2026-05-16) shipped *only* cp311/cp312 wheels — no
  cp313/cp314 — breaking installs on 3.13/3.14 (GitHub microsoft/mssql-python#588; downstream
  projects like dbt-fabric had to pin `<1.7.0`). The issue is closed and current releases carry
  cp314 again, but a 3.14-first project already lived through one 3.14 wheel gap.
- Positioning: Microsoft's official driver (GA, DDBC/pybind11 core, bundles drivers, claims
  cross-platform consistency and no separate ODBC install).

**Recommendation: pyodbc for v1** — broadest verified 3.14 wheel coverage (including
free-threaded), mature PEP 249 implementation, it is what SQLAlchemy/DBT/DuckDB-style tooling
expects, and no vendor-specific packaging surprises. Costs: system prerequisite
(`unixodbc` + `msodbcsql18` on Linux/macOS; ODBC Driver 18 installer on Windows) — documented
in README with exact package names per OS.

mssql-python stays a **documented alternative**: the `DatabaseProvider` seam means it can be
added as a second `providers/mssql/` backend (or a driver switch inside it) without touching
services/TUI — see OQ-1. If the "no system dependencies at all" story matters more to you than
maturity/coverage, say so and we flip the default before M2.

## 11. Testing strategy

| Layer | Tooling | What is tested |
|---|---|---|
| Domain (`domain/`) | pytest (sync) | identifier validation, ChangeSet collapse/dedupe rules, row keys, model invariants |
| SQL generation (`providers/sqlgen.py`, dialects) | pytest, no DB | parameterized + literal text for INSERT/UPDATE/DELETE across value types (str/unicode/quote/NULL/date/decimal/binary/uuid), WHERE identity building, rowversion guard, ordering (D→U→I) |
| Services | pytest-asyncio + FakeProvider | staging flows, Apply orchestration incl. rollback-on-error sequencing, fetch/paging, clipboard plan parsing (TSV/CSV/JSON battery) |
| Provider contract | pytest, parametrized | same suite against FakeProvider always; against live SQL Server when `SWISSKNIFE_TEST_DB_URL` is set (skipped otherwise): metadata completeness vs known schema, paging, transactional apply/rollback |
| TUI | Textual `run_test()` + Pilot | screen flows (home → picker → grid), search-as-you-type filtering, cell edit staging visuals, dialogs (apply confirm, discard), paste flows, help overlay, quit-with-pending guard |
| Security | pytest | redaction through logging/exceptions/previews (planted secrets must never appear), profile files never contain secrets, keyring fallback behavior (mocked backend) |
| Static | ruff check/format, mypy --strict, import-linter | NFR-1 gates, layer dependency rules (§2) |

Principles:

- **No live DB in the default test run** — CI is deterministic; live tests are opt-in via
  env var (OQ-10) and run in a scheduled/nightly job with the docker-compose SQL Server.
- FakeProvider implements the full `DatabaseProvider` Protocol with in-memory tables,
  scripted failures (connection drop, constraint violation, 0-rowcount conflict) and records
  of begin/execute/commit/rollback calls — this is how Apply semantics are proven without a
  server.
- Pilot tests avoid wall-clock sleeps: `pilot.pause()`/`wait_for` on messages; workers are
  deterministic under `run_test`.
- Coverage target: ≥90% on `domain/`, `providers/sqlgen.py`, `services/`; the TUI layer is
  covered by flow tests rather than line quotas.

## 12. Error handling & logging

- `infra.errors.AppError` hierarchy (§5.2) is the only error type services raise; providers
  translate driver exceptions at the boundary. The TUI never sees raw `pyodbc.Error`.
- Each screen implements the four UI states (§9.3); errors render as a dismissible toast +
  status-bar entry with a retry action where meaningful (connect, fetch, apply).
- Logging: single app log (`platformdirs` user log dir, 0600, rotating), `INFO` default,
  `--debug` CLI flag → `DEBUG` (SQL text logged *parameterized only*, values at DEBUG are
  redacted per §8.2 unless `--debug-sql-values` is passed — never passwords either way).
- Apply results and failures are recorded in the log with statement counts, durations and the
  failing statement index — correlatable with the on-screen preview.

## 13. Configuration, dependencies & CLI

### 13.1 Dependencies (pyproject)

```toml
[project]
name = "sql-table-swiss-knife"
requires-python = ">=3.14"
dependencies = [
  "textual>=8.2",
  "rich>=15",
  "pyodbc>=5.3.0",        # OQ-1 recommendation
  "keyring>=25",
  "platformdirs>=4",
]

[project.optional-dependencies]
clipboard = ["pyperclip>=1.9"]     # optional system-clipboard read fallback
mssql-alt = ["mssql-python>=1.15"] # optional alternative driver (future, OQ-1)

[project.scripts]
sql-table-swiss-knife = "sql_table_swiss_knife.cli:main"
stsk = "sql_table_swiss_knife.cli:main"

[dependency-groups]
dev = ["pytest>=9", "pytest-asyncio>=1.4", "pytest-cov>=7",
       "ruff>=0.14", "mypy>=1.19", "import-linter>=2.3"]

[tool.mypy]            # strict everywhere; per-module overrides only if forced
strict = true
[tool.ruff]            # line-length 100, target py314, isort-style rules
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

### 13.2 User settings (`settings.toml` in the platformdirs config dir)

```toml
fetch_limit = 1000
copy_null_repr = ""            # "" | "NULL"
delete_confirm_threshold = 50
theme = "default-dark"
clipboard_read_fallback = false   # enable pyperclip read path
rowcount_advisory_above = 50000
```

### 13.3 CLI

`sql-table-swiss-knife [--profile NAME] [--read-only] [--debug] [--debug-sql-values]
[--version]` — no flags launches the Home screen. `--read-only` forces S-9 globally for the
session (useful when demoing against production).

## 14. Summary & next steps

- SPEC.md: requirements (FR-1…FR-10, S-1…S-11), NFRs, milestones M1–M8, OQ-1…OQ-11,
  assumptions.
- DESIGN.md (this file): layers, folder layout, domain model, provider + dialect Protocols,
  SQL generation, change tracking/Apply algorithm, security design, TUI design, verified tech
  choices with the pyodbc/mssql-python wheel report, testing, errors/logging, config.
- **Status: M1 and M2 are implemented.** Milestone-by-milestone status lives in
  [PROGRESS.md](PROGRESS.md), which is updated and committed at the end of each milestone
  (see §15). Next up: M3 (connection manager & table picker).

---

## 15. Development workflow

### 15.1 `uv` is the only toolchain

`uv` owns the environment, the lockfile and dependency resolution. Never `pip install`
into `.venv`, never add a `requirements.txt`.

| Task | Command |
|---|---|
| create/sync the environment | `uv sync` |
| add a runtime dependency | `uv add <pkg>` (updates `pyproject.toml` **and** `uv.lock`) |
| add a dev dependency | `uv add --dev <pkg>` |
| remove a dependency | `uv remove <pkg>` |
| run something in the env | `uv run <cmd>` (e.g. `uv run pytest`, `uv run mypy`) |
| run the app | `uv run sql-table-swiss-knife` |
| upgrade the lockfile | `uv lock --upgrade` |

`uv run` works whether or not a venv has been created — it syncs on demand. The
repository's virtual environment is `.venv/` (git-ignored).

### 15.2 Quality gates (NFR-1)

Run all four before every commit; CI runs the same four:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy            # strict, files = ["src", "tests"]
uv run pytest
```

### 15.3 Milestone cadence

1. Pick the milestone from SPEC.md §6 and read its bullet list — it is the acceptance
   criteria.
2. Implement; every milestone ships with tests (NFR-5). Core logic must be testable
   *without* a database via `FakeProvider`; DB-specific behaviour additionally gets an
   env-gated `live` test.
3. Run the four gates.
4. **Update `PROGRESS.md`**: milestone table, what was delivered, new tests, and — honestly
   — the known gaps left behind.
5. Commit the milestone: `git commit` with a message naming the milestone
   (`Milestone N: …`).

### 15.4 Git

The repository is local-only (no remote configured). One commit per milestone keeps the
history readable and makes `git bisect` useful across a long build.
