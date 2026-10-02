"""Copy & paste flows through the real UI (M7, FR-4.1-4.7).

Driven with Textual's Pilot against ``FakeProvider``: no database, no terminal. What is
asserted is what the *user* gets — the preview dialog's wording, what ends up on the
clipboard, and what the pending strip says afterwards — because those are the only parts of
this feature that are not pure functions.
"""

from pathlib import Path

from textual.app import App
from textual.coordinate import Coordinate
from textual.events import Paste
from textual.pilot import Pilot
from textual.widgets import Input, Static

from sql_table_swiss_knife.domain.catalog import TableSummary
from sql_table_swiss_knife.services import ChangeService
from sql_table_swiss_knife.storage import ProfileStore, Settings, SettingsStore
from sql_table_swiss_knife.tui.screens import (
    PastePreviewScreen,
    PathPromptScreen,
    TableEditorScreen,
)
from sql_table_swiss_knife.tui.widgets import DataGrid
from tests.tui.conftest import SAMPLE_TABLES, AppFactory, active_screen

SETTLE = 6

#: The ASCII TSV block Excel would give us for the Country table.
EXCEL_BLOCK = "Code\tName\nES\tSpain\nPT\tPortugal"

#: Rows arrive in primary-key order, so the grid starts at CH (Switzerland), not DE.
FIRST_CODE, FIRST_NAME = "CH", "Switzerland"
SECOND_CODE, SECOND_NAME = "DE", "Germany"
CODES = ["CH", "DE", "FR", "JP", "US"]


async def _open(app: App[None], pilot: Pilot[None], name: str = "Country") -> TableEditorScreen:
    await app.connection.connect(app.connection.get_profile("catalog"))  # type: ignore[attr-defined]
    summary: TableSummary = {table.name: table.summary for table in SAMPLE_TABLES}[name]
    app.push_screen(TableEditorScreen(summary))
    for _ in range(SETTLE):
        await pilot.pause()
    return active_screen(app, TableEditorScreen)


async def _settle(pilot: Pilot[None], times: int = 4) -> None:
    for _ in range(times):
        await pilot.pause()


def _grid(app: App[None]) -> DataGrid:
    return app.screen.query_one("#editor-grid", DataGrid)


def _changes(screen: TableEditorScreen) -> ChangeService:
    """The staging service, asserting it exists (it is ``None`` before metadata loads)."""
    changes = screen.changes
    assert changes is not None
    return changes


def _pending(app: App[None]) -> str:
    return str(app.screen.query_one("#editor-pending", Static).render())


def _cell(app: App[None], row: int, column: int) -> str:
    return str(_grid(app).get_cell_at(Coordinate(row, column)))


async def _paste(app: App[None], pilot: Pilot[None], text: str) -> None:
    """Deliver a bracketed paste the way a terminal would."""
    app.screen.post_message(Paste(text))
    await _settle(pilot)


# -- paste ------------------------------------------------------------------


