"""M8 robustness in the UI: expand view, frozen PK columns, reconnect on connection loss.

Pilot tests against ``FakeProvider``. The load-bearing one is
``test_a_lost_connection_keeps_staged_changes``: a dropped link must not cost the user
their unsaved work, and that is exactly the property a screenshot cannot show.
"""

from textual.app import App
from textual.coordinate import Coordinate
from textual.widgets import TextArea

from sql_table_swiss_knife.services.connection import is_connection_lost
from sql_table_swiss_knife.storage import ProfileStore
from sql_table_swiss_knife.tui.screens import CellViewScreen, TableEditorScreen
from sql_table_swiss_knife.tui.widgets import DataGrid
from tests.fakes import FakeProvider
from tests.tui.conftest import AppFactory, active_screen
from tests.tui.test_table_editing import _open, _settle, _stage_name


def _grid(app: App[None]) -> DataGrid:
    return app.screen.query_one("#editor-grid", DataGrid)


async def _focus(app: App[None], pilot: object, row: int, column: int) -> None:
    """Move the grid cursor and let the overlay follow."""
    grid = _grid(app)
    grid.cursor_coordinate = Coordinate(row, column)
    await pilot.pause()  # type: ignore[attr-defined]


# -- the frozen PK columns (wide tables) ------------------------------------


async def test_the_primary_key_columns_are_frozen(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """On a wide table the identity column must survive a horizontal scroll."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        await _open(app, pilot)
        assert _grid(app).frozen_count == 1


async def test_a_wide_table_freezes_only_the_identity(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """A 12-column table must not freeze its ten attribute columns."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        await _open(app, pilot, name="Notes")
        assert len(_grid(app).visible_columns) == 12
        assert _grid(app).frozen_count == 1


async def test_a_table_without_a_key_freezes_nothing(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """Freezing an arbitrary data column would break the guarantee the freeze is for."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        await _open(app, pilot, name="AuditLog")
        assert _grid(app).frozen_count == 0


async def test_the_freeze_follows_the_hidden_columns(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """Hiding the PK column must move the freeze, not freeze a data column instead."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        screen._on_columns_chosen(frozenset({"Code"}))
        await _settle(pilot)
        assert _grid(app).frozen_count == 0


# -- the expand view (long text and binary cells) ---------------------------


async def test_a_short_cell_reports_nothing_to_expand(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        screen.action_expand_cell()
        await _settle(pilot)
        # No dialog: a cell with nothing to show says so instead of opening one.
        assert not isinstance(app.screen, CellViewScreen)


async def test_a_long_cell_opens_the_expand_view(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot, name="Notes")
        await _focus(app, pilot, 0, 1)  # the Body column
        screen.action_expand_cell()
        await _settle(pilot)
        assert isinstance(app.screen, CellViewScreen)
        body = str(app.screen.query_one(TextArea).text)
        assert "End of the description." in body  # the tail the grid cannot show


async def test_the_expand_view_is_read_only(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """Expanding reads; editing goes through the cell editor, which validates."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot, name="Notes")
        await _focus(app, pilot, 0, 1)
        screen.action_expand_cell()
        await _settle(pilot)
        assert app.screen.query_one(TextArea).read_only is True


async def test_a_binary_cell_expands_to_a_hex_dump(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _focus(app, pilot, 0, 4)  # Country.RowVer, served as bytes
        screen.action_expand_cell()
        await _settle(pilot)
        assert isinstance(app.screen, CellViewScreen)
        body = str(app.screen.query_one(TextArea).text)
        assert body.startswith("00000000") and "|" in body  # offset/hex/ASCII


async def test_escape_closes_the_expand_view(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot, name="Notes")
        await _focus(app, pilot, 0, 1)
        screen.action_expand_cell()
        await _settle(pilot)
        await pilot.press("escape")
        await _settle(pilot)
        assert isinstance(active_screen(app, TableEditorScreen), TableEditorScreen)


# -- connection loss (FR-10) ------------------------------------------------


async def test_a_lost_connection_offers_a_reconnect(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """A dead session gets a reconnect prompt, not a raw driver error (FR-10)."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        screen._handle_connection_lost(ConnectionResetError("peer went away"))
        await _settle(pilot)
        message = str(app.screen.query_one("#confirm-message").render()).lower()
        assert "staged changes" in message
        assert "reconnect" in message


async def test_a_lost_connection_keeps_the_staged_changes(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """The staging area is the only copy of the user's unsaved work."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Before the outage")
        assert screen.changes is not None and not screen.changes.is_empty

        screen._handle_connection_lost(ConnectionResetError("peer went away"))
        await _settle(pilot)

        assert screen.changes is not None
        assert screen.changes.is_empty is False
        assert len(screen.changes.updates()) == 1


async def test_declining_the_reconnect_still_keeps_the_work(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Still here")
        screen._handle_connection_lost(ConnectionResetError("peer went away"))
        await _settle(pilot)
        await pilot.press("n")
        await _settle(pilot)
        assert screen.changes is not None
        assert screen.changes.is_empty is False


async def test_reconnecting_reloads_the_table_and_keeps_the_staging(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """After the reconnect the rows are re-read and the edits ride on top."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "After the outage")
        screen.reconnect()
        for _ in range(8):
            await pilot.pause()
        assert screen.changes is not None
        assert screen.changes.is_empty is False
        assert len(screen.changes.updates()) == 1


async def test_reconnecting_re_reads_the_rows(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """The grid must show live data again, not the rows from before the outage."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        screen.reconnect()
        for _ in range(8):
            await pilot.pause()
        assert screen.connection.is_connected is True
        assert len(_grid(app).fetched_rows) == 5


async def test_an_ordinary_query_error_is_not_treated_as_a_lost_connection(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """A constraint violation must not send the user off to reconnect for nothing."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        assert (
            is_connection_lost(
                "The INSERT statement conflicted with the FOREIGN KEY constraint FK_x"
            )
            is False
        )
        assert screen.connection_lost is False


async def test_a_manual_reload_keeps_the_staged_edits(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """``r`` re-reads the table like a reconnect does, and must be equally non-destructive.

    The same code path serves both, so this is the assertion that the fix is about
    reloading in general rather than about one caller.
    """
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _stage_name(app, pilot, "Survives a reload")
        screen.load_metadata()
        for _ in range(8):
            await pilot.pause()
        assert screen.changes is not None
        assert screen.changes.is_empty is False
        assert len(screen.changes.updates()) == 1
