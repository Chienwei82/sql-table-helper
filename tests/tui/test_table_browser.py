"""TableBrowserScreen: search-as-you-type, schema groups, badges, navigation (FR-2)."""

from textual.app import App
from textual.widgets import Input

from sql_table_swiss_knife.storage import ProfileStore
from sql_table_swiss_knife.tui.screens import TableBrowserScreen, TableEditorScreen
from sql_table_swiss_knife.tui.widgets import DataGrid, InspectorPanel, TableTree
from tests.tui.conftest import AUDIT, SAMPLE_TABLES, AppFactory, active_screen


async def _connected(app: App[None], pilot: object) -> None:
    """Connect with the seeded profile and settle on the table browser."""
    await app.connection.connect(app.connection.get_profile("catalog"))  # type: ignore[attr-defined]
    app.push_screen(TableBrowserScreen())
    for _ in range(4):
        await pilot.pause()  # type: ignore[attr-defined]


def _tree(app: App[None]) -> TableTree:
    return app.screen.query_one("#table-tree", TableTree)


def _label_text(label: object) -> str:
    """Tree labels are ``str`` for headers and ``Text`` for rows; flatten both."""
    return str(getattr(label, "plain", label))


def _labels(app: App[None]) -> list[str]:
    """Every schema header of the tree, as rendered."""
    return [_label_text(node.label) for node in _tree(app).root.children]


def _leaf_labels(app: App[None]) -> list[str]:
    """Every table row of the tree, as rendered."""
    return [
        _label_text(child.label) for group in _tree(app).root.children for child in group.children
    ]


async def test_lists_objects_grouped_by_schema(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-2.2: schema headers with their object count, tables underneath."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _connected(app, pilot)
        assert isinstance(app.screen, TableBrowserScreen)
        assert _labels(app) == ["dbo (2)", "docs (1)", "reporting (1)", "sales (1)"]
        assert _tree(app).selected_summary() == AUDIT.summary  # first row of dbo


async def test_badges_show_pk_fk_trigger_and_missing_pk(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-2.3: 🔑 primary key, 🔗 foreign keys, ⚡ triggers, ⚠ no primary key, 👁 view."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _connected(app, pilot)
        by_name = {label.split(" ")[0]: label for label in _leaf_labels(app)}

        assert "🔑" in by_name["Country"] and "⚠" not in by_name["Country"]
        assert "⚠" in by_name["AuditLog"]  # no primary key -> read-only (S-4)
        assert "⚡" in by_name["AuditLog"]  # it does have a trigger
        assert "⚡" in by_name["Order"]
        assert "👁" in by_name["v_Customer"]  # views are read-only


async def test_filter_narrows_instantly_and_drops_empty_schemas(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-2.1: typing filters the in-memory listing; empty schemas disappear."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _connected(app, pilot)
        await pilot.press("/")
        await pilot.press(*"order")
        await pilot.pause()
        assert _labels(app) == ["sales (1)"]
        assert _leaf_labels(app)[0].startswith("Order")

        await pilot.press(*"zzzz")
        await pilot.pause()
        assert _labels(app) == []
        assert "nothing matches" in str(app.screen.query_one("#table-empty").render())


async def test_filter_matches_the_schema_name_too(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _connected(app, pilot)
        app.screen.query_one("#table-filter", Input).value = "reporting"
        await pilot.pause()
        assert _labels(app) == ["reporting (1)"]


async def test_count_line_reports_matches_and_total(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _connected(app, pilot)
        count = str(app.screen.query_one("#table-count").render())
        assert f"{len(SAMPLE_TABLES)}/{len(SAMPLE_TABLES)}" in count
        assert "4 schema(s)" in count


async def test_escape_leaves_the_filter_before_the_screen(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Escape must never trap the user: it backs out one level at a time."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _connected(app, pilot)
        await pilot.press("/")
        await pilot.press(*"order")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, TableBrowserScreen)  # focus moved back to the tree
        assert app.screen.query_one("#table-filter", Input).value == "order"  # filter kept


async def test_opening_a_table_pushes_the_editor_screen(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-2.4: enter on a table row opens the split view; escape comes back."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _connected(app, pilot)
        assert _tree(app).selected_summary() == AUDIT.summary  # first row of dbo
        await pilot.press("enter")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, TableEditorScreen)
        # M4: the workspace is a grid plus the inspector panel, not a title line.
        assert app.screen.query_one("#editor-grid", DataGrid).table == AUDIT
        assert not app.screen.query_one("#editor-inspector", InspectorPanel).collapsed

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, TableBrowserScreen)


async def test_schema_headers_are_not_openable(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Selecting a header explains itself instead of opening nothing."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _connected(app, pilot)
        tree = _tree(app)
        tree.move_cursor(tree.root.children[0])  # the "dbo" header
        await pilot.pause()
        assert tree.selected_summary() is None
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TableBrowserScreen)
        assert "select a table first" in app.screen.status.message


async def test_f6_collapses_and_expands_schema_groups(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _connected(app, pilot)
        tree = _tree(app)
        assert all(group.is_expanded for group in tree.root.children)
        await pilot.press("f6")
        await pilot.pause()
        assert not any(group.is_expanded for group in tree.root.children)
        await pilot.press("f6")
        await pilot.pause()
        assert all(group.is_expanded for group in tree.root.children)


async def test_reload_rereads_the_listing(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _connected(app, pilot)
        app.services.catalog.invalidate()
        await pilot.press("r")
        for _ in range(4):
            await pilot.pause()
        assert len(_leaf_labels(app)) == len(SAMPLE_TABLES)


async def test_browser_without_a_connection_offers_a_way_back(
    app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Losing the session mid-flow must offer a reconnect path, not a crash."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.push_screen(TableBrowserScreen())
        await pilot.pause()
        assert "Not connected" in str(app.screen.query_one("#table-empty").render())
        assert active_screen(app, TableBrowserScreen).status.level == "error"
