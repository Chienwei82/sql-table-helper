"""Base screen: header, status line, key hints, and the async-work contract.

Every screen composes the same three chrome widgets and inherits the same rules for
talking to the database:

* **Workers, never the event loop (FR-10).** :meth:`AppScreen.run_task` schedules a
  coroutine as a Textual worker, shows the status-line spinner immediately, and turns
  any exception into an error toast. The UI keeps repainting while a query runs.
* **Four explicit states (DESIGN §9.3).** ``loading`` / ``ready`` / ``error`` /
  ``disconnected`` — the status line and header classes are updated together so the
  two can never disagree.
* **Generation guard.** Each screen bumps :attr:`AppScreen.generation` when its
  subject changes; a worker result whose generation is stale is dropped instead of
  flashing the wrong table (FR-3.9).
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Footer

from ...services.connection import ConnectionService
from ..commands import ScreenCommand
from ..widgets import AppHeader, KeyHint, KeyHints, StatusLine

if TYPE_CHECKING:  # pragma: no cover - import cycle broken at runtime
    from ..app import AppServices, SwissKnifeApp

__all__ = ["AppScreen", "WorkResult", "app_services", "owning_app"]

T = TypeVar("T")


def app_services(node: Widget) -> AppServices:
    """The service bundle of the app owning ``node``.

    Modal screens do not inherit :class:`AppScreen`, so they call this instead of
    duplicating the lookup. The cast documents the one dynamic step: the services
    hang off our own :class:`~tui.app.SwissKnifeApp`.
    """
    return cast("SwissKnifeApp", node.app).services


def owning_app(node: Widget) -> SwissKnifeApp:
    """The :class:`~tui.app.SwissKnifeApp` owning ``node``.

    Screens need the app's own extras (settings, the clipboard chain), which ``Screen.app``
    is typed as the base ``App``; this one cast documents the expectation instead of
    scattering ``# type: ignore`` comments.
    """
    return cast("SwissKnifeApp", node.app)


#: Signature of the callback a worker hands its result to.
WorkResult = Callable[[T], None]


class AppScreen(Screen[None]):
    """Screen with the standard header/status/hints chrome and worker plumbing."""

    #: Worker group name; workers with the same group are exclusive per screen.
    WORKER_GROUP: ClassVar[str] = "db"

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._generation = 0

    # -- stale-result guard --------------------------------------------------

    @property
    def generation(self) -> int:
        """Bumped whenever the subject of in-flight workers changes."""
        return self._generation

    def bump_generation(self) -> int:
        """Invalidate in-flight worker results and return the new generation."""
        self._generation += 1
        return self._generation

    def compose_chrome(self, title: str, *body: Widget, footer: bool = True) -> ComposeResult:
        """Compose the standard chrome around a screen body.

        Args:
            title: Screen title shown in the app header.
            body: The screen-specific widgets.
            footer: Whether to add Textual's own key-hint footer below ours.
        """
        yield AppHeader()
        with Vertical(id="screen-body"):
            yield from body
        yield StatusLine()
        yield KeyHints()
        if footer:
            yield Footer()

    # -- chrome --------------------------------------------------------------

    @property
    def status(self) -> StatusLine:
        """The screen's status line (spinner + last message)."""
        return self.query_one(StatusLine)

    @property
    def hints(self) -> KeyHints:
        """The screen's context-sensitive key hints."""
        return self.query_one(KeyHints)

    @property
    def header(self) -> AppHeader:
        """The screen's app header."""
        return self.query_one(AppHeader)

    @property
    def services(self) -> AppServices:
        """The session services, read from the owning app."""
        return app_services(self)

    @property
    def connection(self) -> ConnectionService:
        """The app-wide connection service."""
        return self.services.connection

    def set_hints(self, hints: tuple[KeyHint, ...]) -> None:
        """Publish the hints valid for the screen's current state."""
        self.hints.set_hints(hints)

    def refresh_hints(self) -> None:
        """Recompute the key hints for the screen's current state."""
        self.set_hints(self.hints_for())

    def hints_for(self) -> tuple[KeyHint, ...]:
        """Hints for the current state; overridden by the concrete screens."""
        return ()

    def command_actions(self) -> list[ScreenCommand]:
        """Palette entries for this screen, derived from its key bindings.

        Generating them from ``BINDINGS`` means the palette, the footer and the
        keyboard can never disagree about what a screen offers: adding a binding is
        enough to make the action discoverable.
        """
        actions: list[ScreenCommand] = []
        seen: set[str] = set()
        for key, binding in self._bindings:
            name = binding.description or binding.action.replace("_", " ")
            if not binding.show or binding.action in seen:
                continue
            seen.add(binding.action)
            actions.append(
                ScreenCommand(
                    key=key,
                    name=name,
                    category=type(self).__name__.removesuffix("Screen").lower(),
                    help=binding.tooltip or f"{name} ({key})",
                    callback=self._binding_callback(binding.action),
                )
            )
        return actions

    def _binding_callback(self, action: str) -> Callable[[], None]:
        """Return a synchronous callback that runs ``action`` on this screen.

        ``Screen.run_action`` is a coroutine, but the palette calls plain callbacks, so
        it is scheduled on the message pump (``call_next`` awaits coroutines).
        """

        def run() -> None:
            self.call_next(self.run_action, action)

        return run

    # -- navigation ---------------------------------------------------------

    def push(
        self,
        screen: Screen[Any],
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """Push a screen on top of this one and optionally take its result.

        Wraps ``App.push_screen`` so screens never reach for the global app object
        to navigate, and so every push goes through one place.
        """
        if callback is None:
            self.app.push_screen(screen)
        else:
            self.app.push_screen(screen, callback)

    def pop(self) -> None:
        """Return to the previous screen."""
        self.app.pop_screen()

    # -- error reporting ----------------------------------------------------

    def report_error(self, message: str, *, title: str = "Error") -> None:
        """Show an error in the status line *and* as a dismissible toast (DESIGN §12)."""
        self.status.show_message(message, level="error")
        self.notify(message, title=title, severity="error", timeout=8)

    def report_warning(self, message: str, *, title: str = "Warning") -> None:
        """Show a warning in the status line and as a toast."""
        self.status.show_message(message, level="warning")
        self.notify(message, title=title, severity="warning", timeout=6)

    def report_info(self, message: str, *, title: str = "") -> None:
        """Show a neutral message in the status line and as a toast."""
        self.status.show_message(message)
        self.notify(message, title=title, severity="information", timeout=4)

    # -- async work ---------------------------------------------------------

    def run_task(
        self,
        work: Callable[[], Awaitable[T]],
        *,
        busy_message: str,
        on_result: Callable[[T], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        group: str | None = None,
    ) -> None:
        """Run a coroutine in a Textual worker with loading + error handling.

        The spinner appears before the first ``await`` inside the worker, so the user
        sees progress immediately (NFR-2: within 100 ms). Failures go to
        :meth:`report_error` unless the caller handles them itself.
        """
        generation = self._generation

        async def runner() -> None:
            self.status.show_busy(busy_message)
            try:
                result = await work()
            except Exception as exc:  # user-facing error, never a crash (DESIGN §12)
                if generation != self._generation:
                    return  # stale: a newer operation owns the UI now
                if on_error is not None:
                    on_error(exc)
                else:
                    self.report_error(str(exc))
            else:
                if generation == self._generation and on_result is not None:
                    on_result(result)

        self.run_worker(runner(), name=busy_message, group=group or self.WORKER_GROUP)

    def on_unmount(self) -> None:
        """Drop every worker this screen started so nothing writes to a dead UI."""
        for worker in list(self.workers):
            worker.cancel()

    def set_header(self, title: str) -> None:
        """Set the header title, keeping the connection summary in sync."""
        self.header.show_connection(title, self.connection.state, self._session_detail())
        self._sync_badge()

    def _sync_badge(self) -> None:
        """Show the environment pill for the live session, hidden when disconnected.

        The badge is driven by the *session*, not by the screen: "PROD" has to be true
        everywhere at once, and a screen that forgot to show it would be the one place
        the user assumed they were safe.
        """
        session = self.connection.session
        self.header.show_environment(
            session.environment if session is not None else None,
            read_only=self.services.safety.read_only,
        )

    def refresh_header(self) -> None:
        """Re-render the header and hints from the current service state."""
        self.set_header(self._screen_title())

    def _session_detail(self) -> str:
        session = self.connection.session
        return session.label if session else ""

    def _screen_title(self) -> str:
        """Header title for this screen; overridden by the concrete screens."""
        return type(self).__name__
