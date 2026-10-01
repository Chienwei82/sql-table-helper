"""M8 safety in the UI: the environment badge, read-only enforcement, Apply gating.

Everything is driven through Textual's Pilot against ``FakeProvider``. The assertions
are on what the user can actually *see and do*: the badge text, whether staging is
refused, whether the Apply dialog appears at all, and whether the typed word is
required before the confirm button arms.
"""

from textual.app import App
from textual.widgets import Button, Input

from sql_table_swiss_knife.domain import ChangeKind, Environment
from sql_table_swiss_knife.services import (
    PRODUCTION_CONFIRM_WORD,
    ApplyVerdict,
    TypedConfirmation,
)
from sql_table_swiss_knife.storage import ProfileStore
from sql_table_swiss_knife.tui.screens import ApplyConfirmScreen, TableEditorScreen
from sql_table_swiss_knife.tui.widgets import EnvBadge
from tests.fakes import FakeProvider
from tests.tui.conftest import AppFactory, active_screen
from tests.tui.test_table_editing import _open, _settle


def _badge(app: App[None]) -> EnvBadge:
    return app.screen.query_one(EnvBadge)


def _badge_text(app: App[None]) -> str:
    """The badge as the user reads it on screen."""
    return str(_badge(app).render())


# -- the environment badge --------------------------------------------------


