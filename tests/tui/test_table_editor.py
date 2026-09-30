"""TableEditorScreen: split view, F2 toggle, grid contents, inspector contents (M4).

Driven through Textual's Pilot against ``FakeProvider`` — no database, no terminal. The
assertions are on what the user can actually read: grid headers and cells, the inspector's
four sections, and the status/hint chrome around them (FR-3, FR-6).
"""

from textual.app import App
from textual.coordinate import Coordinate
from textual.pilot import Pilot
from textual.widgets import Static

from sql_table_swiss_knife.domain.catalog import TableSummary
from sql_table_swiss_knife.storage import ProfileStore
from sql_table_swiss_knife.tui.screens import CellEditorScreen, TableEditorScreen
from sql_table_swiss_knife.tui.widgets import DataGrid, InspectorPanel
from tests.tui.conftest import (
    AUDIT,
    COUNTRY,
    SAMPLE_ROWS,
    SAMPLE_TABLES,
    AppFactory,
    active_screen,
)

#: Long enough for the workers (metadata + rows) to finish.
SETTLE = 6


async def _open(app: App[None], pilot: Pilot[None], name: str) -> TableEditorScreen:
    """Connect, push the editor for ``name`` and let the workers finish."""
    await app.connection.connect(app.connection.get_profile("catalog"))  # type: ignore[attr-defined]
    summary: TableSummary = {table.name: table.summary for table in SAMPLE_TABLES}[name]
    app.push_screen(TableEditorScreen(summary))
    for _ in range(SETTLE):
        await pilot.pause()
    return active_screen(app, TableEditorScreen)


def _panel_text(app: App[None]) -> str:
    """The inspector panel rendered as plain text (what the user reads)."""
    panel = app.screen.query_one("#editor-inspector", InspectorPanel)
    rendered = panel.query_one("#inspector-body", Static).render()
    return str(rendered)


def _headers(app: App[None]) -> list[str]:
    grid = app.screen.query_one("#editor-grid", DataGrid)
    return [str(key.label) for key in grid.columns.values()]


def _cell(app: App[None], row: int, column: int) -> str:
    grid = app.screen.query_one("#editor-grid", DataGrid)
    return str(grid.get_cell_at(Coordinate(row, column)))


# -- split view --------------------------------------------------------------


