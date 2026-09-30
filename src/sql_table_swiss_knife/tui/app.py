"""Textual application shell: services, themes, screen stack, global bindings.

The app owns the two service objects for the whole session (one connection, one
catalog cache) and hands them to the screens; screens never construct services of their
own. Themes are registered here, switched at runtime with ``ctrl+t`` and persisted to
``settings.toml``, so the choice survives a restart (FR-3.7).
"""

from collections.abc import Callable
from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType
from textual.command import Provider
from textual.system_commands import SystemCommandsProvider

from ..services import CatalogService, ConnectionService
from ..services.connection import ProviderFactory
from ..storage import ProfileStore, SecretStore, Settings, SettingsError, SettingsStore
from .commands import ActionProvider
from .screens import ConnectionsScreen
from .theme import SEMANTIC_ROLES, THEMES, load_theme, next_theme, register_themes

__all__ = ["AppServices", "SwissKnifeApp"]


class AppServices:
    """The service bundle screens read as ``app.services`` (DESIGN §2).

    A tiny container rather than passing two arguments around: screens touch
    ``app.services.connection`` / ``.catalog`` and nothing else, which keeps the
    dependency direction ``tui → services``.
    """

    __slots__ = ("catalog", "connection")

    def __init__(self, connection: ConnectionService, catalog: CatalogService) -> None:
        self.connection = connection
        self.catalog = catalog


class SwissKnifeApp(App[None]):
    """sql-table-swiss-knife: screen stack, theming and global bindings."""

    TITLE = "sql-table-swiss-knife"

    CSS = """
    #screen-body {
        height: 1fr;
        width: 100%;
    }
    """

    #: The built-in command palette (ctrl+p) plus our action provider.
    COMMANDS: ClassVar[set[type[Provider] | Callable[[], type[Provider]]]] = {
        SystemCommandsProvider,
        ActionProvider,
    }

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+p", "command_palette", "Commands", priority=True, show=False),
        Binding("ctrl+t", "next_theme", "Theme", priority=True, show=False),
        Binding("f1,question_mark", "show_help", "Help", priority=True, show=False),
        Binding("ctrl+h", "go_home", "Connections", priority=True, show=False),
    ]

    def __init__(
        self,
        *,
        profiles: ProfileStore | None = None,
        secrets: SecretStore | None = None,
        settings: SettingsStore | None = None,
        provider_factory: ProviderFactory | None = None,
        initial_theme: str | None = None,
    ) -> None:
        """Create the app, wiring up services.

        Args:
            profiles: Profile store override (tests / portable setups).
            secrets: Secret store override; defaults to keyring-or-ephemeral.
            settings: Settings store override (tests).
            provider_factory: Provider resolver override (tests use a fake).
            initial_theme: Force a theme instead of reading ``settings.toml``.
        """
        super().__init__()
        self.settings_store = settings if settings is not None else SettingsStore()
        self._settings: Settings = self._load_settings()
        self._settings_error: str | None = None
        self.connection = ConnectionService(
            profiles=profiles,
            secrets=secrets,
            **({"provider_factory": provider_factory} if provider_factory else {}),
        )
        self.catalog = CatalogService(self.connection)
        self._forced_theme = initial_theme
        self._quitting = False

    # -- state --------------------------------------------------------------

    @property
    def services(self) -> AppServices:
        """The session services, as the screens consume them."""
        return AppServices(connection=self.connection, catalog=self.catalog)

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Fallbacks for the semantic palette (DESIGN §9.4).

        CSS is parsed before a theme is active, so every variable a rule mentions must
        resolve. Each theme overrides these; the values here only matter for that first
        parse and for the built-in Textual themes.
        """
        defaults = dict.fromkeys(SEMANTIC_ROLES, "#ffd166")
        defaults["muted"] = "#8b93a3"
        return defaults

    def _load_settings(self) -> Settings:
        """Read settings, falling back to defaults when the file is unusable."""
        try:
            return self.settings_store.load()
        except SettingsError as exc:
            self._settings_error = str(exc)
            return Settings()

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        """Register the themes, activate the persisted one, show the first screen."""
        register_themes(self)
        self.theme = load_theme(self.settings_store, self._forced_theme)
        self.sub_title = self._subtitle()
        self.push_screen(ConnectionsScreen())
        if self._settings_error is not None:
            self.notify(
                f"settings.toml could not be read ({self._settings_error}); defaults are in use",
                title="Settings",
                severity="warning",
            )

    async def on_unmount(self) -> None:
        """Close the database connection before the app goes away (clean quit)."""
        if not self._quitting:
            self._quitting = True
            await self.connection.disconnect()

    async def action_quit(self) -> None:
        """Disconnect, then quit — never leave a driver handle behind."""
        self._quitting = True
        await self.connection.disconnect()
        self.exit()

    async def action_disconnect(self) -> None:
        """Close the connection and drop the catalog cache."""
        await self.connection.disconnect()
        self.catalog.invalidate()
        self._refresh_chrome()

    def action_go_home(self) -> None:
        """Return to the connection list, resetting the screen stack."""
        while len(self.screen_stack) > 1:
            self.pop_screen()

    def go_home(self) -> None:
        """Alias used by the command palette."""
        self.action_go_home()

    def action_show_help(self) -> None:
        """Show the key-binding reference overlay (NFR-6)."""
        self.push_screen("help")

    def action_next_theme(self) -> None:
        """Cycle to the next theme and remember the choice (FR-3.7)."""
        self.set_theme(next_theme(self.theme))

    def set_theme(self, name: str) -> None:
        """Activate ``name`` and persist it, notifying the user on failure.

        A broken settings file must not lose the preference silently: the theme still
        switches, and the reason is shown as a toast.
        """
        if name not in THEMES:
            self.notify(f"unknown theme {name!r}", title="Theme", severity="warning")
            return
        self.theme = name
        try:
            self._settings = self.settings_store.update(self._settings, theme=name)
        except SettingsError as exc:
            self.notify(f"theme not persisted: {exc}", title="Theme", severity="warning")
        self._refresh_chrome()

    # -- internals ----------------------------------------------------------

    def _refresh_chrome(self) -> None:
        """Ask the active screen to redraw its header/hints after a state change."""
        refresh = getattr(self.screen, "refresh_header", None)
        if refresh is not None:
            refresh()
        self.sub_title = self._subtitle()

    def _subtitle(self) -> str:
        """Sub-title: who we are connected as, if anyone."""
        session = self.connection.session
        if session is None:
            return "no connection"
        return f"{session.profile_name} · {session.database}"
