"""TableEditorScreen editing: staging, the pending counts, and Apply (M5, FR-7).

Driven through Textual's Pilot against ``FakeProvider`` — no database, no terminal. The
assertions are on what the user can read and do: the cell editor, the staged overlay's
glyphs, the pending strip's counts, the foreign-key picker, and what reaches the (fake)
database when Apply runs.
"""

from textual.app import App
from textual.coordinate import Coordinate
from textual.pilot import Pilot
from textual.widgets import Input, Static

from sql_table_swiss_knife.domain import ChangeKind
from sql_table_swiss_knife.domain.catalog import TableSummary
from sql_table_swiss_knife.storage import ProfileStore
from sql_table_swiss_knife.tui.screens import (
    CellEditorScreen,
    TableEditorScreen,
)
from sql_table_swiss_knife.tui.widgets import DataGrid
from tests.fakes import FakeProvider
from tests.tui.conftest import SAMPLE_TABLES, AppFactory, active_screen, make_provider

SETTLE = 6

#: The delete marker DESIGN §9.4 specifies; ruff flags the glyph as an ambiguous
#: look-alike, which is exactly why it is also spelled out in the STATE_GLYPHS table.
DELETED_GLYPH = "\u00d7"


async def _open(app: App[None], pilot: Pilot[None], name: str = "Country") -> TableEditorScreen:
    """Connect, push the editor for ``name`` and let the workers finish."""
    await app.connection.connect(app.connection.get_profile("catalog"))  # type: ignore[attr-defined]
    summary: TableSummary = {table.name: table.summary for table in SAMPLE_TABLES}[name]
    app.push_screen(TableEditorScreen(summary))
    for _ in range(SETTLE):
        await pilot.pause()
    return active_screen(app, TableEditorScreen)


async def _settle(pilot: Pilot[None]) -> None:
    for _ in range(3):
        await pilot.pause()


def _grid(app: App[None]) -> DataGrid:
    return app.screen.query_one("#editor-grid", DataGrid)


def _cell(app: App[None], row: int, column: int) -> str:
    return str(_grid(app).get_cell_at(Coordinate(row, column)))


def _pending(app: App[None]) -> str:
    """The pending-changes strip, as the user reads it (FR-7.2)."""
    return str(app.screen.query_one("#editor-pending", Static).render())


async def _stage_name(app: App[None], pilot: Pilot[None], value: str) -> None:
    """Open the cell editor on the Name column, replace the text and stage it.

    The cursor starts on the PK (``Code``, a 2-character column), so the helper moves to
    ``Name`` first — a three-letter test value would be refused by ``char(2)``, which is
    the correct behaviour but not what these tests are about.
    """
    await pilot.press("right")
    await pilot.press("enter")
    await _settle(pilot)
    editor = app.screen
    assert isinstance(editor, CellEditorScreen)
    editor.query_one("#cell-editor-input", Input).value = value
    await _settle(pilot)
    await pilot.press("enter")
    await _settle(pilot)


def conflicting_provider() -> FakeProvider:
    """A provider whose first statement always reports 0 rows affected (FR-7.8)."""
    return make_provider(conflict_on=0)


# -- the cell editor ----------------------------------------------------------


