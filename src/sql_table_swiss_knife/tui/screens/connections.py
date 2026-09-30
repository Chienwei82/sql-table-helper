"""Connections screen (FR-1): profile list plus add/edit/duplicate/delete/test.

The list is the app's home. Everything needed to reach a table happens from here:
manage profiles, test one without connecting, connect, and — when the profile has no
usable default database — pick one (FR-1.4). Every DB interaction runs through
:meth:`AppScreen.run_task`, so the list stays responsive while a server is probed.
"""

import dataclasses
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static

from ...domain.connection import ConnectionProfile, ConnectionResult
from ...services import SessionInfo
from ..widgets import KeyHint
from .base import AppScreen
from .confirm import ConfirmScreen
from .database_picker import DatabasePickerScreen
from .profile_edit import ProfileEditScreen
from .table_browser import TableBrowserScreen

__all__ = ["CONNECTIONS_TITLE", "ConnectionsScreen"]

CONNECTIONS_TITLE = "Connections"


class ConnectionsScreen(AppScreen):
    """Profile list with per-profile actions."""

    CSS = """
    #profile-actions {
        height: auto;
        padding: 0 1;
    }
    #profile-actions Button {
        border: none;
        background: transparent;
        color: $text-muted;
        min-width: 8;
        height: 1;
        margin-right: 2;
    }
    #profile-actions Button:hover, #profile-actions Button:focus {
        background: $boost;
        color: $text;
    }
    #profile-table {
        height: 1fr;
    }
    #profile-empty {
        height: auto;
        padding: 1 2;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("n", "new_profile", "New", show=True),
        Binding("enter,c", "connect", "Connect", show=True),
        Binding("e", "edit_profile", "Edit", show=True),
        Binding("d", "duplicate_profile", "Duplicate", show=True),
        Binding("x,delete", "delete_profile", "Delete", show=True),
        Binding("t", "test_profile", "Test", show=True),
        Binding("b", "pick_database", "Database", show=True),
        Binding("r", "reload", "Reload", show=False),
    ]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._profiles: tuple[ConnectionProfile, ...] = ()

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield from self.compose_chrome(CONNECTIONS_TITLE)
        with Horizontal(id="profile-actions"):
            yield Button("New (n)", id="new", compact=True)
            yield Button("Edit (e)", id="edit", compact=True)
            yield Button("Duplicate (d)", id="duplicate", compact=True)
            yield Button("Test (t)", id="test", compact=True)
            yield Button("Delete (x)", id="delete", compact=True)
        yield Static(id="profile-empty")
        yield DataTable(id="profile-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        self._load_profiles()
        self.refresh_header()
        self.refresh_hints()

    # -- data ---------------------------------------------------------------

    def _load_profiles(self) -> None:
        """(Re)read the profile store and repopulate the table."""
        try:
            self._profiles = self.connection.list_profiles()
        except Exception as exc:
            self._profiles = ()
            self.report_error(f"cannot read profiles: {exc}", title="Profiles")
        self._render_profiles()

    def _render_profiles(self) -> None:
        table = self.query_one("#profile-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Profile", "Provider", "Server", "Database", "Auth", "State")
        active = self.connection.session
        for index, profile in enumerate(self._profiles):
            state = "connected" if active and active.profile_name == profile.name else ""
            table.add_row(
                profile.name,
                profile.provider,
                f"{profile.host}:{profile.port}",
                profile.database or "(pick on connect)",
                profile.auth.value,
                state,
                key=str(index),
            )
        self._render_empty_state()
        if self._profiles:
            table.focus()
        self.refresh_hints()

    def _render_empty_state(self) -> None:
        empty = self.query_one("#profile-empty", Static)
        if self._profiles:
            empty.display = False
            return
        empty.update(
            "No connection profiles yet. Press [b]n[/b] to create one, "
            "or [b]ctrl+p[/b] for the command palette."
        )
        empty.display = True

    # -- actions ------------------------------------------------------------

    def action_new_profile(self) -> None:
        """Open the profile editor for a new profile."""
        self.open_profile_editor(None)

    def action_edit_profile(self) -> None:
        """Open the profile editor for the selected profile."""
        profile = self._selected_profile()
        if profile is None:
            self.report_warning("no profile selected — press n to create one")
            return
        self.open_profile_editor(profile)

    def action_duplicate_profile(self) -> None:
        """Duplicate the selected profile under a new name (FR-1.1)."""
        from ...services import duplicate_profile

        profile = self._selected_profile()
        if profile is None:
            self.report_warning("no profile selected")
            return
        new_name = f"{profile.name}-copy"
        self.connection.save_profile(duplicate_profile(profile, new_name))
        self._load_profiles()
        self.report_info(f"duplicated {profile.name} as {new_name}")

    def action_delete_profile(self) -> None:
        """Delete the selected profile after confirmation."""
        profile = self._selected_profile()
        if profile is None:
            self.report_warning("no profile selected")
            return
        self.push(ConfirmScreen(f"Delete profile {profile.name!r}?"), self._on_deleted)

    def _on_deleted(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        profile = self._selected_profile()
        if profile is None:
            return
        try:
            self.connection.delete_profile(profile.name)
        except Exception as exc:
            self.report_error(str(exc), title="Delete")
            return
        self._load_profiles()
        self.report_info(f"deleted profile {profile.name}")

    def action_test_profile(self) -> None:
        """Test the selected profile without opening a session (FR-1.3)."""
        profile = self._selected_profile()
        if profile is None:
            self.report_warning("no profile selected")
            return
        self.bump_generation()
        self.run_task(
            lambda: self.connection.test_connection(profile),
            busy_message=f"testing {profile.name}…",
            on_result=self._on_tested,
        )

    def _on_tested(self, result: ConnectionResult) -> None:
        """Report a successful test in the status line and as a toast."""
        self.status.show_message(
            f"{result.server_name} · {result.server_version} · {result.latency_ms} ms",
            level="success",
        )
        self.notify(
            f"{result.server_name}\n{result.server_version} · {result.latency_ms} ms",
            title=f"Connected to {result.database}",
            severity="information",
        )

    def action_connect(self) -> None:
        """Connect the selected profile, offering a database picker when needed."""
        profile = self._selected_profile()
        if profile is None:
            self.report_warning("no profile selected — press n to create one")
            return
        self.connect_to(profile)

    def connect_to(self, profile: ConnectionProfile) -> None:
        """Connect, then open the database picker or the table browser.

        A profile without a default database must let the user choose one (FR-1.4);
        with a default we go straight to the tables and keep the flow short.
        """
        self.bump_generation()
        if profile.database:
            self._connect_and_open(profile)
        else:
            self.push(DatabasePickerScreen(profile), self._on_database_picked)

    def _on_database_picked(self, database: str | None) -> None:
        profile = self._selected_profile()
        if profile is None or database is None:
            self._load_profiles()
            return
        updated = dataclasses.replace(profile, database=database)
        self.connection.save_profile(updated)
        self._load_profiles()
        self._connect_and_open(updated)

    def _connect_and_open(self, profile: ConnectionProfile) -> None:
        self.run_task(
            lambda: self.connection.connect(profile),
            busy_message=f"connecting to {profile.name}…",
            on_result=self._on_connected,
        )

    def _on_connected(self, _session: SessionInfo) -> None:
        self.status.show_message("connected", level="success")
        self.push(TableBrowserScreen())
        self._load_profiles()

    def action_pick_database(self) -> None:
        """Switch database on the live session (FR-1.4)."""
        profile = self._selected_profile()
        if profile is None:
            self.report_warning("no profile selected")
            return
        if not self.connection.is_connected:
            self.report_warning("connect first, then pick a database")
            return
        self.push(DatabasePickerScreen(profile), self._on_database_switched)

    def _on_database_switched(self, database: str | None) -> None:
        """Reconnect to a different database and refresh the list."""
        if database is None:
            return
        profile = self._selected_profile()
        if profile is None:
            return
        updated = dataclasses.replace(profile, database=database)
        self.connection.save_profile(updated)
        self._load_profiles()
        self.bump_generation()
        self.run_task(
            lambda: self.connection.connect(updated),
            busy_message=f"switching to {database}\u2026",
            on_result=lambda _session: self._load_profiles(),
        )

    def action_reload(self) -> None:
        """Re-read the profile file from disk."""
        self._load_profiles()

    # -- hints & chrome ---------------------------------------------------

    def hints_for(self) -> tuple[KeyHint, ...]:
        """Hints depend on whether a profile exists and whether we are connected."""
        if not self._profiles:
            return (
                KeyHint("n", "new profile"),
                KeyHint("ctrl+p", "commands"),
                KeyHint("ctrl+q", "quit"),
            )
        if not self.connection.is_connected:
            return (
                KeyHint("enter", "connect"),
                KeyHint("n", "new"),
                KeyHint("e", "edit"),
                KeyHint("d", "duplicate"),
                KeyHint("x", "delete"),
                KeyHint("t", "test"),
                KeyHint("ctrl+p", "commands"),
            )
        return (
            KeyHint("enter", "reconnect"),
            KeyHint("b", "change database"),
            KeyHint("t", "test"),
            KeyHint("ctrl+p", "commands"),
            KeyHint("ctrl+q", "quit"),
        )

    def _screen_title(self) -> str:
        return CONNECTIONS_TITLE

    # -- messages ----------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on the focused row connects (the table owns the ``enter`` key)."""
        event.stop()
        self.action_connect()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route the action buttons to the same handlers as the key bindings."""
        handlers = {
            "new": self.action_new_profile,
            "edit": self.action_edit_profile,
            "duplicate": self.action_duplicate_profile,
            "test": self.action_test_profile,
            "delete": self.action_delete_profile,
        }
        handler = handlers.get(event.button.id or "")
        if handler is not None:
            handler()

    # -- profile editor ----------------------------------------------------

    def open_profile_editor(self, profile: ConnectionProfile | None) -> None:
        """Push the profile editor for a new (``None``) or existing profile."""
        self.push(ProfileEditScreen(profile), self._on_profile_saved)

    def _on_profile_saved(self, profile: ConnectionProfile | None) -> None:
        """Persist the edited profile and refresh the list."""
        if profile is None:
            return
        try:
            self.connection.save_profile(profile)
        except Exception as exc:
            self.report_error(str(exc), title="Save profile")
            return
        self._load_profiles()
        self.report_info(f"saved profile {profile.name}")

    def _selected_profile(self) -> ConnectionProfile | None:
        """Profile under the cursor, or ``None`` when the list is empty."""
        table = self.query_one("#profile-table", DataTable)
        if table.row_count == 0:
            return None
        index = table.cursor_row
        if not 0 <= index < len(self._profiles):
            return None
        return self._profiles[index]
