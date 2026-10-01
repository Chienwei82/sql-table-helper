"""ConnectionsScreen flows: list, add/edit, duplicate, delete, test, connect (FR-1)."""

from textual.app import App
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Static

from sql_table_swiss_knife.providers import ConnectError
from sql_table_swiss_knife.services import ConnectionState
from sql_table_swiss_knife.tui.screens import (
    ConfirmScreen,
    ConnectionsScreen,
    DatabasePickerScreen,
    ProfileEditScreen,
    TableBrowserScreen,
)
from sql_table_swiss_knife.tui.widgets import KeyHints
from tests.fakes import FakeProvider
from tests.tui.conftest import (
    AppFactory,
    ProfileStore,
    active_screen,
    demo_profile,
    no_database_profile,
)


def _rows(app: App[None]) -> list[str]:
    """First column of every row of the profile table."""
    table = app.screen.query_one("#profile-table", DataTable)
    return [str(table.get_row_at(row)[0]) for row in range(table.row_count)]


def _hints(app: App[None]) -> str:
    """The footer text of the active screen."""
    return str(app.screen.query_one(KeyHints).render())


async def test_starts_on_the_connections_screen_with_the_profiles(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ConnectionsScreen)
        assert _rows(app) == ["catalog"]
        assert active_screen(app, ConnectionsScreen).header.title == "Connections"
        assert "offline" in active_screen(app, ConnectionsScreen).header.session_label


async def test_empty_state_explains_how_to_add_a_profile(app_factory: AppFactory) -> None:
    app = app_factory()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        empty = app.screen.query_one("#profile-empty", Static)
        assert empty.display is True
        assert "No connection profiles yet" in str(empty.render())
        assert _rows(app) == []


async def test_footer_hints_are_context_sensitive(app_factory, seeded_profiles) -> None:  # type: ignore[no-untyped-def]
    """The footer describes what the *current* state offers (DESIGN §9.2)."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        assert "connect" in _hints(app)
        assert "duplicate" in _hints(app)
        assert "new profile" not in _hints(app)


async def test_test_connection_reports_success_without_opening_a_session(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-1.3: the probe reports the server and leaves no session behind."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        await pilot.pause()
        assert active_screen(app, ConnectionsScreen).status.level == "success"
        assert "localhost:1433" in active_screen(app, ConnectionsScreen).status.message
        assert not app.connection.is_connected


async def test_test_connection_failure_becomes_an_error_toast(app_factory: AppFactory) -> None:
    """A dead server must not crash the list; it becomes a status line + toast."""

    def failing(name: str) -> FakeProvider:
        provider = FakeProvider()

        async def boom(profile, password=None):  # type: ignore[no-untyped-def]
            del profile, password
            raise ConnectError("cannot reach sql01")

        provider.connect = boom  # type: ignore[method-assign]
        return provider

    app = app_factory(provider_factory=failing)
    app.connection.save_profile(demo_profile())
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        active_screen(app, ConnectionsScreen)._load_profiles()
        await pilot.press("t")
        await pilot.pause()
        await pilot.pause()
        assert active_screen(app, ConnectionsScreen).status.level == "error"
        assert "cannot reach sql01" in active_screen(app, ConnectionsScreen).status.message
        assert app.connection.state is ConnectionState.ERROR


async def test_connect_opens_the_table_browser(app_factory, seeded_profiles) -> None:  # type: ignore[no-untyped-def]
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, TableBrowserScreen)
        assert app.connection.is_connected
        assert app.connection.session is not None
        assert app.connection.session.profile_name == "catalog"


async def test_connecting_without_a_database_opens_the_picker(app_factory, seeded_profiles) -> None:  # type: ignore[no-untyped-def]
    """FR-1.4: a profile without a default database must ask which one to use."""
    store = seeded_profiles
    store.save_all((no_database_profile(),))
    app = app_factory(profiles=store)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DatabasePickerScreen)
        for _ in range(4):
            await pilot.pause()
        table = app.screen.query_one("#db-table", DataTable)
        assert table.row_count == 1  # the fake server offers "test"
        await pilot.press("enter")  # confirm the highlighted database
        for _ in range(4):
            await pilot.pause()
        # The choice is remembered on the profile and the picker hands us on.
        assert store.get("no-db").database == "test"
        assert isinstance(app.screen, TableBrowserScreen)


async def test_database_picker_can_be_cancelled(app_factory, seeded_profiles) -> None:  # type: ignore[no-untyped-def]
    store = seeded_profiles
    store.save_all((no_database_profile(),))
    app = app_factory(profiles=store)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ConnectionsScreen)
        assert store.get("no-db").database is None  # nothing was changed


async def test_duplicate_profile_creates_a_copy(app_factory, seeded_profiles) -> None:  # type: ignore[no-untyped-def]
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert _rows(app) == ["catalog", "catalog-copy"]
        assert seeded_profiles.get("catalog-copy").secret_ref == "catalog-copy@localhost"


async def test_delete_asks_for_confirmation_and_can_be_cancelled(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Nothing destructive happens without an explicit yes (S-2 spirit)."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await pilot.pause()
        assert _rows(app) == ["catalog"]

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert _rows(app) == []


async def test_new_profile_opens_the_editor_and_saves_it(app_factory, seeded_profiles) -> None:  # type: ignore[no-untyped-def]
    """FR-1.1: the form writes the profile file and the list refreshes."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, ProfileEditScreen)
        editor.query_one("#f-name", Input).value = "staging"
        editor.query_one("#f-host", Input).value = "sql02"
        editor.query_one("#f-username", Input).value = "reader"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, ConnectionsScreen)
        assert _rows(app) == ["catalog", "staging"]
        assert seeded_profiles.get("staging").host == "sql02"


