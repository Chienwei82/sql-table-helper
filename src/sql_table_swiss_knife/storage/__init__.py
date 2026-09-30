"""Persistent storage: connection profiles (TOML, no secrets) and secret stores (DESIGN §8)."""

from .paths import config_dir, profiles_path
from .profiles import ProfileError, ProfileStore
from .secrets import (
    EphemeralSecretStore,
    KeyringSecretStore,
    SecretStore,
    SecretStoreError,
    default_secret_store,
    resolve_password,
    secret_ref_for,
)

__all__ = [
    "EphemeralSecretStore",
    "KeyringSecretStore",
    "ProfileError",
    "ProfileStore",
    "SecretStore",
    "SecretStoreError",
    "config_dir",
    "default_secret_store",
    "profiles_path",
    "resolve_password",
    "secret_ref_for",
]
