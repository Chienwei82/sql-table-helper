## What this changes

<!-- What changed, and why this way. -->

## Related issue

<!-- Closes #123 -->

## Gates

All five must be green. Tick what you actually ran — if you could not run one, say so
instead of leaving it unticked without comment.

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] `uv run lint-imports`
- [ ] `uv run pytest`

<!-- Any gate not run, and why: -->

## Testing

- [ ] New behaviour has a test, and the test **fails without** the change
- [ ] Screenshots regenerated (`pytest --snapshot-update`) if a screen changed, and the
      diff was reviewed
- [ ] TUI behaviour driven through `Pilot` where relevant

## Checklist

- [ ] `services/` still does not import `tui/`; `tui/` still contains no SQL or file I/O
- [ ] Decisions live in pure functions in `services/`, testable without a terminal
- [ ] No secrets, connection strings or passwords in code, tests, logs or fixtures
- [ ] README / PROGRESS.md updated if behaviour or a known limitation changed