async def test_profile_editor_rejects_an_invalid_form(app_factory: AppFactory) -> None:
    """Validation happens in the form, with a message, before anything is saved."""
    app = app_factory()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, ProfileEditScreen)
        editor.query_one("#f-name", Input).value = "staging"
        editor.query_one("#f-host", Input).value = "sql02"
        editor.query_one("#f-username", Input).value = "reader"
        editor.query_one("#f-port", Input).value = "not-a-number"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, ProfileEditScreen)  # still open: nothing saved
        assert "port must be a number" in str(editor.query_one("#profile-hint").render())


# -- layout of the connection form (the modal is a form, so its geometry is behaviour) --

#: A terminal too short for the whole form at once. The dialog used to size itself to
#: its content and let ``overflow-y: auto`` clip it, which pushed Cancel/Save off the
#: bottom of an 80x24 window: the user could not tell whether the dialog had more.
SHORT: tuple[int, int] = (80, 24)


def _visible_on_screen(editor: ProfileEditScreen, widget: Widget) -> bool:
    """Whether a widget's region falls inside its parent's visible region.

    Compared as rectangles rather than by ``display``, because a widget scrolled out of
    an overflowing container is still "displayed" as far as Textual is concerned — the
    distinction is exactly the defect this guards.
    """
    return editor.region.contains_region(widget.region)


async def test_profile_editor_keeps_its_actions_visible_on_a_short_terminal(
    app_factory: AppFactory,
) -> None:
    """The Save/Cancel buttons must be on screen even when the fields are not.

    Clipped action buttons are worse than an ugly form: the dialog looks complete and
    ``ctrl+s`` is the only visible way out, which a mouse user never finds.
    """
    app = app_factory()
    async with app.run_test(size=SHORT) as pilot:
        await pilot.press("n")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, ProfileEditScreen)
        for selector in ("#save", "#cancel"):
            button = editor.query_one(selector, Button)
            assert _visible_on_screen(editor, button), f"{selector} is clipped off the modal"


async def test_profile_editor_form_controls_share_one_column(app_factory: AppFactory) -> None:
    """Every field control starts at the same x — the form reads as one column.

    The grid declared three columns but sized only two, so cells filled
    ``[label, input, label]`` and the second control of each pair started in the label
    column. The visible result was a zig-zag with alternating field widths.
    """
    app = app_factory()
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, ProfileEditScreen)
        # Text inputs only: a Select is three rows tall and its box, not its text, is
        # what this asserts on, so including it would test the wrong edge.
        lefts = {
            editor.query_one(selector, Input).region.x
            for selector in ("#f-name", "#f-provider", "#f-host", "#f-port", "#f-username")
        }
        assert len(lefts) == 1, f"field controls start at {sorted(lefts)}"


async def test_profile_editor_labels_are_not_truncated(app_factory: AppFactory) -> None:
    """A label is fully readable, or it is a defect the user cannot report usefully.

    Asserting the *rendered* width against the *assigned* width is what caught both
    ways this breaks: a label wider than its column, and a checkbox whose long caption
    was cut to "Force read-only (production o…".
    """
    app = app_factory()
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, ProfileEditScreen)
        # A selector that matches nothing would make this pass vacuously, which is how a
        # layout test rots: assert the census first.
        labels = list(editor.query("Label, Checkbox"))
        assert len(labels) == 14, f"expected the full form, found {len(labels)} captions"
        clipped = [
            f"{widget!r} needs {widest_line(str(widget.render()))} of {widget.region.width}"
            for widget in labels
            if widest_line(str(widget.render())) > widget.region.width
        ]
        assert clipped == []


def widest_line(text: str) -> int:
    """Longest line of rendered text, in cells."""
    return max((len(line) for line in text.splitlines()), default=0)
