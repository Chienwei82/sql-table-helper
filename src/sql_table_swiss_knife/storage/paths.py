"""Config file locations via platformdirs, with a test override (DESIGN §13)."""

import os
from pathlib import Path

from platformdirs import user_config_path

__all__ = ["APP_NAME", "CONFIG_DIR_ENV", "config_dir", "profiles_path"]

#: Application name used for platformdirs locations and the OS keyring service.
APP_NAME = "sql-table-swiss-knife"

#: Environment variable overriding the config directory (used by tests and portable setups).
CONFIG_DIR_ENV = "SWISSKNIFE_CONFIG_DIR"


def config_dir() -> Path:
    """Directory holding user configuration (``profiles.toml``)."""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path(user_config_path(APP_NAME, appauthor=False))


def profiles_path() -> Path:
    """Path of the connection-profile file (never contains secrets)."""
    return config_dir() / "profiles.toml"
