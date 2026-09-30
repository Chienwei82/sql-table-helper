# CLAUDE.md

@AGENTS.md

The full instructions for working in this repository live in [AGENTS.md](AGENTS.md) and
are imported above. In short:

- `uv` only — `uv sync`, `uv add`, `uv run`. Never `pip install`.
- Run all five gates before claiming done: `ruff check`, `ruff format --check`, `mypy`,
  `lint-imports`, `pytest`. Do not trust a recorded result; run it.
- `services/` must never import `tui/`. `tui/` must never contain SQL or file I/O.
- Decisions belong in pure functions in `services/`, tested without a terminal.
- Every fix comes with a test that fails without it.

Read [AGENTS.md](AGENTS.md) in full before making changes. Project docs:
[README.md](README.md) (humans), [SPEC.md](SPEC.md) (requirements),
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (layering),
[PROGRESS.md](PROGRESS.md) (status).
