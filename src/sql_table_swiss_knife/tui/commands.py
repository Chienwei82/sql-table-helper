"""Command palette (ctrl+p): every action of the active screen, fuzzy-searched.

Textual's palette already provides the modal, the input and the fuzzy matcher; this
module contributes the *content*. Each screen publishes its actions through
:meth:`Screen.command_actions` (see ``AppScreen``), and this provider searches them
plus the app-wide actions. Because the palette searches whatever the active screen
offers, the shortcuts are discoverable exactly where they apply.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

from textual.command import DiscoveryHit, Hit, Hits, Provider

from .theme import theme_names

if TYPE_CHECKING:  # pragma: no cover - typing only
    from textual.screen import Screen

    from .app import SwissKnifeApp

__all__ = ["Action", "ActionProvider", "ScreenCommand"]


@dataclass(frozen=True, slots=True)
class Action:
    """One palette entry: a label, a category for grouping, and a callback."""

    name: str
    category: str
    help: str
    callback: Callable[[], None]
    discover: bool = True

    @property
    def display(self) -> str:
        """What the user reads in the palette: ``name  (category)``."""
        return f"{self.name}  ({self.category})"


@dataclass(frozen=True, slots=True)
class ScreenCommand:
    """A key binding exposed as a palette action.

    Keeping the key alongside the action means the palette and the footer can never
    drift apart: both are generated from the same declaration.
    """

    key: str
    name: str
    category: str
    help: str
    callback: Callable[[], None]


class ActionProvider(Provider):
    """Fuzzy search over the active screen's actions and the app-wide actions."""

    async def search(self, query: str) -> Hits:
        """Yield scored hits for every action matching ``query``."""
        matcher = self.matcher(query)
        for action in self._actions():
            score = matcher.match(action.name)
            if score <= 0:
                continue
            yield Hit(
                score=score,
                match_display=matcher.highlight(action.display),
                text=action.display,
                help=action.help,
                command=action.callback,
            )

    async def discover(self) -> Hits:
        """Show every action, in declaration order, before the user types."""
        for action in self._actions():
            if not action.discover:
                continue
            yield DiscoveryHit(
                display=action.display,
                text=action.display,
                help=action.help,
                command=action.callback,
            )

    def _actions(self) -> list[Action]:
        """Screen actions first (most relevant), then the app-wide ones.

        Textual's own :class:`SystemCommandsProvider` is deliberately *not* registered
        (see ``App.COMMANDS``): the palette runs one task per provider and merges their
        hits into a single queue, so with two providers the arrival order — and therefore
        the rendered order of equally-scored hits — depends on task scheduling. Folding
        the system commands in here keeps a single producer and a deterministic list.
        """
        actions: list[Action] = list(_screen_actions(self.screen))
        actions.extend(_app_actions(cast("SwissKnifeApp", self.app)))
        actions.extend(_system_actions(cast("SwissKnifeApp", self.app), self.screen))
        return actions


def schedule(app: SwissKnifeApp, action: Callable[[], Awaitable[None]]) -> Callable[[], None]:
    """Adapt a coroutine action to the palette's synchronous callback signature.

    The palette calls the callback and ignores its result, so a coroutine function
    would be created and dropped. Handing it to ``call_later`` makes the message pump
    await it instead.
    """
    return partial(app.call_later, action)  # type: ignore[return-value]


def _screen_actions(screen: Screen[object]) -> list[Action]:
    """Ask the screen for its commands, tolerating screens that define none."""
    getter = getattr(screen, "command_actions", None)
    if getter is None:
        return []
    return [
        Action(name=item.name, category=item.category, help=item.help, callback=item.callback)
        for item in getter()
    ]


def _app_actions(app: SwissKnifeApp) -> list[Action]:
    """App-wide actions: theme switching, help, disconnect, quit."""
    actions = [
        Action(
            name=f"Switch theme: {name}",
            category="theme",
            help="change the colour theme (persisted)",
            callback=partial(app.set_theme, name),
        )
        for name in theme_names()
    ]
    actions.extend(
        [
            Action(
                name="Help: list key bindings",
                category="help",
                help="show the help overlay",
                callback=app.action_show_help,
            ),
            Action(
                name="Disconnect",
                category="connection",
                help="close the active connection",
                callback=schedule(app, app.action_disconnect),
            ),
            Action(
                name="Go to connections",
                category="navigate",
                help="back to the connection list",
                callback=app.go_home,
            ),
            Action(
                name="Quit",
                category="app",
                help="close the connection and quit",
                callback=schedule(app, app.action_quit),
            ),
        ]
    )
    return actions


def _system_actions(app: SwissKnifeApp, screen: Screen[object]) -> list[Action]:
    """The app's built-in system commands, re-yielded as palette actions.

    ``App.get_system_commands`` is the single source of truth for these (theme cycling,
    quit, the keys panel, maximise, screenshot), so this adapter must not restate them:
    it only translates them into :class:`Action` so our single provider can serve them.
    Keeping the upstream ``discover`` flag preserves the empty-query behaviour.
    """
    return [
        Action(
            name=command.title,
            category="system",
            help=command.help,
            callback=cast("Callable[[], None]", command.callback),
            discover=command.discover,
        )
        for command in app.get_system_commands(screen)
    ]