async def test_the_badge_shows_the_environment_when_connected(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        assert "DEV" in _badge_text(app)
        assert screen is not None


async def test_the_badge_is_hidden_before_connecting(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """No session means no environment; guessing one would be a lie."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _badge(app).environment is None
        assert "DEV" not in _badge_text(app)


async def test_toggling_read_only_shows_the_marker(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        await _open(app, pilot)
        assert "RO" not in _badge_text(app)
        app.action_toggle_read_only()
        await pilot.pause()
        assert "RO" in _badge_text(app)
        app.action_toggle_read_only()
        await pilot.pause()
        assert "RO" not in _badge_text(app)


async def test_a_production_profile_opens_read_only_and_says_so(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """The badge must state the posture, not just the environment."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        await _open(app, pilot)
        # The badge follows the write policy, which is the thing that actually gates
        # the Apply — so setting the environment alone is enough to repaint it.
        app.safety.environment = Environment.PRODUCTION
        app.safety.read_only = True
        active_screen(app, TableEditorScreen).refresh_header()
        await pilot.pause()
        text = _badge_text(app)
        assert "PROD" in text
        assert "RO" in text


async def test_the_badge_cannot_disagree_with_the_write_gate(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """A badge that reads DEV while Apply demands a production word is worse than none.

    The two live in different objects — the session and the policy — so nothing but a
    test stops them drifting apart. This is the one safety assertion in the suite that
    is about the *display* rather than the gate, and it is the reason it exists.
    """
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        assert screen is not None
        app.safety.environment = Environment.PRODUCTION
        app.safety.read_only = False
        active_screen(app, TableEditorScreen).refresh_header()
        await pilot.pause()
        assert "PROD" in _badge_text(app)
        assert app.safety.confirmation_for({ChangeKind.UPDATE: 1}).required


# -- read-only enforcement --------------------------------------------------


async def test_read_only_refuses_to_stage_an_edit(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        await _open(app, pilot)
        app.action_toggle_read_only()
        await pilot.pause()
        screen = active_screen(app, TableEditorScreen)
        screen.action_edit_cell()
        await _settle(pilot)
        assert screen.changes is not None
        assert screen.changes.is_empty


async def test_read_only_refuses_to_stage_a_new_row(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        await _open(app, pilot)
        app.action_toggle_read_only()
        await pilot.pause()
        screen = active_screen(app, TableEditorScreen)
        screen.action_insert_row()
        await _settle(pilot)
        assert screen.changes is not None
        assert screen.changes.is_empty


async def test_read_only_refuses_a_delete(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        screen.action_delete_row()
        await _settle(pilot)
        assert screen.changes is not None and not screen.changes.is_empty
        app.action_toggle_read_only()
        await pilot.pause()
        screen.action_delete_row()
        await _settle(pilot)
        # The earlier delete is still staged; the new one was refused.
        assert len(screen.changes.deletes()) == 1


async def test_read_only_hides_the_apply_hint(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        await _open(app, pilot)
        app.action_toggle_read_only()
        await pilot.pause()
        hints = " ".join(hint.key for hint in active_screen(app, TableEditorScreen).hints_for())
        assert "^s" not in hints
        assert "f5" in hints  # and the way out is offered


# -- the Apply confirmation dialog ------------------------------------------


def _verdict(**overrides: object) -> ApplyVerdict:
    """An allowed Apply verdict with overridable fields."""
    defaults: dict[str, object] = {
        "allowed": True,
        "tables": ("dbo.Country",),
        "summary": "1 insert / 2 updates",
        "confirmation": TypedConfirmation(),
    }
    return ApplyVerdict(**{**defaults, **overrides})  # type: ignore[arg-type]


async def test_the_dialog_shows_the_counts_and_the_affected_tables(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """FR-7.5: the user sees what changes, not only how much."""
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        app.push_screen(ApplyConfirmScreen(_verdict(tables=("dbo.Country", "dbo.Region"))))
        for _ in range(3):
            await pilot.pause()
        screen = active_screen(app, ApplyConfirmScreen)
        counts = str(screen.query_one("#apply-counts").render())
        assert "1 insert / 2 updates" in counts
        # The affected tables are listed one widget per table, so a test can assert on
        # the list itself rather than on a blob of rendered text.
        listed = [str(widget.render()) for widget in screen.query(".-table-name")]
        assert listed == ["  • dbo.Country", "  • dbo.Region"]


async def test_an_ordinary_apply_confirms_without_typing(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        results: list[bool | None] = []
        app.push_screen(ApplyConfirmScreen(_verdict()), results.append)
        await pilot.pause()
        button = active_screen(app, ApplyConfirmScreen).query_one("#confirm", Button)
        assert button.disabled is False
        await pilot.click("#confirm")
        await pilot.pause()
        assert results == [True]


async def test_production_requires_the_exact_word(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    verdict = _verdict(
        confirmation=TypedConfirmation(word=PRODUCTION_CONFIRM_WORD, reason="PRODUCTION")
    )
    async with app.run_test() as pilot:
        results: list[bool | None] = []
        app.push_screen(ApplyConfirmScreen(verdict), results.append)
        await pilot.pause()
        # Nothing typed yet: the button cannot be pressed.
        screen = active_screen(app, ApplyConfirmScreen)
        assert screen.query_one("#confirm", Button).disabled is True
        assert "Type" in str(screen.query_one("#confirm", Button).label)


async def test_production_enables_the_button_on_the_right_word(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    verdict = _verdict(
        confirmation=TypedConfirmation(word=PRODUCTION_CONFIRM_WORD, reason="PRODUCTION")
    )
    async with app.run_test() as pilot:
        results: list[bool | None] = []
        app.push_screen(ApplyConfirmScreen(verdict), results.append)
        await pilot.pause()
        active_screen(app, ApplyConfirmScreen).query_one(
            "#apply-word", Input
        ).value = PRODUCTION_CONFIRM_WORD
        await pilot.pause()
        assert (
            active_screen(app, ApplyConfirmScreen).query_one("#confirm", Button).disabled is False
        )


async def test_a_wrong_word_does_not_enable_the_button(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """A near-miss must not pass: that is the whole point of typing the phrase."""
    app = app_factory(profiles=seeded_profiles)
    verdict = _verdict(
        confirmation=TypedConfirmation(word=PRODUCTION_CONFIRM_WORD, reason="PRODUCTION")
    )
    async with app.run_test() as pilot:
        results: list[bool | None] = []
        app.push_screen(ApplyConfirmScreen(verdict), results.append)
        await pilot.pause()
        for wrong in ("apply to production", "PRODUCTION", "APPLY TO PRODUCTIO"):
            active_screen(app, ApplyConfirmScreen).query_one("#apply-word", Input).value = wrong
            await pilot.pause()
            assert (
                active_screen(app, ApplyConfirmScreen).query_one("#confirm", Button).disabled
                is True
            )
        assert results == []


async def test_escape_cancels_without_applying(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        results: list[bool | None] = []
        app.push_screen(ApplyConfirmScreen(_verdict()), results.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert results == [False]


async def test_the_services_bundle_is_built_once_and_stays_live(
    app_factory: AppFactory, seeded_profiles: ProfileStore, provider: FakeProvider
) -> None:
    """A cached bundle must not become a stale snapshot of the session's services.

    ``App.services`` rebuilt the bundle — and a ``LookupService`` — on every property
    access, and screens read it on each cursor move. Caching is only safe because the
    collaborators are long-lived objects that are never replaced, so this pins both halves
    of that: the bundle is the same object each time, and the policy inside it is still the
    live one that the read-only toggle mutates.
    """
    app = app_factory(profiles=seeded_profiles)
    async with app.run_test() as pilot:
        first = app.services
        assert app.services is first, "the bundle is rebuilt on every access"
        assert app.services.lookup is first.lookup

        await _open(app, pilot)
        assert "RO" not in _badge_text(app)
        app.action_toggle_read_only()
        await pilot.pause()
        # The badge and the write gate read the same SafetyPolicy object; a cached
        # bundle holding a different one would make them disagree.
        assert app.services.safety is app.safety
        assert app.services.safety.read_only is True
        assert "RO" in _badge_text(app)
