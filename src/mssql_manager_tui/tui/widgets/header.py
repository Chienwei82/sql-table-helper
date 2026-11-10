"""Application header: connection / database name on every screen (DESIGN §9.1).

The header is the persistent answer to "which server am I looking at?" — it shows
the screen title on the left and the live connection on the right, and it colours the
connection state with the theme's semantic error/warning roles so a lost connection is
obvious without reading the status line.
"""

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import var
from textual.widgets import Static

from ...domain import Environment
from ...services import ConnectionState

__all__ = ["ENV_BADGE_CLASS", "AppHeader", "EnvBadge", "badge_text"]

#: Glyph + label for each connection state (never colour alone — FR-3.7 spirit).
STATE_LABEL: dict[ConnectionState, str] = {
    ConnectionState.DISCONNECTED: "○ offline",
    ConnectionState.CONNECTING: "◐ connecting…",
    ConnectionState.CONNECTED: "● connected",
    ConnectionState.ERROR: "✖ connection error",
}

#: CSS class per environment. The classes name the *severity*, not the colour, so a
#: theme can repaint them without the badge knowing anything about hues (FR-3.7).
ENV_BADGE_CLASS: dict[Environment, str] = {
    Environment.PRODUCTION: "-env-prod",
    Environment.STAGING: "-env-stg",
    Environment.TEST: "-env-test",
    Environment.DEVELOPMENT: "-env-dev",
}

#: The word shown next to the environment glyph. Spelled out rather than abbreviated
#: alone, so the badge survives a narrow window and a colour-vision difference.
ENV_WORD: dict[Environment, str] = {
    Environment.PRODUCTION: "PROD",
    Environment.STAGING: "STAGING",
    Environment.TEST: "TEST",
    Environment.DEVELOPMENT: "DEV",
}


def badge_text(environment: Environment, *, read_only: bool = False) -> str:
    """The header badge label: ``⬤ PROD`` plus a lock when the session is read-only.

    The word is the point; the glyph only reinforces it. Read-only adds its own word
    (``RO``) rather than a lock alone, because "PROD" and "PROD + read-only" are very
    different states and the difference must not depend on colour.
    """
    text = f"⬤ {ENV_WORD[environment]}"
    return f"{text} · RO" if read_only else text


class EnvBadge(Static):
    """The environment pill: red PROD / green DEV, plus the read-only marker.

    A standalone widget rather than a string inside the title so the badge has its own
    width, its own tooltip and its own colours, and so it can be asserted on directly
    in a Pilot test. When no session is connected it renders empty rather than a
    misleading ``DEV``: "which environment am I in?" has no answer before connecting,
    and guessing one is exactly the failure this widget exists to prevent.
    """

    DEFAULT_CSS = """
    EnvBadge {
        width: auto;
        height: 1;
        padding: 0 1;
        text-style: bold;
        color: $text;
        background: $panel;
    }
    EnvBadge.-env-prod {
        background: $error;
        color: $text;
    }
    EnvBadge.-env-stg {
        background: $warning;
        color: $text;
    }
    EnvBadge.-env-test {
        background: $success 40%;
        color: $text;
    }
    EnvBadge.-env-dev {
        background: $success;
        color: $text;
    }
    """

    environment: var[Environment | None] = var(None, init=False)
    read_only: var[bool] = var(False, init=False)

    def watch_environment(self) -> None:
        self._refresh_view()

    def watch_read_only(self) -> None:
        self._refresh_view()

    def show_environment(self, environment: Environment | None, *, read_only: bool) -> None:
        """Show ``environment`` (or nothing when ``None``) and the read-only marker."""
        self.environment = environment
        self.read_only = read_only

    def _refresh_view(self) -> None:
        if not self.is_mounted:
            return  # on_mount renders once the widget is attached
        environment = self.environment
        if environment is None:
            self.update("")
            self.remove_class(*ENV_BADGE_CLASS.values())
            return
        for env, css_class in ENV_BADGE_CLASS.items():
            self.set_class(env is environment, css_class)
        self.update(Text(badge_text(environment, read_only=self.read_only)))


class AppHeader(Horizontal):
    """Title bar carrying the active connection identity."""

    DEFAULT_CSS = """
    AppHeader {
        dock: top;
        height: 1;
        background: $panel;
        color: $text;
    }
    AppHeader > #header-title {
        width: 1fr;
        content-align: left middle;
        text-style: bold;
        padding: 0 1;
    }
    AppHeader > #header-session {
        width: auto;
        max-width: 50%;
        content-align: right middle;
        padding: 0 1;
        color: $muted;
    }
    AppHeader.-connected > #header-session {
        color: $success;
    }
    AppHeader.-connecting > #header-session {
        color: $warning;
    }
    AppHeader.-error > #header-session {
        color: $error;
    }
    """

    title: var[str] = var("", init=False)
    session_label: var[str] = var("○ offline", init=False)
    state: var[ConnectionState] = var(ConnectionState.DISCONNECTED, init=False)
    #: Environment of the live session; ``None`` while disconnected.
    environment: var[Environment | None] = var(None, init=False)
    #: Whether the live session currently refuses writes.
    read_only: var[bool] = var(False, init=False)

    def compose(self) -> ComposeResult:
        yield Static(id="header-title")
        yield EnvBadge(id="header-env")
        yield Static(id="header-session")

    def on_mount(self) -> None:
        self._refresh_view()

    def watch_title(self) -> None:
        self._refresh_view()

    def watch_session_label(self) -> None:
        self._refresh_view()

    def watch_state(self) -> None:
        self._refresh_view()

    def watch_environment(self) -> None:
        self._refresh_view()

    def watch_read_only(self) -> None:
        self._refresh_view()

    def show_connection(self, title: str, state: ConnectionState, detail: str = "") -> None:
        """Set the title and the right-hand connection summary in one call."""
        session = STATE_LABEL[state]
        self.title = title
        self.state = state
        self.session_label = f"{session} · {detail}" if detail else session

    def show_environment(self, environment: Environment | None, *, read_only: bool = False) -> None:
        """Set the environment pill and the read-only marker in one call.

        Passing ``None`` hides the badge — used when there is no session, because a
        badge showing an environment nobody is connected to would be a lie.
        """
        self.environment = environment
        self.read_only = read_only

    def _refresh_view(self) -> None:
        """Push the reactive state into the child widgets and CSS classes."""
        if not self.is_mounted:
            return  # on_mount renders once the children exist
        self.query_one("#header-title", Static).update(self.title)
        self.query_one("#header-session", Static).update(self.session_label)
        self.query_one("#header-env", EnvBadge).show_environment(
            self.environment, read_only=self.read_only
        )
        self.set_class(self.state is ConnectionState.CONNECTED, "-connected")
        self.set_class(self.state is ConnectionState.CONNECTING, "-connecting")
        self.set_class(self.state is ConnectionState.ERROR, "-error")