async def test_a_bracketed_paste_opens_the_preview_before_anything_is_staged(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-4.7 + FR-4.6: the payload arrives intact and nothing is staged yet."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        assert _changes(screen).is_empty  # nothing staged

        await _paste(app, pilot, EXCEL_BLOCK)

        preview = app.screen
        assert isinstance(preview, PastePreviewScreen)
        assert _changes(screen).is_empty  # still nothing staged until confirmed


async def test_the_preview_names_the_mapping_and_the_update_insert_split(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """The dialog must answer "which columns" and "update or insert" up front."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await _open(app, pilot)
        await _paste(app, pilot, f"Code\tName\n{SECOND_CODE}\tGermany II\nES\tSpain")

        preview = app.screen
        assert isinstance(preview, PastePreviewScreen)
        rendered = "\n".join(str(widget.render()) for widget in preview.query(Static))
        assert "1 update" in rendered
        assert "1 insert" in rendered
        assert "Code → Code" in rendered
        assert "Name → Name" in rendered
        assert "UPDATE" in rendered and "INSERT" in rendered


async def test_confirming_the_preview_stages_the_paste(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await _paste(app, pilot, f"Code\tName\n{SECOND_CODE}\tGermany II")
        await pilot.press("enter")
        await _settle(pilot)

        assert "1 update" in _changes(screen).summary
        assert _pending(app).startswith("staged: 1 update")


async def test_cancelling_the_preview_stages_nothing(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await _paste(app, pilot, EXCEL_BLOCK)
        await pilot.press("escape")
        await _settle(pilot)

        assert _changes(screen).is_empty


async def test_a_single_value_pastes_into_the_focused_cell(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-4.4: the cursor is on Code; move to Name and paste one value."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await pilot.press("right")
        await _settle(pilot, 2)
        await _paste(app, pilot, "Deutschland")

        preview = app.screen
        assert isinstance(preview, PastePreviewScreen)
        assert preview.plan.mode.value == "cell"
        await pilot.press("enter")
        await _settle(pilot)
        assert "Deutschland" in _cell(app, 0, 1)
        assert _changes(screen).counts  # an update, not an insert


async def test_a_multiline_value_stays_one_cell(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Newlines in a value are data: pasting two lines must not insert two rows."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        rows_before = _grid(app).row_count
        await pilot.press("right")  # Name is a text column; Population is not
        await _settle(pilot, 2)
        await _paste(app, pilot, "first line\nsecond line")
        preview = app.screen
        assert isinstance(preview, PastePreviewScreen)
        await pilot.press("enter")
        await _settle(pilot)
        assert _grid(app).row_count == rows_before  # no rows inserted
        assert "update" in _changes(screen).summary


async def test_a_paste_with_a_bad_cell_is_refused_and_reported(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-4.6: per-cell errors are collected; nothing is staged half-parsed."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await _paste(app, pilot, f"Code\tName\n{SECOND_CODE}\tGermany\nXX\t" + "y" * 300)

        assert not isinstance(app.screen, PastePreviewScreen)  # never offered
        assert _changes(screen).is_empty
        status = str(app.screen.query_one("#status-message", Static).render())
        assert "paste refused" in status


async def test_an_empty_paste_says_so_instead_of_doing_nothing(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await _paste(app, pilot, "   \n  ")

        assert _changes(screen).is_empty
        status = str(app.screen.query_one("#status-message", Static).render())
        assert "empty" in status


async def test_pasting_into_a_read_only_table_is_refused(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """S-4: a table with no usable key cannot be written at all."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "AuditLog")
        await _paste(app, pilot, "Id\n42")

        assert _changes(screen).is_empty
        status = str(app.screen.query_one("#status-message", Static).render())
        assert "read-only" in status


# -- copy -------------------------------------------------------------------


async def test_copying_a_cell_puts_its_value_on_the_clipboard(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open(app, pilot)
        app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
        copied: list[str] = []
        await pilot.press("ctrl+c")
        await _settle(pilot)

        assert copied == [FIRST_CODE]


async def test_copying_a_row_copies_every_visible_column(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open(app, pilot)
        copied: list[str] = []
        app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
        await pilot.press("b")  # scope: cell -> row
        await _settle(pilot, 2)
        await pilot.press("ctrl+c")
        await _settle(pilot)

        assert copied and copied[0].startswith(f"{FIRST_CODE}\t{FIRST_NAME}")


async def test_copying_a_column_copies_the_whole_column(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open(app, pilot)
        copied: list[str] = []
        app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
        await pilot.press("b", "b")  # scope: cell -> row -> column
        await _settle(pilot, 2)
        await pilot.press("ctrl+c")
        await _settle(pilot)

        # One value per line: a whole column is still a single-cell-wide block.
        assert copied == ["\n".join(CODES)]


async def test_the_copy_format_cycles_and_is_persisted(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open(app, pilot)
        copied: list[str] = []
        app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]

        await pilot.press("p")  # tsv -> csv
        await _settle(pilot, 3)
        assert app.settings.copy_format == "csv"
        await pilot.press("ctrl+c")
        await _settle(pilot)
        assert copied == [FIRST_CODE]


async def test_ctrl_v_without_the_read_fallback_explains_itself(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """No bracketed paste and no read fallback: say why, do not paste nothing silently."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open(app, pilot)
        await pilot.press("ctrl+v")
        await _settle(pilot)

        assert "bracketed paste" in str(app.screen.query_one("#status-message", Static).render())


# -- import / export --------------------------------------------------------


async def test_exporting_writes_the_rows_as_seen(
    app_factory: AppFactory, seeded_profiles: ProfileStore, tmp_path: Path
) -> None:
    app = app_factory(profiles=seeded_profiles)
    target = tmp_path / "countries.csv"
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open(app, pilot)
        await pilot.press("o")
        await _settle(pilot)
        prompt = app.screen
        assert isinstance(prompt, PathPromptScreen)
        prompt.query_one("#path-input", Input).value = str(target)
        await _settle(pilot, 2)
        await pilot.press("enter")
        await _settle(pilot)

    lines = target.read_text(encoding="utf-8-sig").splitlines()
    # Every column the grid shows is exported, read-only ones included: this is what the
    # user can see, and an export of a subset would silently drop data.
    assert lines[0] == "Code,Name,Population,NameUpper,RowVer"
    assert lines[1].startswith(f"{FIRST_CODE},{FIRST_NAME}")


async def test_importing_a_csv_file_goes_through_the_same_preview(
    app_factory: AppFactory, seeded_profiles: ProfileStore, tmp_path: Path
) -> None:
    """A file is a paste whose source you chose: same preview, same staging rules."""
    app = app_factory(profiles=seeded_profiles)
    source = tmp_path / "more.csv"
    source.write_text("Code,Name\nES,Spain\nPT,Portugal\n", encoding="utf-8")
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await pilot.press("i")
        await _settle(pilot)
        prompt = app.screen
        assert isinstance(prompt, PathPromptScreen)
        prompt.query_one("#path-input", Input).value = str(source)
        await _settle(pilot, 2)
        await pilot.press("enter")
        await _settle(pilot, SETTLE)

        preview = app.screen
        assert isinstance(preview, PastePreviewScreen)
        assert "2 inserts" in preview.plan.summary()
        await pilot.press("enter")
        await _settle(pilot)
        assert "2 inserts" in _changes(screen).summary


async def test_importing_a_missing_file_is_refused_in_the_dialog(
    app_factory: AppFactory, seeded_profiles: ProfileStore, tmp_path: Path
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open(app, pilot)
        await pilot.press("i")
        await _settle(pilot)
        prompt = app.screen
        assert isinstance(prompt, PathPromptScreen)
        prompt.query_one("#path-input", Input).value = str(tmp_path / "nope.csv")
        await _settle(pilot, 2)
        await pilot.press("enter")
        await _settle(pilot)

        assert isinstance(app.screen, PathPromptScreen)  # still open
        assert "does not exist" in str(prompt.query_one("#path-error", Static).render())


async def test_shift_arrows_select_a_rectangle_that_copy_takes(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-4.1: "the selected range" is a real scope, not a promise in the docs."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await pilot.press("shift+right", "shift+down")
        await _settle(pilot, 2)
        assert screen.selection() == (0, 0, 1, 1)

        copied: list[str] = []
        app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
        await pilot.press("b", "b", "b")  # scope: cell -> row -> column -> selection
        await _settle(pilot, 2)
        await pilot.press("ctrl+c")
        await _settle(pilot)

        assert copied == [f"{FIRST_CODE}\t{FIRST_NAME}\n{SECOND_CODE}\t{SECOND_NAME}"]


def _code(index: int) -> str:
    """A two-letter key value (``char(2)``) for the nth generated row."""
    return chr(ord("A") + index // 26) + chr(ord("A") + index % 26)


async def test_a_large_paste_is_planned_off_the_event_loop(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """NFR-2: a 500-row block plans in a worker thread and still reaches the preview."""
    app = app_factory(profiles=seeded_profiles)
    block = "Code\tName\n" + "".join(f"{_code(i)}\tCountry {i}\n" for i in range(500))
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await _paste(app, pilot, block)

        preview = app.screen
        assert isinstance(preview, PastePreviewScreen)
        assert len(preview.plan.rows) == 500
        await pilot.press("escape")
        await _settle(pilot)
        assert _changes(screen).is_empty


async def test_a_block_over_the_configured_row_limit_is_refused(
    app_factory: AppFactory, seeded_profiles: ProfileStore, tmp_path: Path
) -> None:
    """``paste_max_rows`` is a guard rail, not a suggestion: 20 rows with a limit of 5."""
    SettingsStore(tmp_path / "settings.toml").save(Settings(paste_max_rows=5))
    app = app_factory(profiles=seeded_profiles)
    block = "Code\tName\n" + "".join(f"{_code(i)}\tCountry {i}\n" for i in range(20))
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot)
        await _paste(app, pilot, block)

        assert not isinstance(app.screen, PastePreviewScreen)  # never offered
        assert _changes(screen).is_empty
        status = str(screen.query_one("#status-message", Static).render())
        assert "paste refused" in status
