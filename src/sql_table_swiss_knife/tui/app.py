"""Textual application shell: services, themes, screen stack, global bindings.

The app owns the two service objects for the whole session (one connection, one
catalog cache) and hands them to the screens; screens never construct services of their
own. Themes are registered here, switched at runtime with ``ctrl+t`` and persisted to
``settings.toml``, so the choice survives a restart (FR-3.7).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from textual.app import App
from textual.binding import Binding, BindingType
from textual.command import Provider

from ..infra.clipboard import ClipboardService, default_backends
from ..services import (
    CatalogService,
    ConnectionService,
    DataService,
    LookupService,
    SafetyPolicy,
)
from ..services.connection import ProviderFactory
from ..storage import (
    AuditLog,
    ProfileStore,
    SecretStore,
    Settings,
    SettingsError,
    SettingsStore,
    keybindings_path,
)
from .commands import ActionProvider
from .keymap import Keymap, load_keymap, write_template
from .screens import ConnectionsScreen, HelpScreen
from .theme import SEMANTIC_ROLES, THEMES, load_theme, next_theme, register_themes

__all__ = ["AppServices", "SwissKnifeApp"]


class AppServices:
    """The service bundle screens read as ``app.services`` (DESIGN §2).

        A tiny container rather than passing four arguments around: screens touch
        ``app.services.connection`` / ``.catalog`` / ``.data`` / ``.lookup`` and nothing else,
        which keeps the dependency direction ``tui → services``.

        ``lookup`` is stateless and cheap, so it is created once here rather than per screen;
        ``clipboard`` is built on demand from the app itself (it needs the terminal), and
    ``changes`` is *not* in this bundle: staging belongs to one table, so each
        ``TableEditorScreen`` owns its own :class:`~services.changes.ChangeService`.

        ``safety`` is the session's write posture (M8). It lives here rather than on a
        screen because read-only is a property of the *session*, not of a table: it
        survives navigating between tables and is toggled from anywhere.
    """

    __slots__ = ("catalog", "connection", "data", "lookup", "safety")

    def __init__(
        self,
        connection: ConnectionService,
        catalog: CatalogService,
        data: DataService,
        lookup: LookupService,
        safety: SafetyPolicy,
    ) -> None:
        self.connection = connection
        self.catalog = catalog
        self.data = data
        self.lookup = lookup
        self.safety = safety


class SwissKnifeApp(App[None]):
    """sql-table-swiss-knife: screen stack, theming and global bindings."""

    TITLE = "sql-table-swiss-knife"

    CSS = """
    #screen-body {
        height: 1fr;
        width: 100%;
    }
    """

    #: The command palette (ctrl+p) is served by our single :class:`ActionProvider`.
    #:
    #: Textual's ``SystemCommandsProvider`` is intentionally absent. The palette runs one
    #: task per provider and merges the hits into one queue, so with two providers the
    #: arrival order — and hence the rendered order of equally-scored hits — depends on
    #: task scheduling, which made the palette snapshot intermittently mismatch.
    #: ``ActionProvider`` yields the system commands itself instead (see
    #: ``commands._system_actions``), so one provider means one deterministic order.
    COMMANDS: ClassVar[set[type[Provider] | Callable[[], type[Provider]]]] = {
        ActionProvider,
    }

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+p", "command_palette", "Commands", priority=True, show=False),
        Binding("ctrl+t", "next_theme", "Theme", priority=True, show=False),
        Binding("f1,question_mark", "show_help", "Help", priority=True, show=False),
        Binding("ctrl+h", "go_home", "Connections", priority=True, show=False),
        # M8 safety: the read-only posture is one keystroke away from anywhere,
        # and always visible in the header badge (S-9).
        Binding("f5", "toggle_read_only", "Read-only", priority=True, show=False),
    ]

    def __init__(
        self,
        *,
        profiles: ProfileStore | None = None,
        secrets: SecretStore | None = None,
        settings: SettingsStore | None = None,
        provider_factory: ProviderFactory | None = None,
        initial_theme: str | None = None,
        read_only: bool = False,
        audit_log: AuditLog | None = None,
        keybindings: Path | None = None,
    ) -> None:
        """Create the app, wiring up services.

        Args:
            profiles: Profile store override (tests / portable setups).
            secrets: Secret store override; defaults to keyring-or-ephemeral.
            settings: Settings store override (tests).
            provider_factory: Provider resolver override (tests use a fake).
            initial_theme: Force a theme instead of reading ``settings.toml``.
            read_only: ``--read-only``: the session refuses writes and the toggle is
                locked, so the posture is the one the process was started in.
            audit_log: Apply audit log override (tests write to a temp file).
            keybindings: ``keybindings.toml`` override (tests / portable setups).
        """
        super().__init__()
        self.settings_store = settings if settings is not None else SettingsStore()
        self._settings: Settings = self._load_settings()
        self._settings_error: str | None = None
        self._read_only_requested = read_only
        # A commented template is written next to the other config files the first time
        # the app runs, so the keybinding format is discoverable without a manual. A
        # user's edits are never overwritten: write_template returns early if the file
        # exists, so this is a no-op on every run after the first.
        keymap_file = keybindings if keybindings is not None else keybindings_path()
        write_template(keymap_file)
        self.keymap: Keymap = load_keymap(keymap_file)
        self.audit_log = audit_log if audit_log is not None else AuditLog()
        # The posture a profile opens with is resolved on connect (it depends on the
        # profile's environment); this is the starting point until then.
        self.safety = SafetyPolicy(
            forced_read_only=read_only,
            read_only=read_only,
            delete_confirm_threshold=self._settings.delete_confirm_threshold,
            allow_keyless_writes=self._settings.allow_keyless_writes,
        )
        self.connection = ConnectionService(
            profiles=profiles,
            secrets=secrets,
            **({"provider_factory": provider_factory} if provider_factory else {}),
        )
        self.catalog = CatalogService(self.connection)
        self.data = DataService(self.connection)
        self._forced_theme = initial_theme
        self._quitting = False
        # Built once here because every one of these is a long-lived collaborator that
        # holds no per-screen state: this property used to rebuild the bundle — and a
        # LookupService — on every access, and screens touch it on each cursor move.
        # `safety` is only ever mutated in place (toggle_read_only), never replaced, so a
        # cached bundle cannot hold a stale policy. Anything that genuinely must be fresh
        # per call is a separate property — `clipboard_service` binds Textual's own
        # clipboard each time, on purpose.
        self._services = AppServices(
            connection=self.connection,
            catalog=self.catalog,
            data=self.data,
            lookup=LookupService(self.connection, self.catalog),
            safety=self.safety,
        )

    # -- state --------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        """The live user preferences (re-read after every persisted change)."""
        return self._settings

    def persist_settings(self, **changes: Any) -> None:
        """Save preference changes and keep the in-memory copy in step.

        Screens change preferences (the copy format, for one), and a screen that only wrote
        the file would leave the app believing the old value until a restart — the sort of
        drift the rest of this app is careful to avoid.
        """
        try:
            self._settings = self.settings_store.update(self._settings, **changes)
        except SettingsError as exc:
            self.notify(f"setting not persisted: {exc}", title="Settings", severity="warning")

    @property
    def clipboard_service(self) -> ClipboardService:
        """The copy/read chain: pyperclip, then platform tools, then OSC 52 (FR-4.3).

        Built per call rather than cached so it always reflects the current settings and
        always binds Textual's own ``copy_to_clipboard`` for the terminal fallback — which
        is the mechanism that keeps working over SSH, where the native tools cannot.
        """
        return ClipboardService(
            default_backends(self.copy_to_clipboard),
            read_fallback=self.settings.clipboard_read_fallback,
        )

    @property
    def services(self) -> AppServices:
        """The session services, as the screens consume them.

        Built once in ``__init__``; these collaborators hold no per-screen state, so
        rebuilding the bundle — and a ``LookupService`` — per access was pure allocation.
        """
        return self._services

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

    def action_toggle_read_only(self) -> None:
        """Flip the session's read-only posture (``f5``, S-9).

        Read-only is the default for production, but it is never a lock: the user can
        always choose to write, and doing so changes the header badge immediately. The
        reverse also holds — a writable session can be locked at any moment, which is
        the point of making this a toggle rather than a per-profile setting only.
        """
        read_only = self.safety.toggle_read_only()
        if self.safety.forced_read_only:
            self.notify(
                "this session was started with --read-only; it cannot write",
                title="Read-only",
                severity="warning",
            )
        elif read_only:
            self.notify("read-only: staging and Apply are disabled", title="Read-only")
        else:
            self.notify(
                "writes enabled — production Apply still asks for a typed confirmation",
                title="Read-only",
                severity="warning",
            )
        self._refresh_chrome()

    def action_show_help(self) -> None:
        """Show the key-binding reference (NFR-6, M8).

        Our own screen rather than Textual's built-in help, because it is rendered
        from the same registry the footer and the palette read and it carries the
        safety summary — the built-in one lists bindings and nothing else.
        """
        self.push_screen(HelpScreen(self.keymap))

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
