"""Config file locations via platformdirs, with a test override (DESIGN §13)."""

import os
from pathlib import Path

from platformdirs import user_config_path

__all__ = [
    "APP_NAME",
    "CONFIG_DIR_ENV",
    "KEYBINDINGS_ENV",
    "KEYBINDINGS_FILENAME",
    "atomic_write",
    "audit_log_path",
    "config_dir",
    "keybindings_path",
    "profiles_path",
    "settings_path",
]

#: Application name used for platformdirs locations and the OS keyring service.
APP_NAME = "sql-table-swiss-knife"

#: Environment variable overriding the config directory (used by tests and portable setups).
CONFIG_DIR_ENV = "SWISSKNIFE_CONFIG_DIR"

#: Environment variable overriding just the keybindings file (M8, NFR-6).
KEYBINDINGS_ENV = "SWISSKNIFE_KEYBINDINGS"

#: The keybindings file name inside the config directory.
KEYBINDINGS_FILENAME = "keybindings.toml"

#: Distinguishes this process's temporary files from another instance's at the same pid.
_WRITE_COUNTER = 0


def config_dir() -> Path:
    """Directory holding user configuration (``profiles.toml``)."""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path(user_config_path(APP_NAME, appauthor=False))


def profiles_path() -> Path:
    """Path of the connection-profile file (never contains secrets)."""
    return config_dir() / "profiles.toml"


def settings_path() -> Path:
    """Path of the user settings file (``settings.toml``; DESIGN §13.2)."""
    return config_dir() / "settings.toml"


def audit_log_path() -> Path:
    """Path of the Apply audit log (``audit.log.jsonl``), one JSON object per line.

    Kept next to the config rather than in a system-wide location: the log records
    *which profile* wrote to which server, which is user data, not machine state.
    """
    return config_dir() / "audit.log.jsonl"


def keybindings_path() -> Path:
    """Path of the user keybinding overrides (``keybindings.toml``; M8, NFR-6).

    The env override points at a *file* rather than a directory, because the useful case
    is a checked-in keymap that a team shares, not a second config directory.
    """
    override = os.environ.get(KEYBINDINGS_ENV)
    if override:
        return Path(override).expanduser()
    return config_dir() / KEYBINDINGS_FILENAME


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a reader never sees a half-written file.

    The write goes to a temporary file in the same directory (so ``os.replace`` stays
    atomic — a cross-device move is not) and is then renamed over the target.

    The temporary name carries this process's id and a counter rather than a fixed
    ``.tmp`` suffix: two instances of the app writing the same file used to share one
    temporary path, so one could rename the other's file away mid-write and the loser
    failed with ``FileNotFoundError`` — and, before that, could read back the other
    process's half-written content. A unique name per write removes the collision; the
    leftover is cleaned up if the rename cannot happen.
    """
    global _WRITE_COUNTER
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{_WRITE_COUNTER}.tmp")
    _WRITE_COUNTER += 1
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)  # owner-only: no secrets, but keep config files tight
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
