"""Textual snapshot tests for the M3 screens (DESIGN §11).

The SVG snapshots are the visual contract of the milestone: they catch a regression in
layout, badges, hints or theming that a behavioural assertion would happily miss.

Regenerate them deliberately with::

    uv run pytest tests/tui/test_snapshots.py --snapshot-update

and *look at the diff* before accepting: the snapshots are reviewed artefacts, not
rubbish to be auto-accepted.
"""

from collections.abc import Callable
from pathlib import Path

from textual.app import App
from textual.pilot import Pilot

from sql_table_swiss_knife.providers import ConnectError
from sql_table_swiss_knife.storage import ProfileStore
from sql_table_swiss_knife.tui.app import SwissKnifeApp
from sql_table_swiss_knife.tui.screens import TableBrowserScreen, TableEditorScreen
from tests.fakes import FakeProvider
from tests.tui.conftest import AUDIT, SAMPLE_TABLES, AppFactory, no_database_profile

#: Signature of pytest-textual-snapshot's ``snap_compare`` fixture.
#:
#: The fixture is untyped upstream, hence the callable alias rather than a direct import.
SnapCompare = Callable[..., bool]

#: Fixed terminal size for every snapshot, so the SVGs are comparable.
SIZE: tuple[int, int] = (110, 32)


async def _connect(pilot: Pilot[App[None]]) -> None:
    """Connect the app and settle on the table browser."""
    app = pilot.app
    assert isinstance(app, SwissKnifeApp)
    await app.connection.connect(app.connection.get_profile("catalog"))
    app.push_screen(TableBrowserScreen())
    await pilot.pause()
    for _ in range(4):
        await pilot.pause()


async def _settle(pilot: Pilot[App[None]], times: int = 4) -> None:
    """Pause repeatedly so background workers finish before the screenshot."""
    for _ in range(times):
        await pilot.pause()


def test_connections_screen_empty(snap_compare: SnapCompare, app_factory: AppFactory) -> None:
    """The first-run state: no profiles, and an empty state that says what to do."""
    snap_compare(app_factory(), terminal_size=SIZE)


def test_connections_screen_with_profiles(
    snap_compare: SnapCompare, app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    snap_compare(app_factory(profiles=seeded_profiles), terminal_size=SIZE)


def test_connections_screen_connected(
    snap_compare: SnapCompare, app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """The header and footer change once a session exists (context-sensitive chrome)."""

    async def run_before(pilot: Pilot[App[None]]) -> None:
        app = pilot.app
        assert isinstance(app, SwissKnifeApp)
        await app.connection.connect(seeded_profiles.get("catalog"))
        await pilot.pause()

    snap_compare(app_factory(profiles=seeded_profiles), terminal_size=SIZE, run_before=run_before)


def test_table_browser(
    snap_compare: SnapCompare, app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Schema groups, kind/PK/trigger/FK badges and the row estimate (FR-2)."""

    async def run_before(pilot: Pilot[App[None]]) -> None:
        await _connect(pilot)

    snap_compare(app_factory(profiles=seeded_profiles), terminal_size=SIZE, run_before=run_before)


def test_table_browser_filtered(
    snap_compare: SnapCompare, app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Search-as-you-type: immediate feedback, no database round trip."""

    async def run_before(pilot: Pilot[App[None]]) -> None:
        await _connect(pilot)
        await pilot.press("/")
        await pilot.press(*"ord")
        await pilot.pause()

    snap_compare(app_factory(profiles=seeded_profiles), terminal_size=SIZE, run_before=run_before)


def test_table_browser_no_matches(
    snap_compare: SnapCompare, app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """A filter with no hits says so instead of showing an empty tree."""

    async def run_before(pilot: Pilot[App[None]]) -> None:
        await _connect(pilot)
        await pilot.press("/")
        await pilot.press(*"zzz")
        await pilot.pause()

    snap_compare(app_factory(profiles=seeded_profiles), terminal_size=SIZE, run_before=run_before)


def test_table_editor_placeholder(
    snap_compare: SnapCompare, app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """The M3 editor: object identity, badges, and an honest "M4 comes next" note."""

    async def run_before(pilot: Pilot[App[None]]) -> None:
        app = pilot.app
        assert isinstance(app, SwissKnifeApp)
        await app.connection.connect(seeded_profiles.get("catalog"))
        app.push_screen(TableEditorScreen(AUDIT.summary))
        await _settle(pilot)

    snap_compare(app_factory(profiles=seeded_profiles), terminal_size=SIZE, run_before=run_before)


def _snapshot_in_theme(
    snap_compare: SnapCompare,
    app_factory: AppFactory,
    seeded_profiles: ProfileStore,
    theme: str,
) -> None:
    """The same screen in another theme: the semantic palette must hold up (FR-3.7)."""

    async def run_before(pilot: Pilot[App[None]]) -> None:
        app = pilot.app
        assert isinstance(app, SwissKnifeApp)
        app.set_theme(theme)
        await pilot.pause()
        await _connect(pilot)

    snap_compare(app_factory(profiles=seeded_profiles), terminal_size=SIZE, run_before=run_before)


def test_light_theme_table_browser(
    snap_compare: SnapCompare, app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    _snapshot_in_theme(snap_compare, app_factory, seeded_profiles, "light")


def test_high_contrast_theme_table_browser(
    snap_compare: SnapCompare, app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    _snapshot_in_theme(snap_compare, app_factory, seeded_profiles, "high-contrast")


def test_database_picker(
    snap_compare: SnapCompare, app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """FR-1.4: the modal shown when a profile has no default database."""
    seeded_profiles.save_all((no_database_profile(),))

    async def run_before(pilot: Pilot[App[None]]) -> None:
        await pilot.press("enter")
        await _settle(pilot)

    snap_compare(app_factory(profiles=seeded_profiles), terminal_size=SIZE, run_before=run_before)


def test_command_palette(
    snap_compare: SnapCompare, app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """Ctrl+P: the fuzzy action list of the active screen."""

    async def run_before(pilot: Pilot[App[None]]) -> None:
        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press(*"te")
        await pilot.pause()

    snap_compare(app_factory(profiles=seeded_profiles), terminal_size=SIZE, run_before=run_before)


def test_profile_editor(snap_compare: SnapCompare, app_factory: AppFactory) -> None:
    """The connection form, including the keyring hint under the password field."""

    async def run_before(pilot: Pilot[App[None]]) -> None:
        await pilot.press("n")
        await _settle(pilot)

    snap_compare(app_factory(), terminal_size=SIZE, run_before=run_before)


def test_error_toast(
    snap_compare: SnapCompare, app_factory: AppFactory, seeded_profiles: ProfileStore
) -> None:
    """A failed probe is visible twice: status line and toast (DESIGN §12)."""

    def failing(name: str) -> FakeProvider:
        provider = FakeProvider(SAMPLE_TABLES)

        async def boom(profile, password=None):  # type: ignore[no-untyped-def]
            del profile, password
            raise ConnectError("login failed for user 'sa'")

        provider.connect = boom  # type: ignore[method-assign]
        return provider

    async def run_before(pilot: Pilot[App[None]]) -> None:
        await pilot.press("t")
        await _settle(pilot)

    snap_compare(
        app_factory(profiles=seeded_profiles, provider_factory=failing),
        terminal_size=SIZE,
        run_before=run_before,
    )


def test_snapshots_live_beside_the_tests() -> None:
    """The single-file SVG extension writes into ``__snapshots__`` next to the test."""
    assert (Path(__file__).parent / "__snapshots__").is_dir()
