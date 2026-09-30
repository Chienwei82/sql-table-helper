"""Persistent storage: connection profiles (TOML, no secrets), user settings, secret stores.

All files live in the platformdirs config directory and honour the
``SWISSKNIFE_CONFIG_DIR`` override (DESIGN §8.3).
"""

from .paths import config_dir, profiles_path, settings_path
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
from .settings import Settings, SettingsError, SettingsStore

__all__ = [
    "EphemeralSecretStore",
    "KeyringSecretStore",
    "ProfileError",
    "ProfileStore",
    "SecretStore",
    "SecretStoreError",
    "Settings",
    "SettingsError",
    "SettingsStore",
    "config_dir",
    "default_secret_store",
    "profiles_path",
    "resolve_password",
    "secret_ref_for",
    "settings_path",
]