async def test_opening_a_table_shows_a_grid_and_the_inspector(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """M4: the workspace is a split view; both halves are populated after one open."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "Country")

        assert isinstance(screen, TableEditorScreen)
        grid = screen.query_one("#editor-grid", DataGrid)
        assert grid.table is COUNTRY
        assert grid.row_count == len(SAMPLE_ROWS["Country"])
        assert "TABLE SUMMARY" in _panel_text(app)


async def test_the_inspector_toggles_with_f2(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """F2 collapses and re-expands the panel (FR-6.3)."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "Country")
        panel = screen.query_one("#editor-inspector", InspectorPanel)
        assert not panel.collapsed

        await pilot.press("f2")
        await pilot.pause()
        assert panel.collapsed

        await pilot.press("f2")
        await pilot.pause()
        assert not panel.collapsed


async def test_the_hint_bar_reports_the_inspector_state(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Context-sensitive hints (DESIGN §9.2): the verb follows the panel state."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "Country")
        assert "hide inspector" in screen.hints.hints[0].label

        await pilot.press("f2")
        await pilot.pause()
        assert "show inspector" in screen.hints.hints[0].label


# -- inspector contents ------------------------------------------------------


async def test_the_panel_has_all_four_sections(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Summary, warnings, column list, column detail — in that order."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _open(app, pilot, "Country")
        text = _panel_text(app)

        assert text.index("TABLE SUMMARY") < text.index("WARNINGS")
        assert text.index("WARNINGS") < text.index("COLUMN LIST")
        assert text.index("COLUMN LIST") < text.index("COLUMN DETAIL")
        assert "dbo.Country" in text
        assert "primary key: Code" in text


async def test_the_summary_reports_rows_pk_and_incoming_references(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-6.1: row count, PK columns and "referenced by N tables"."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _open(app, pilot, "Country")
        text = _panel_text(app)

        assert "rows: ≈5" in text
        assert "primary key: Code" in text
        assert "referenced by: 1 table" in text


async def test_the_column_list_shows_badges_for_every_column(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """🔑 PK, # identity-ish facts, ƒ computed, ⏱ rowversion, ∅/✱ nullability, D, U, ✓."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _open(app, pilot, "Country")
        text = _panel_text(app)

        assert "Code" in text and "🔑" in text
        assert "✓" in text  # CK_Country_Population
        assert "U" in text  # UQ_Country_Name
        assert "ƒ" in text  # NameUpper
        assert "⏱" in text  # RowVer
        assert "∅" in text  # NameUpper is nullable
        assert "✱" in text  # Code is NOT NULL


async def test_the_detail_section_follows_the_focused_column(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Moving the grid cursor moves the inspector's column detail (FR-6.3)."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "Country")
        panel = screen.query_one("#editor-inspector", InspectorPanel)
        assert "COLUMN DETAIL" in _panel_text(app)
        assert "type: char(2)" in _panel_text(app)  # Code, the first column

        await pilot.press("right")  # -> Name
        await pilot.pause()
        assert "type: nvarchar(100)" in _panel_text(app)
        assert "unique: UQ_Country_Name" in _panel_text(app)
        assert panel.collapsed is False


async def test_the_detail_names_the_exact_type_and_read_only_reason(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """The focused identity/computed column explains why it cannot be edited (S-3)."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "Country")
        grid = screen.query_one("#editor-grid", DataGrid)

        for _ in range(3):  # Code -> Name -> Population -> NameUpper
            await pilot.press("right")
        await pilot.pause()
        text = _panel_text(app)
        assert "computed: UPPER([Name])" in text
        assert "read-only: computed column" in text
        assert grid.state_for(3) == "readonly"


# -- warnings banner ---------------------------------------------------------


async def test_warnings_are_rendered_with_severity_glyphs(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """An AFTER trigger is a caution; a disabled one is dimmed and says it will not fire."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _open(app, pilot, "Country")
        text = _panel_text(app)

        assert "AFTER trigger on UPDATE" in text
        assert "disabled AFTER trigger on INSERT" in text
        assert "will not fire" in text
        assert "referenced by 1 table" in text  # incoming FK note


async def test_a_view_with_an_instead_of_trigger_shows_the_strong_warning(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """INSTEAD OF gets the explicit "may not do what you expect" wording."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _open(app, pilot, "v_Customer")
        text = _panel_text(app)

        assert "INSTEAD OF trigger on INSERT, UPDATE" in text
        assert "may not do what you expect" in text
        assert text.index("⛔") < text.index("COLUMN LIST")  # errors come first


async def test_a_table_without_a_primary_key_warns_about_row_identity(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """S-4 in words: identity is ambiguous, updates/deletes are blocked."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "AuditLog")
        text = _panel_text(app)

        assert "no primary key" in text
        assert "row identity is ambiguous" in text
        assert "read-only" in text
        assert screen.query_one("#editor-grid", DataGrid).has_class("-readonly")


async def test_the_warning_strip_summarises_severity_when_the_panel_is_collapsed(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """A collapsed panel must still tell the user the table is risky."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "Country")
        strip = screen.query_one("#editor-warnings", Static)

        assert strip.has_class("-shown")
        assert "caution" in str(strip.render())

        await pilot.press("f2")  # collapse
        await pilot.pause()
        assert strip.has_class("-shown")  # still visible


# -- grid rendering ----------------------------------------------------------


async def test_grid_headers_carry_badges_and_types(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Headers show name + badges + compact type (FR-3.1)."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _open(app, pilot, "Country")
        headers = _headers(app)

        assert headers[0].startswith("Code ") and "char(2)" in headers[0]
        assert "🔑" in headers[0]  # PK badge
        assert "🔒" in headers[3]  # NameUpper is computed -> read-only marker
        assert "⏱" in headers[4]  # RowVer


async def test_grid_cells_render_null_and_binary_distinctly(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-3.1: NULL upper-case, binary as 0x… with a length."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _open(app, pilot, "Country")

        # Rows arrive in PK (Code) order: CH, DE, FR, JP, US.
        assert _cell(app, 0, 0) == "CH"
        assert _cell(app, 0, 1) == "Switzerland"
        assert _cell(app, 0, 2) == "8700000"
        # A rowversion column is binary *and* read-only, so it carries the lock glyph
        # (S-3): the grid says "you cannot edit this" without the cell being opened.
        assert _cell(app, 0, 4) == "0x05 (1 bytes) 🔒"


async def test_pressing_enter_on_a_read_only_cell_explains_itself(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """S-3 in the status line: the user is told why the cell cannot be edited."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "Country")
        grid = screen.query_one("#editor-grid", DataGrid)
        grid.focus()

        for _ in range(3):  # move to the computed column
            await pilot.press("right")
        await pilot.press("enter")
        await pilot.pause()

        assert "computed column" in screen.status.message


async def test_pressing_enter_on_an_editable_cell_opens_the_editor(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """M5: Enter opens the cell editor instead of explaining that editing is unavailable."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _open(app, pilot, "Country")
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, CellEditorScreen)


# -- paging ------------------------------------------------------------------


async def test_the_status_bar_reports_the_loaded_span(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-3.8: the header/status tells the user how many rows are loaded."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "Country")
        grid = screen.query_one("#editor-grid", DataGrid)

        assert "rows 1-5 of 5" in screen._screen_title()
        assert grid.row_count == 5


async def test_fetch_more_appends_the_next_page(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """The ``m`` action grows the window rather than replacing it."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "Country")
        grid = screen.query_one("#editor-grid", DataGrid)
        assert grid.row_count == 5

        # Nothing left to fetch: the action reports that instead of doing nothing.
        await pilot.press("m")
        await pilot.pause()
        assert "no more rows" in screen.status.message
        assert grid.row_count == 5


async def test_reload_rereads_metadata_and_rows(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, "Country")
        await pilot.press("r")
        for _ in range(SETTLE):
            await pilot.pause()

        assert screen.query_one("#editor-grid", DataGrid).row_count == 5


async def test_the_screen_without_a_connection_offers_a_way_back(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Losing the session mid-flow must offer a reconnect path, not a crash (FR-10)."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        app.push_screen(TableEditorScreen(AUDIT.summary))
        await pilot.pause()

        assert isinstance(app.screen, TableEditorScreen)
        assert "no active connection" in app.screen.status.message
