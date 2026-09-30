"""Profile editor modal: create or edit a connection profile (FR-1.1, FR-1.2).

A plain form rather than a wizard: every field of a profile is visible at once, which
matches how people copy an existing connection. The password field is write-only and
goes straight to the OS keyring — it is never written to ``profiles.toml`` and never
echoed back into the form (S-sec-1, FR-1.6).
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, MaskedInput, Select, Static

from ...domain.connection import AuthMode, ConnectionOptions, ConnectionProfile
from ...services import ConnectionService
from .base import app_services

__all__ = ["ProfileEditScreen"]

#: Choices offered by the authentication select (FR-1.2).
AUTH_CHOICES: list[tuple[str, str]] = [(mode.value, mode.value) for mode in AuthMode]


class ProfileEditScreen(ModalScreen[ConnectionProfile | None]):
    """Create (``profile=None``) or edit a connection profile."""

    CSS = """
    ProfileEditScreen {
        align: center middle;
    }
    #profile-box {
        width: 78;
        height: auto;
        max-height: 90%;
        padding: 0 1;
        background: $surface;
        border: round $primary;
        overflow-y: auto;
    }
    #profile-title {
        height: 1;
        margin-bottom: 1;
    }
    #profile-grid {
        grid-size: 3;
        grid-columns: 18 1fr;
        grid-gutter: 0 1;
        height: auto;
    }
    #profile-grid Label {
        height: 3;
        content-align: left middle;
    }
    #profile-grid Checkbox {
        height: 3;
        width: 1fr;
        content-align: left middle;
        border: none;
        padding: 0;
        background: transparent;
    }
    #profile-hint {
        height: auto;
        margin: 1 0;
        color: $text-muted;
    }
    #profile-actions {
        height: 3;
        align-horizontal: right;
    }
    #profile-actions Button {
        margin-left: 2;
        min-width: 12;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+s", "save", "Save", show=True),
    ]

    def __init__(self, profile: ConnectionProfile | None = None) -> None:
        super().__init__()
        self._profile = profile
        self._error: str | None = None

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        profile = self._profile
        title = f"Edit profile — {profile.name}" if profile else "New connection profile"
        with Vertical(id="profile-box"):
            yield Static(title, id="profile-title")
            yield from self._fields(profile)
            yield Static(id="profile-hint")
            with Horizontal(id="profile-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save (ctrl+s)", id="save", variant="primary")

    def _fields(self, profile: ConnectionProfile | None) -> ComposeResult:
        """Yield the label/widget pairs of the form, pre-filled when editing."""
        options = profile.options if profile else ConnectionOptions()
        with Grid(id="profile-grid"):
            yield Label("Name")
            yield Input(
                value=profile.name if profile else "", placeholder="catalog-prod", id="f-name"
            )
            yield Label("Provider")
            yield Input(
                value=profile.provider if profile else "mssql", placeholder="mssql", id="f-provider"
            )
            yield Label("Host")
            yield Input(value=profile.host if profile else "", placeholder="sql01", id="f-host")
            yield Label("Port")
            yield Input(value=str(profile.port if profile else 1433), id="f-port")
            yield Label("Default database")
            yield Input(
                value=(profile.database or "") if profile else "",
                placeholder="(choose on connect)",
                id="f-database",
            )
            yield Label("Authentication")
            yield Select(
                AUTH_CHOICES,
                value=profile.auth.value if profile else AuthMode.SQL.value,
                allow_blank=False,
                id="f-auth",
            )
            yield Label("Username")
            yield Input(
                value=(profile.username or "") if profile else "",
                placeholder="sa",
                id="f-username",
            )
            yield Label("Password")
            yield MaskedInput(
                # A trailing literal anchors the mask; the dots hide the length.
                template="\u2022" * 20 + "x",
                value="",
                placeholder="(empty keeps the stored password)",
                id="f-password",
            )
            yield Label("ODBC driver")
            yield Input(value=options.driver, id="f-driver")
            yield Label("Encrypt")
            yield Checkbox("Encrypt connection", value=options.encrypt, id="f-encrypt")
            yield Label("Trust certificate")
            yield Checkbox(
                "Trust the server certificate (self-signed)",
                value=options.trust_server_certificate,
                id="f-trust",
            )
            yield Label("Connect timeout (s)")
            yield Input(value=str(options.connect_timeout_s), id="f-timeout")

    def on_mount(self) -> None:
        self.query_one("#f-name", Input).focus()
        self._update_hint()

    # -- helpers ------------------------------------------------------------

    @property
    def connection_service(self) -> ConnectionService:
        """The app-wide connection service (owns the secret store)."""
        return app_services(self).connection

    def _value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _update_hint(self, error: str | None = None) -> None:
        """Show either the save-password hint or a validation error, never both."""
        hint = self.query_one("#profile-hint", Static)
        if error:
            hint.update(f"✖ {error}")
            hint.display = True
            return
        if self.connection_service.secret_store.persistent:
            hint.update(
                "A password entered here is stored in the OS keyring. "
                "It is never written to profiles.toml."
            )
        else:
            hint.update(
                "No OS keyring is available: a password entered here stays in memory "
                "for this session and must be re-entered next time."
            )
        hint.display = True

    # -- actions ------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        """Validate the form and dismiss with the resulting profile."""
        password = self.query_one("#f-password", MaskedInput).value
        try:
            profile = self._build_profile()
        except ValueError as exc:
            self._update_hint(str(exc))
            return
        if password:
            try:
                self.connection_service.store_password(profile, password)
            except Exception as exc:
                # The keyring refused: say so rather than silently dropping it.
                self._update_hint(f"cannot store the password: {exc}")
                return
        self.dismiss(profile)

    def _build_profile(self) -> ConnectionProfile:
        """Build a validated :class:`ConnectionProfile` from the form fields.

        Raises:
            ValueError: with a user-facing message when a field is unusable.
        """
        name = self._value("#f-name")
        if not name:
            raise ValueError("name is required")
        host = self._value("#f-host")
        if not host:
            raise ValueError("host is required")
        auth = AuthMode(str(self.query_one("#f-auth", Select).value))
        options = ConnectionOptions(
            encrypt=self.query_one("#f-encrypt", Checkbox).value,
            trust_server_certificate=self.query_one("#f-trust", Checkbox).value,
            connect_timeout_s=_as_int(self._value("#f-timeout"), "connect timeout"),
            driver=self._value("#f-driver") or ConnectionOptions().driver,
            application_intent=self._profile.options.application_intent if self._profile else None,
        )
        try:
            return ConnectionProfile(
                name=name,
                provider=self._value("#f-provider") or "mssql",
                host=host,
                port=_as_int(self._value("#f-port"), "port"),
                database=self._value("#f-database") or None,
                auth=auth,
                username=self._value("#f-username") or None,
                secret_ref=f"{name}@{host}",
                options=options,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_cancel()


def _as_int(raw: str, label: str) -> int:
    """Parse an integer form field, naming the field in the error message."""
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number, got {raw!r}") from exc
