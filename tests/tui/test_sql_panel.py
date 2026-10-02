"""The SQL panel: F3, the three renderings, copying, and "generate SQL for…" (FR-5).

Driven through Textual's Pilot against ``FakeProvider``. The assertions are on what the user
can read and copy, never on internals: which mode the panel is in, that the shown text
really contains the staged values, that the clipboard receives the runnable script, and —
the property that matters most — that nothing on this screen ever reaches the database.
"""

from typing import cast

from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Input, ListView, Static

from sql_table_swiss_knife.domain.catalog import TableSummary
from sql_table_swiss_knife.services.sqlpreview import PREVIEW_ONLY_NOTE, SqlMode
from sql_table_swiss_knife.storage import ProfileStore
from sql_table_swiss_knife.tui.screens import SqlActionScreen, TableEditorScreen
from sql_table_swiss_knife.tui.widgets import SqlPanel
from tests.fakes import FakeProvider
from tests.tui.conftest import SAMPLE_TABLES, AppFactory, active_screen

SETTLE = 6


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


def _panel(app: App[None]) -> SqlPanel:
    return app.screen.query_one("#editor-sql", SqlPanel)


def _body(app: App[None]) -> str:
    """The SQL the panel is showing.

    Read from the panel's own text rather than the rendered widget: the body holds a Rich
    ``Syntax`` object, whose ``render()`` is a visual, not the SQL. Asserting on
    ``current_text()`` also pins the claim that matters — the copy target and the displayed
    text are the same string.
    """
    return _panel(app).current_text()


def _legend(app: App[None]) -> str:
    return str(_panel(app).query_one("#sql-panel-params", Static).render())


def _hint(app: App[None]) -> str:
    return str(_panel(app).query_one("#sql-panel-hint", Static).render())


def _summary(app: App[None]) -> str:
    return str(_panel(app).query_one("#sql-panel-summary", Static).render())


def _listing(app: App[None]) -> ListView:
    return _panel(app).query_one("#sql-panel-list", ListView)


async def _stage_name(app: App[None], pilot: Pilot[None], value: str) -> None:
    """Edit the Name column and stage it (the cursor starts on the 2-character PK)."""
    await pilot.press("right")
    await pilot.press("enter")
    await _settle(pilot)
    app.screen.query_one("#cell-editor-input", Input).value = value
    await _settle(pilot)
    await pilot.press("enter")
    await _settle(pilot)


async def _set_mode(
    app: App[None], pilot: Pilot[None], screen: TableEditorScreen, mode: SqlMode
) -> None:
    """Open the panel and press ``v`` until it is in ``mode`` — the key is a full cycle."""
    await pilot.press("f3")
    await _settle(pilot)
    for _ in range(4):
        if screen.build_preview().mode is mode:
            return
        await pilot.press("v")
        await _settle(pilot)
    raise AssertionError(f"never reached {mode}")


# -- opening the panel --------------------------------------------------------


