"""Secret storage: OS keyring with an in-memory, prompt-per-session fallback (DESIGN §8.1).

Passwords live **only** here — never in ``profiles.toml``, logs, previews or error
messages. ``storage/profiles.py`` stores just a ``secret_ref`` (keyring account name).
"""

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ..domain.connection import ConnectionProfile
from ..infra.errors import AppError
from .paths import APP_NAME

__all__ = [
    "EphemeralSecretStore",
    "KeyringSecretStore",
    "SecretStore",
    "SecretStoreError",
    "default_secret_store",
    "resolve_password",
    "secret_ref_for",
]


class SecretStoreError(AppError):
    """Keyring unavailable or refusing an operation."""


@runtime_checkable
class SecretStore(Protocol):
    """Get/set/delete a secret by reference; ``persistent`` False means "session only"."""

    def get(self, ref: str) -> str | None: ...

    def set(self, ref: str, secret: str) -> None: ...

    def delete(self, ref: str) -> None: ...

    @property
    def persistent(self) -> bool: ...


class EphemeralSecretStore:
    """In-memory store used when no OS keyring works (headless Linux, CI, first run)."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    @property
    def persistent(self) -> bool:
        return False

    def get(self, ref: str) -> str | None:
        return self._secrets.get(ref)

    def set(self, ref: str, secret: str) -> None:
        if not secret:
            raise ValueError("secret must not be empty")
        self._secrets[ref] = secret

    def delete(self, ref: str) -> None:
        self._secrets.pop(ref, None)

    def __repr__(self) -> str:  # never render secret values
        return f"EphemeralSecretStore(entries={len(self._secrets)})"


class KeyringSecretStore:
    """OS keyring store (service = app name, account = ``secret_ref``)."""

    def __init__(self, service: str = APP_NAME) -> None:
        self._service = service

    @property
    def persistent(self) -> bool:
        return True

    def get(self, ref: str) -> str | None:
        import keyring

        try:
            return keyring.get_password(self._service, ref)
        except Exception as exc:  # backend-specific failures
            raise SecretStoreError(f"keyring read failed: {exc}") from exc

    def set(self, ref: str, secret: str) -> None:
        import keyring

        if not secret:
            raise ValueError("secret must not be empty")
        try:
            keyring.set_password(self._service, ref, secret)
        except Exception as exc:
            raise SecretStoreError(f"keyring write failed: {exc}") from exc

    def delete(self, ref: str) -> None:
        import keyring

        try:
            keyring.delete_password(self._service, ref)
        except keyring.errors.PasswordDeleteError:
            return  # already gone — deleting is idempotent
        except Exception as exc:
            raise SecretStoreError(f"keyring delete failed: {exc}") from exc


def secret_ref_for(profile: ConnectionProfile) -> str:
    """Derive the keyring account reference for a profile (``name@host``)."""
    return f"{profile.name}@{profile.host}"


def _keyring_is_functional() -> bool:
    """True when the active keyring backend can actually store secrets.

    ``keyring.backends.fail.Keyring`` (priority 0) is the sentinel installed when no
    real backend is usable — headless Linux without a Secret Service, CI, containers.
    """
    try:
        import keyring

        backend = keyring.get_keyring()
    except Exception:
        return False
    if backend is None:
        return False
    return getattr(backend, "priority", 0) > 0


def default_secret_store() -> SecretStore:
    """Keyring when a real backend is available, otherwise the ephemeral fallback."""
    return KeyringSecretStore() if _keyring_is_functional() else EphemeralSecretStore()


#: Prompter signature: given a message, return the typed secret (or None to cancel).
PromptFn = Callable[[str], str | None]


def resolve_password(
    store: SecretStore,
    profile: ConnectionProfile,
    prompt: PromptFn | None = None,
) -> str | None:
    """Find the password for a profile: explicit store hit → prompt → ``None``.

    Integrated-auth profiles never reach this function with a secret request, but
    returning ``None`` for them keeps the contract simple.
    """
    ref = profile.secret_ref or secret_ref_for(profile)
    secret = store.get(ref)
    if secret:
        return secret
    if prompt is None:
        return None
    typed = prompt(f"Password for {profile.username or profile.name}@{profile.host}: ")
    if typed:
        return typed
    return None
