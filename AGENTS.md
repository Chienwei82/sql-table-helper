# AGENTS.md

Instructions for AI coding agents (and humans) working in this repository.

**Read this before changing anything.** The architecture here is enforced mechanically, so
an import that "looks harmless" will fail the build. The rules below are not stylistic
preferences — breaking one breaks `lint-imports`, `mypy` or a test.

- Project: `sql-table-manager` (distribution) / `sql_table_swiss_knife` (import
  package), in a repo directory named `sql-table-helper`. **Import with the underscore
  name**, always.
- Human-facing docs: [README.md](README.md). Requirements: [SPEC.md](SPEC.md). Design
  rationale: [DESIGN.md](DESIGN.md). Status: [PROGRESS.md](PROGRESS.md).

## The one-paragraph version

A Python 3.14 Textual TUI for viewing and editing rows of database tables that have no
CRUD UI (catalog/lookup tables). Edits are staged in memory, the exact SQL is shown, and
Apply runs everything in one transaction. The app's central promise is that a non-SQL
author can maintain a catalog table, and a DBA can still read exactly what will run.

## Two rules about believing your own tests

Both defect classes fixed on this branch were caught by the *same* mistake, so they are
written down here rather than left in a commit message.

1. **A test that bypasses the layer where a bug lives cannot find it.** The unit tests
   built `Column` objects by hand with `max_length` in **bytes**, so they agreed with the
   `nvarchar` double-halving and stayed green while every Unicode column in the app was
   capped at half its real width. Where a value crosses a unit boundary (bytes ↔
   characters, cents ↔ units, local ↔ UTC), test the boundary, not the type.
2. **A snapshot that records a bug is worse than no snapshot.** Six TUI SVGs had
   `nvarchar(100)` baked in for a `nvarchar(200)` column. They agreed with each other and
   with the buggy code, so agreement proved nothing. When you regenerate a snapshot, diff
   the *rendered text*, not the SVG bytes — a wider label reflows the whole file and a
   539-line diff hid a one-token change.

Corollary, both hit here: **a fixture that only passes on a fresh container is not a
fixture.** The live suite passed once and then poisoned its own next run. Run it twice.

## Non-negotiable rules

1. **`uv` only.** Never `pip install` into `.venv`; there is no `requirements.txt`. Use
   `uv sync`, `uv add <pkg>`, `uv add --dev <pkg>`, `uv run <cmd>`.
2. **Run the gates before you claim you are done.** All five, from the repo root:
   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy
   uv run lint-imports
   uv run pytest
   ```
   If you cannot run them, say so explicitly. **Do not report a gate as passing because a
   note in PROGRESS.md says it passed** — run it. (The M8 commit shipped with
   `ruff format` failing because the recorded result was trusted.)
3. **`services/` must never import `tui/`.** This is what keeps safety, staging, clipboard
   and cell logic testable without a terminal. It is the single most valuable rule here.
4. **`tui/` contains no SQL and no file I/O.** The SQL panel renders a string produced by
   `services/sqlpreview.py` via the provider's dialect. **The SQL you read is the SQL that
   runs** — a screen that can invent SQL can be wrong in a way nobody reviews.
5. **`domain/` imports nothing from this project but itself.** Pure stdlib frozen
   dataclasses (`slots=True`), validated in `__post_init__`. No pydantic, ever (OQ-3).
6. **Nothing is written to the database before Apply.** `services/changes.py` cannot reach
   a database — keep it that way. Do not give the staging buffer a connection.
7. **Add a test with every fix.** A bug fix without a test that fails without the fix is
   incomplete. Say in the commit message what actually happened.

## Architecture

```
tui/  →  services/  →  { storage/ , providers/ }  →  domain/
                                   infra/ (clipboard backends)
