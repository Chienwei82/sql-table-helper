# Contributing

Thanks for looking. This is a small, deliberately-scoped project, and the rules below
exist to keep it that way.

**If you use an AI coding agent, point it at [AGENTS.md](AGENTS.md).** That file is the
authoritative set of instructions for this repository; the rules here are the human-facing
summary of the same thing.

## Setup

```bash
git clone https://github.com/Chienwei82/sql-table-helper.git
cd sql-table-helper
uv sync
uv run sql-table-swiss-knife --version
```

Requires Python 3.14 (see `.python-version`). [`uv`](https://docs.astral.sh/uv/) manages
the environment — **do not `pip install` into the venv**; there is no
`requirements.txt`. Add dependencies with `uv add` / `uv add --dev` so `uv.lock` stays
in sync.

Running against SQL Server also needs `unixODBC` and the Microsoft ODBC Driver 18. A
dockerized sample server lives in `tests/live` (see the README).

If your network intercepts TLS with a private CA, `uv sync` will fail in a way `pip`
would not — it does not read `REQUESTS_CA_BUNDLE`. See
[docs/CORPORATE_PROXY.md](docs/CORPORATE_PROXY.md); the short version is
`SSL_CERT_FILE=/path/to/corp-ca.pem` or `UV_SYSTEM_CERTS=1`, set as environment
variables rather than in `pyproject.toml`.

## The five gates

Run all of them before you open a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
uv run pytest
```

- `mypy` is **strict** and covers `tests/` too.
- `lint-imports` enforces the layering. If your change breaks it, the architecture needs
  fixing — not the contract.
- The full suite takes about 2.5 minutes. For a fast inner loop use
  `uv run pytest tests/unit -q` (no terminal, no database).

If you cannot run a gate, say so in the PR rather than implying it passed.

## The rules that matter

1. **`services/` must never import `tui/`.** It keeps the safety policy, change staging
   and clipboard logic testable without a terminal. This is the most valuable constraint
   in the codebase.
2. **`tui/` contains no SQL and no file I/O.** The SQL panel renders a string a service
   produced. *The SQL you read is the SQL that runs.*
3. **`domain/` imports nothing from the project but itself.** Models are stdlib frozen
   dataclasses with `slots=True`, validated in `__post_init__`. No pydantic.
4. **Decisions are pure functions.** "Is this Apply allowed?" and "what will this paste
   do?" are answerable over plain values and tested that way.
5. **Nothing reaches the database before Apply.** The staging buffer has no connection,
   deliberately.

Rationale for all of these: [AGENTS.md](AGENTS.md) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

- Every bug fix needs a test that **fails without the fix**. Verify that it does.
- TUI behaviour is driven through Textual's `Pilot`, key by key.
- Visual changes are covered by SVG snapshots. If you change a screen's appearance on
  purpose, regenerate with `pytest --snapshot-update` **and review the diff** — stale
  snapshots are worse than none.
- Integration tests (`pytest -m live`) need `SWISSKNIFE_TEST_DB_URL`; they skip
  themselves otherwise. They are not required for a contribution.

## Style

- Comments explain **why**, never **what**. A comment restating the line below it is
  noise. The existing bar is high; match it.
- Line length 100. `ruff` handles the rest.
- If you add a keybinding, update `tui/keybindings.py` — a test fails if the docs and the
  real `BINDINGS` drift apart.
- If you change behaviour or remove a limitation, update the README and `PROGRESS.md`.

## Commit messages

Say **what happened**, not just what changed. The M8 commit that fixed a silent data-loss
bug on reload explains the defect precisely; that is the standard. A useful message lets
the next person understand a decision without reading the diff.

## Honesty about gaps

The README's "Known limitations" and `PROGRESS.md` list what is *not* done and what is
*not* verified — currently including that the full Apply path has never been driven
against a real SQL Server. Please keep that culture: document your own uncertainty
rather than letting a reviewer assume coverage that does not exist.

## Reporting a security issue

This tool writes to production databases. If you find a way to make it write when it
should refuse, or to leak a secret into a log, please report it — see
[SECURITY.md](SECURITY.md) for what to open publicly, what to disclose privately, and
exactly what the app does and does not protect. Saying "allowed when it should have been
blocked" is a security bug, and naming it plainly helps.
