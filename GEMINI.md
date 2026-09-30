# AGENTS.md — instructions for coding agents

The complete, authoritative instructions for working in this repository are in
**[AGENTS.md](AGENTS.md)**. Read that file first; it is the single source of truth and
this document only points at it.

## Quick reference

- **Toolchain:** `uv` exclusively. `uv sync`, `uv add <pkg>`, `uv run <cmd>`. There is no
  `requirements.txt`; do not `pip install` into the venv.
- **Quality gates (all five, before you claim done):**
  `uv run ruff check .` · `uv run ruff format --check .` · `uv run mypy` ·
  `uv run lint-imports` · `uv run pytest`
- **Architecture is a contract, not a convention:** `import-linter` enforces
  `tui → services → {storage, providers} → domain`. The two rules that matter most:
  `services/` never imports `tui/`, and `tui/` never contains SQL or file I/O.
- **Tests:** a bug fix without a test that fails without the fix is incomplete.
- **Honesty:** the README's "Known limitations" and PROGRESS.md are kept up to date. Do
  not claim a path is verified when it is not.

## Project documents

| Document | What it is for |
|---|---|
| [AGENTS.md](AGENTS.md) | **the agent instructions — read this** |
| [README.md](README.md) | for humans: what it does, install, keys, safety |
| [SPEC.md](SPEC.md) | requirements (FR/S/NFR ids) and the safety rules |
| [DESIGN.md](DESIGN.md) | why it is built this way |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | layering, safety model, concurrency |
| [docs/ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md) | supporting another DBMS |
| [PROGRESS.md](PROGRESS.md) | milestone status, coverage, known gaps |
