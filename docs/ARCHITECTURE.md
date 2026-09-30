# Architecture

The application is a **layered hexagonal** design: one hard rule, one direction of
dependency. `domain` knows nothing about Textual, SQL or the filesystem; everything
above it does. The only way data reaches a database is through a provider implementing
a protocol, and the only way a screen reaches the outside world is through a service.

```
                        ┌───────────────────────────────┐
   terminal  ──────────▶│  tui/      App, Screens,      │  presentation
                        │             Widgets, Theme      │  (Textual)
                        └───────────────┬───────────────┘
                                        │ calls (awaits)
                        ┌───────────────▼───────────────┐
                        │  services/  Connection, Data, │  application
                        │              Changes, Safety, │  logic
                        │              CellView, …      │
                        └──────┬───────────────┬────────┘
                               │               │
              ┌────────────────▼──┐      ┌─────▼──────────────────┐
              │ storage/          │      │ providers/             │  adapters
              │ Profiles, Secrets,│      │ DatabaseProvider       │  (pyodbc, …)
              │ Settings, Audit   │      │ SqlDialect             │
              └───────────────────┘      └────────────────────────┘
                               │               │
                        ┌──────▼───────────────▼────────┐
                        │  domain/   ConnectionProfile, │  the shared
                        │            Table, Column,     │  vocabulary
                        │            PendingChange, …   │
                        └───────────────────────────────┘
                        ┌───────────────────────────────┐
                        │  infra/    Clipboard backends │  the messy world
                        └───────────────────────────────┘
```

`domain/` is the innermost ring and imports nothing from the project but itself. It is
pure data: `Table`, `Column`, `ConnectionProfile`, `PendingChange`, `RowKey`. Every
layer above may know it; it may know none of them. This is what makes the safety rules
and the SQL generation testable without a terminal and without a database.

## The layers

| Package | Owns | Must not |
|---|---|---|
| `domain/` | data structures and the invariants on them (frozen, slots, `__post_init__` checks) | import Textual, SQL, or `services` |
| `providers/` | the DBMS seam: `DatabaseProvider` (I/O) and `SqlDialect` (text) | import `tui`, `storage` or `services` |
| `storage/` | profiles, keyring secrets, settings, the audit log — all file formats | import `tui`; write to a database |
| `services/` | orchestration and decisions: connections, paging, staged changes, validation, safety policy, SQL preview, cell views, clipboard plans | import `tui` |
| `tui/` | screens, widgets, theme, keybindings | contain SQL strings or file I/O |
| `infra/` | clipboard backends and error translation | know about the domain |

### Why the rules are worth stating

Every rule above exists to kill a specific class of bug:

- **`services/` never imports `tui/`** — so a decision ("is this Apply allowed?",
  "what will this paste do?") is answerable in a unit test without a terminal. The safety
  policy has a large test suite and none of it spins up Textual.
- **`tui/` contains no SQL** — the SQL panel renders a string produced by
  `services/sqlpreview.py`, which delegates to the provider's dialect. A screen cannot
  invent SQL, so the SQL you read is the SQL that runs.
- **`domain/` is driver-free** — a new DBMS never touches it, so a provider can be
  written, reviewed and tested against the vocabulary without a running application.

## Where the interesting decisions live

- **`services/safety.py`** — the write posture. Given a policy, a table and a set of
  staged changes, it answers "may this be written, and how hard should confirming it

## The concurrency and I/O story

- Every database call runs in a Textual worker (`AppScreen._run_db`), never on the event
  loop thread, and is cancellable: the screen drops its workers in `on_unmount`.
- Screens that reload data use a **generation counter**. A slow query that finishes after
  the user has moved on is discarded instead of overwriting fresher state.
- **Staged changes survive a lost connection.** A dropped link raises
  `ConnectionResetError`/`OperationalError`, the editor pushes a reconnect prompt that
  says the staged work is safe, and `ChangeService` re-attaches the buffer to the new
  session on reconnect. Losing the network must never lose the user's work.