```

Full narrative: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The order is a
`layers` contract in `pyproject.toml` enforced by `import-linter` (`lint-imports`).

| Package | Owns | Must never |
|---|---|---|
| `domain/` | models + invariants, no I/O | import Textual, SQL, or `services` |
| `providers/` | `DatabaseProvider` (I/O) + `SqlDialect` (text), `mssql/` | import `tui`, `storage`, `services` |
| `storage/` | profiles.toml, settings.toml, keyring secrets, audit log | import `tui`; touch a database |
| `services/` | orchestration and **all decisions** | import `tui` |
| `tui/` | screens, widgets, theme, keybindings | contain SQL strings or file I/O |
| `infra/` | clipboard backends, error translation | know about the domain |

### Decisions belong in pure functions

"Is this Apply allowed?", "what will this paste do?", "how should this cell render?" must
all be answerable as functions over plain values, and tested that way — no widget, no
terminal. **If a decision needs a widget to answer, it is in the wrong layer.** Move it
into `services/` and have the screen call it.

### Key patterns

- **Protocols, not ABCs** (OQ-4). `DatabaseProvider` and `SqlDialect` are
  `typing.Protocol`; `tests/fakes.py` provides `FakeProvider` so the whole app — including
  Apply, rollback and conflicts — is testable without a server.
- **Row identity requires a PK**, or a single-column non-null UNIQUE. Keyless
  UPDATE/DELETE is refused by default (`allow_keyless_writes` overrides). **Never fall back
  to "match all columns"** — fragile with duplicates and NULLs.
- **Views are read-only** in v1 (OQ-6).
- **Secrets are references, never values.** Profiles store a secret *reference*; the secret
  lives in the OS keyring or is prompted per session. It must never reach a file or a log
  (there is a test asserting this for the audit log).
- **Staging keeps the pre-image** for optimistic concurrency, and
  `ChangeService.rebind()` re-points the buffer at refreshed metadata — it refuses if the
  table's identity changed, so edits cannot land on another table's rows.
- **All-or-nothing staging.** One bad cell in a paste stages *nothing*; a half-pasted block
  is worse than a rejected one.
- **An optional preferences file must never stop the app.** A malformed
  `keybindings.toml` is a warning; defaults are kept.
- **The header badge and the write gate read the same `SafetyPolicy`.** A badge saying
  `DEV` while the gate fires as `PROD` is worse than no badge. Pinned by
  `test_the_badge_cannot_disagree_with_the_write_gate`.

## Testing

| Kind | How | Use for |
|---|---|---|
| domain / services / providers | plain `pytest` | decisions as pure functions |
| TUI | Textual `Pilot` (`run_test`), driven key by key | real interactions |
| visual | `pytest-textual-snapshot` (SVGs) | layout regressions; also the README images |
| live | `pytest -m live`, needs `SWISSKNIFE_TEST_DB_URL` | a real SQL Server |
| architecture | `lint-imports` | the layering |

Fast inner loop: `uv run pytest tests/unit -q` (no terminal, no database, seconds). Full
suite takes ~2.5 minutes — run it in the background and poll rather than assuming failure.

**Snapshot tests being stale is worse than not having them.** If you change a screen's
appearance deliberately, regenerate with `pytest --snapshot-update` and eyeball the diff.

## Conventions

- Comments explain **why**, never **what**. A comment restating the line below it is noise.
  The existing bar is high; match it.
- `docs/` and `README.md` are generated where possible: README screenshots are exported
  from the snapshot suite, and `tests/unit/tui/test_keybindings.py` fails if the
  documented keybindings drift from the widgets' real `BINDINGS`. If you add a keybinding,
  update the registry, not the docs.
- **Be honest about gaps.** The README's "Known limitations" and PROGRESS.md list what is
  *not* done, unprompted. If you add a limitation, document it; if you remove one, remove
  the line. Do not paper over an untested path — say it is untested.
- Line length 100. `ruff` rules `E,F,W,I,UP,B,C4,SIM,RUF`. `mypy` is **strict over `src`
  *and* `tests`** — tests are typed like production code.

## The memory bank is a local mechanism — it is not versioned

`memory-bank/` is [Cline's Memory Bank](https://docs.cline.bot/best-practices/memory-bank):
structured markdown that carries session-to-session context for whichever agent is
working on **this checkout**. It is a local convenience, not project documentation:

- It is in `.gitignore` (along with `.clinerules/`) and is **never committed, pushed, or
  reviewed in a PR**. Do not `git add -f` it.
- Its contents may differ — or the directory may not exist at all — in another clone, and
  that is expected. Never rely on it as the only record of a decision or a limitation.
- Anything that must be shared belongs in a committed doc instead: `README.md`,
  `PROGRESS.md`, `docs/`, or this file. Those are the shared source of truth; the memory
  bank only points the next local session at them.
- When you finish significant work, update it locally ("update memory bank": review all
  files) and commit the *real* docs in the same round.

## Before you commit

- [ ] All five gates run and green (or explicitly reported as not run).
- [ ] A test exists that fails without your fix.
- [ ] `lint-imports` still passes if you touched imports.
- [ ] README/PROGRESS updated if behaviour or a limitation changed.
- [ ] No secret, connection string or password in a file, a log or a test fixture.
- [ ] Commit message says **what happened**, not just what changed.