async def test_f3_opens_and_closes_the_sql_panel(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """The panel is opt-in, so it must not take grid space until it is asked for."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await _open(app, pilot)
        assert _panel(app).has_class("-hidden")

        await pilot.press("f3")
        await _settle(pilot)
        assert not _panel(app).has_class("-hidden")

        await pilot.press("f3")
        await _settle(pilot)
        assert _panel(app).has_class("-hidden")


async def test_the_panel_says_it_never_executes(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """S-1: looking at SQL must never leave the user in doubt about whether it writes."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await _open(app, pilot)
        await pilot.press("f3")
        await _settle(pilot)
        assert PREVIEW_ONLY_NOTE in _hint(app)


async def test_an_empty_panel_explains_itself(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await _open(app, pilot)
        await pilot.press("f3")
        await _settle(pilot)
        assert _summary(app) == "nothing staged"
        assert "no pending changes" in _body(app)
        assert len(_listing(app).children) == 0


# -- the three renderings -----------------------------------------------------


async def test_the_mode_key_cycles_the_rendering(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """``v`` toggles parameterized → literal → script (FR-5.2/5.3)."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Germany")
        assert screen.changes is not None and not screen.changes.is_empty

        seen = [screen.build_preview().mode]
        for _ in range(3):
            await pilot.press("v")
            await _settle(pilot)
            seen.append(screen.build_preview().mode)
        assert seen == [
            SqlMode.SCRIPT,  # the default: the rendering that is safe to run by hand
            SqlMode.PARAMETERIZED,
            SqlMode.LITERAL,
            SqlMode.SCRIPT,
        ]


async def test_the_script_mode_shows_an_all_or_nothing_transaction(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-5.3: the script is the rolled-back form, and the text has to show that."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Germany")
        await _set_mode(app, pilot, screen, SqlMode.SCRIPT)
        body = _body(app)
        assert "BEGIN TRANSACTION;" in body
        assert "ROLLBACK TRANSACTION;" in body
        assert "N'Germany'" in body


async def test_the_parameterized_mode_shows_placeholders_and_a_legend(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-5.2a: the parameterized view is what is sent, with the values as a legend."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Germany")
        await _set_mode(app, pilot, screen, SqlMode.PARAMETERIZED)
        assert "@p0" in _body(app)
        assert "Germany" not in _body(app)
        # The legend is what makes the parameterized view usable at all.
        assert "N'Germany'" in _legend(app)


async def test_the_literal_mode_inlines_and_escapes(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-5.2b: literal values, escaped — a quote in the data must not end the literal."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "O'Neil")
        await _set_mode(app, pilot, screen, SqlMode.LITERAL)
        assert "N'O''Neil'" in _body(app)


# -- the statement list -------------------------------------------------------


async def test_the_statement_list_has_one_row_per_change_plus_the_script(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Germany")
        await _set_mode(app, pilot, screen, SqlMode.LITERAL)
        listing = _listing(app)
        labels = [str(item.query_one(Static).render()) for item in listing.children]
        assert len(labels) == 2  # the UPDATE, and the whole-script row
        assert "UPDATE" in labels[0]
        assert "dbo.Country" in labels[0]
        assert "whole script" in labels[1]


# -- copying (FR-5.5) ---------------------------------------------------------


def _clipboard(app: App[None]) -> str:
    """What Textual put on the clipboard (``App.copy_to_clipboard`` records it here)."""
    return str(app.clipboard)


async def test_copying_puts_the_whole_script_on_the_clipboard(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """No selection means the whole change set, as a runnable transaction (FR-5.5)."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Germany")
        await pilot.press("f3")
        await _settle(pilot)
        await pilot.press("y")
        await _settle(pilot)
        copied = _clipboard(app)
        assert "BEGIN TRANSACTION;" in copied
        assert "COMMIT TRANSACTION;" in copied
        assert "N'Germany'" in copied
        assert copied == screen.build_preview().script()


async def test_copying_a_selected_statement_copies_only_that_one(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """A selected row copies that statement, not the whole script (FR-5.5)."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Germany")
        await _set_mode(app, pilot, screen, SqlMode.LITERAL)
        _panel(app).selected = 0
        await _settle(pilot)
        await pilot.press("y")
        await _settle(pilot)
        copied = _clipboard(app)
        assert "BEGIN TRANSACTION;" not in copied
        assert "N'Germany'" in copied


async def test_copying_nothing_says_so_instead_of_emptying_the_clipboard(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await _open(app, pilot)
        await pilot.press("f3")
        await _settle(pilot)
        await pilot.press("y")
        await _settle(pilot)
        screen = active_screen(app, TableEditorScreen)
        assert "nothing to copy" in str(screen.status.message)


# -- after Apply (FR-5.5) ------------------------------------------------------


async def _apply(app: App[None], pilot: Pilot[None]) -> None:
    """Commit the staged changes and let the re-fetch finish (FR-7.6, FR-7.7)."""
    await pilot.press("ctrl+s")
    await _settle(pilot)
    await pilot.press("y")
    for _ in range(SETTLE):
        await pilot.pause()


async def test_applying_keeps_the_statements_visible_as_ran_success(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-5.5: the SQL that ran stays on screen, labelled as having run.

    Before the fix the panel fell back to "nothing staged" the moment Apply committed,
    so the one moment where the user wants to re-read what was just written to the
    database was exactly the moment the panel went blank.
    """
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await _open(app, pilot)
        await _stage_name(app, pilot, "Germany")
        await pilot.press("f3")
        await _settle(pilot)
        assert _summary(app) == "1 statement · 1 update"

        await _apply(app, pilot)

        summary = _summary(app)
        assert summary.startswith("ran (success)")
        assert " ms" in summary
        assert "UPDATE" in _body(app)
        assert "Germany" in _body(app)
        labels = [str(item.query_one(Static).render()) for item in _listing(app).children]
        assert any("UPDATE" in label for label in labels)


async def test_staging_again_puts_the_plan_back_in_front(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """The panel owes the user what runs *next*; the run returns once staging is empty."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await _open(app, pilot)
        await _stage_name(app, pilot, "Germany")
        await _apply(app, pilot)
        # Opened after the run, so this also covers the panel being closed across Apply.
        await pilot.press("f3")
        await _settle(pilot)
        assert _summary(app).startswith("ran (success)")

        await _stage_name(app, pilot, "France")
        await _settle(pilot)
        assert _summary(app) == "1 statement · 1 update"
        assert "Germany" not in _body(app)
        assert "France" in _body(app)


# -- "generate SQL for…" (FR-5.6) --------------------------------------------


async def test_generate_offers_the_row_statements(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await _open(app, pilot)
        await pilot.press("g")
        await _settle(pilot)
        assert isinstance(app.screen, SqlActionScreen)
        listing = app.screen.query_one("#sql-action-list", ListView)
        labels = [str(item.query_one(Static).render()) for item in listing.children]
        assert "SELECT" in labels
        assert "INSERT" in labels
        assert "UPDATE" in labels
        assert "DELETE" in labels
        assert "MERGE (upsert by key)" in labels
        assert any("INSERT script for all rows" in label for label in labels)


async def test_generate_cancels_without_producing_anything(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await _open(app, pilot)
        await pilot.press("g")
        await _settle(pilot)
        await pilot.press("escape")
        await _settle(pilot)
        assert isinstance(app.screen, TableEditorScreen)


async def test_generating_a_select_shows_it_and_executes_nothing(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """The dry-run property: the panel produces text, the database is never touched."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await _open(app, pilot)
        # The fake provider records every statement it was asked to run; the panel
        # must not add to it, which is what "preview only" means in practice.
        provider = cast("FakeProvider", app.connection.provider())
        before = list(provider.executed)

        await pilot.press("g")
        await _settle(pilot)
        await pilot.press("enter")  # SELECT is the first entry
        await _settle(pilot)

        assert isinstance(app.screen, TableEditorScreen)
        assert "SELECT" in _summary(app)
        assert "[Country]" in _body(app)
        assert provider.executed == before  # nothing ran