## The safety model in one diagram

```
        profile (environment, read_only flag)
                     │
                     ▼
            SafetyPolicy.for_profile()   ── forced read-only?  ──▶ always read-only
                     │
        ┌────────────┼─────────────┐
        ▼            ▼             ▼
   read_only    allow_keyless   delete_confirm_
     (bool)      _writes         threshold
        │            │             │
        ▼            ▼             ▼
   check_table()  review()   confirmation_for()
        │            │             │
        └────────────┴────────────┘
                     ▼
              ApplyVerdict
     (allowed / blocked reason / tables / counts)
                     │
                     ▼
              ApplyConfirmScreen   ── typed word when production
                     │
                     ▼
        provider.execute_changes()  ── one transaction
                     │
                     ▼
                AuditLog.record()
```

The header badge reads from the same `SafetyPolicy` that gates the write. That is
deliberate: two objects that could disagree about the environment would eventually
disagree on screen, and a badge that says `DEV` while a production confirmation is
demanded is worse than no badge.

## Testing strategy

| Level | Tool | What it buys |
|---|---|---|
| domain / services / providers | plain `pytest` | decisions tested as pure functions; no terminal, no database |
| TUI | Textual `Pilot` (`run_test`) | the user's actual interactions, driven key by key |
| visuals | `pytest-textual-snapshot` | SVG screenshots; a layout regression is a test failure, and the same SVGs are the README images |
| integration | `pytest -m live` | real SQL Server via `tests/live/docker-compose.yml`; skipped unless `SWISSKNIFE_TEST_DB_URL` is set |

`FakeProvider` implements the full provider protocol against in-memory fixtures, so the
whole application is testable — including Apply, rollback and concurrency conflicts —
without a server.

## Screens

| Screen | Job |
|---|---|
| `ConnectionsScreen` | profile list, connect/test/edit, environment per row |
| `DatabasePickerScreen` | pick a database for a profile that has no default |
| `ProfileEditScreen` | the connection form, with the keyring hint |
| `TableBrowserScreen` | schema tree with PK/FK/trigger/view badges, search-as-you-type |
| `TableEditorScreen` | the workspace: data grid + inspector + SQL panel |
| `CellViewScreen` | expanded view of a long text or binary cell |
| `CellEditorScreen` | in-place cell editing with validation |
| `ColumnPickerScreen`, `QuickFilterScreen` | narrow the grid |
| `PastePreviewScreen` | what a paste will do, before anything is staged |
| `TransferPathScreen` | import/export file chooser |
| `SqlActionScreen` | generate SELECT / INSERT / MERGE / script for a row |
| `LookupPickerScreen` | pick a value for a foreign key |
| `ApplyConfirmScreen` | the last gate: counts, affected tables, typed word |
| `ConfirmScreen`, `HelpScreen` | generic confirmation; the F1 keybinding reference |

  be?". Pure functions over plain values: `check_table`, `review`, `confirmation_for`.
- **`services/changes.py`** — the staging buffer. Edits live here until Apply, with the
  original values needed for optimistic concurrency. Nothing in this module can reach a
  database, which is what makes "nothing is written before Apply" true by construction
  rather than by discipline.
- **`services/cellview.py`** — decides what kind of cell the user is looking at (plain,
  long text, binary) and how to expand it. Separate from the grid so the rule is unit
  tested and the widget stays dumb.
- **`providers/sqlgen.py`** — statement composition shared by all DBMS. The dialect
  supplies quoting, placeholders and literal rendering; `sqlgen` assembles the
  statements. Neither has the other's knowledge, so neither can be wrong in a new DBMS
  way.
- **`tui/keybindings.py` + `tui/keymap.py`** — one registry of documented actions, a
  drift check against the widgets that actually bind keys, and an optional
  `keybindings.toml` override file.