async def test_the_cell_editor_shows_the_live_hints(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-3.5: the editor explains what the column accepts while you type."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _open(app, pilot)
        await pilot.press("right", "right")  # Population: int
        await pilot.press("enter")
        await _settle(pilot)

        editor = app.screen
        assert isinstance(editor, CellEditorScreen)
        editor.query_one("#cell-editor-input", Input).value = "not a number"
        await pilot.pause()
        hints = str(editor.query_one("#cell-editor-hints", Static).render())
        assert "whole number" in hints


async def test_an_invalid_value_is_never_staged(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await pilot.press("right", "right")  # Population: int
        await pilot.press("enter")
        await _settle(pilot)

        editor = app.screen
        assert isinstance(editor, CellEditorScreen)
        editor.query_one("#cell-editor-input", Input).value = "not a number"
        await _settle(pilot)
        await pilot.press("enter")  # blocked: the modal stays open
        await _settle(pilot)

        assert isinstance(app.screen, CellEditorScreen)
        assert screen.changes is not None
        assert screen.changes.is_empty is True


async def test_cancelling_the_cell_editor_stages_nothing(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await pilot.press("enter")
        await _settle(pilot)
        await pilot.press("escape")
        await _settle(pilot)

        assert screen.changes is not None
        assert screen.changes.is_empty is True


# -- staging ------------------------------------------------------------------


async def test_staging_an_edit_shows_the_new_value_with_a_marker(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-3.7: the staged value is displayed with the ``~`` glyph, nothing is written yet."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        before = _cell(app, 0, 1)

        await _stage_name(app, pilot, "Xed")

        assert _cell(app, 0, 1) == "Xed ~"
        assert _cell(app, 0, 1) != before
        assert screen.changes is not None
        assert screen.changes.counts[ChangeKind.UPDATE] == 1


async def test_the_pending_strip_counts_what_is_staged(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-7.2: the status bar says N inserts, M updates, K deletes."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _open(app, pilot)
        assert _pending(app) == ""  # nothing staged: the strip is hidden

        await _stage_name(app, pilot, "Xed")
        assert "1 update" in _pending(app)

        await pilot.press("ctrl+n")  # a new row
        await _settle(pilot)
        assert "1 insert, 1 update" in _pending(app)

        await pilot.press("delete")  # forgetting a staged row is not a "delete"
        await _settle(pilot)
        assert "1 update" in _pending(app)


async def test_delete_marks_the_row_and_undo_restores_it(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await pilot.press("delete")
        await _settle(pilot)

        assert screen.changes is not None
        assert screen.changes.counts[ChangeKind.DELETE] == 1
        assert _cell(app, 0, 1).endswith(DELETED_GLYPH)

        await pilot.press("ctrl+z")
        await _settle(pilot)

        assert screen.changes.counts[ChangeKind.DELETE] == 0
        assert DELETED_GLYPH not in _cell(app, 0, 1)


async def test_a_new_row_appears_in_the_grid_with_a_marker(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        rows_before = _grid(app).row_count

        await pilot.press("ctrl+n")
        await _settle(pilot)

        assert _grid(app).row_count == rows_before + 1
        assert screen.changes is not None
        assert screen.changes.counts[ChangeKind.INSERT] == 1
        assert _cell(app, rows_before, 1).endswith("+")


async def test_a_new_row_can_be_filled_in_right_away(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """The cursor lands on the staged row, so the next thing is typing into it."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await pilot.press("ctrl+n")
        await _settle(pilot)
        await pilot.press("right")  # Name
        await pilot.press("enter")
        await _settle(pilot)

        editor = app.screen
        assert isinstance(editor, CellEditorScreen)
        editor.query_one("#cell-editor-input", Input).value = "Freedonia"
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)

        assert screen.changes is not None
        staged = screen.changes.inserts()
        assert staged[0].after is not None
        assert staged[0].after["Name"] == "Freedonia"
        assert "1 insert" in _pending(app)


async def test_revert_row_drops_a_staged_new_row_entirely(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        rows_before = _grid(app).row_count
        await pilot.press("ctrl+n")
        await _settle(pilot)
        assert _grid(app).row_count == rows_before + 1

        await pilot.press("ctrl+u")
        await _settle(pilot)

        assert screen.changes is not None
        assert screen.changes.is_empty is True
        assert _grid(app).row_count == rows_before


# -- apply --------------------------------------------------------------------


async def test_apply_asks_for_confirmation_and_writes_nothing_until_then(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-7.5: Apply is confirmed with the counts; only then does it reach the database."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Xed")

        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert screen.changes is not None
        assert screen.changes.is_empty is False  # still staged: the dialog is open

        await pilot.press("y")
        for _ in range(SETTLE):
            await pilot.pause()

        assert screen.changes.is_empty is True
        assert _pending(app) == ""


async def test_applying_refreshes_so_the_grid_shows_the_written_value(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """The server owns identity and rowversion values, so Apply re-reads the rows."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Xed")
        await pilot.press("ctrl+s")
        await _settle(pilot)
        await pilot.press("y")
        for _ in range(SETTLE):
            await pilot.pause()

        assert screen.changes is not None
        assert screen.changes.is_empty is True
        assert "Xed" in _cell(app, 0, 1)
        assert "~" not in _cell(app, 0, 1)


async def test_apply_with_nothing_staged_says_so(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await pilot.press("ctrl+s")
        await _settle(pilot)

        assert "nothing staged" in screen.status.message


async def test_a_concurrency_conflict_is_reported_and_the_work_is_kept(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-7.8: the conflicting row is named, the UI survives, nothing is lost."""
    app = app_factory(profiles=seeded_profiles, provider=conflicting_provider())
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Xed")
        await pilot.press("ctrl+s")
        await _settle(pilot)
        await pilot.press("y")
        for _ in range(SETTLE):
            await pilot.pause()

        assert "0 rows affected" in screen.status.message or "changed by someone else" in (
            screen.status.message
        )
        assert screen.changes is not None
        assert screen.changes.is_empty is False  # still staged, so the user can retry
