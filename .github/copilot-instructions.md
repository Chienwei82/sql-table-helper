# Instructions for GitHub Copilot

The complete instructions for coding agents in this repository are in
[AGENTS.md](../AGENTS.md). Read that first — it is the single source of truth.

## Quick reference

- **Toolchain:** `uv` only. `uv sync`, `uv add <pkg>`, `uv run <cmd>`. No `pip install`,
  no `requirements.txt`.
- **Gates (run all five before claiming done):** `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy`, `uv run lint-imports`, `uv run pytest`.
- **Layering is enforced by `import-linter`:** `tui → services → {storage, providers} →
  domain`. The two rules most often broken:
  - `services/` must never import `tui/`.
  - `tui/` must never contain SQL strings or file I/O.
- **Decisions belong in pure functions** in `services/`, unit-tested without a terminal.
- **Every bug fix needs a test that fails without the fix.**

## Project conventions

- Python 3.14, `mypy` **strict** over `src` *and* `tests`. Line length 100.
- stdlib frozen dataclasses with `slots=True` for models — no pydantic.
- `typing.Protocol` for the provider/dialect seams — no ABCs.
- Comments explain **why**, never **what**.
- Documentation is generated where possible: README screenshots come from the snapshot
  suite, and keybinding docs are drift-checked against the real `BINDINGS`.
- Be explicit about what is *not* tested or *not* done; see the README's Known limitations.

Start with [AGENTS.md](../AGENTS.md) for the full picture.
