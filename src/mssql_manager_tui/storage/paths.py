"""Config file locations via platformdirs, with a test override (DESIGN §13).

The application was renamed ``sql-table-swiss-knife`` → ``sql-table-manager`` →
``mssql-manager-tui``, so :func:`migrate_legacy_config` copies a pre-existing
configuration directory forward exactly once, trying each previous name in turn.
It is a *copy*, never a move, and only fills gaps: a file that already exists under
the new name is left untouched.
"""

import os
import shutil
from pathlib import Path

from platformdirs import user_config_path

__all__ = [
    "APP_NAME",
    "CONFIG_DIR_ENV",
    "FILE_MODE",
    "KEYBINDINGS_ENV",
    "KEYBINDINGS_FILENAME",
    "LEGACY_APP_NAMES",
    "atomic_write",
    "audit_log_path",
    "config_dir",
    "keybindings_path",
    "legacy_config_dirs",
    "migrate_legacy_config",
    "profiles_path",
    "settings_path",
]

#: Application name used for platformdirs locations and the OS keyring service.
APP_NAME = "mssql-manager-tui"

#: Names this application used before it was renamed, most recent first; only the
#: migration reads them. The OS-keyring *service* is also keyed on the name, so secrets
#: stored under an old name are not carried over (the file holds references, not
#: secrets) — see the README.
LEGACY_APP_NAMES: tuple[str, ...] = ("sql-table-manager", "sql-table-swiss-knife")

#: Configuration files carried forward by :func:`migrate_legacy_config`.
_MIGRATED_FILES = ("profiles.toml", "settings.toml", "keybindings.toml", "audit.log.jsonl")

#: Environment variable overriding the config directory (used by tests and portable setups).
CONFIG_DIR_ENV = "MSSQL_MANAGER_CONFIG_DIR"

#: Environment variable overriding just the keybindings file (M8, NFR-6).
KEYBINDINGS_ENV = "MSSQL_MANAGER_KEYBINDINGS"

#: The keybindings file name inside the config directory.
KEYBINDINGS_FILENAME = "keybindings.toml"

#: Distinguishes this process's temporary files from another instance's at the same pid.
_WRITE_COUNTER = 0

#: Owner-only mode for config and audit files.
FILE_MODE = 0o600


def config_dir() -> Path:
    """Directory holding user configuration (``profiles.toml``)."""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path(user_config_path(APP_NAME, appauthor=False))


def legacy_config_dirs() -> tuple[Path, ...]:
    """The pre-rename configuration directories, most recent name first.

    An empty tuple means "do not migrate": the caller pinned the directory with the env
    override, so every name points at the same place and there is nothing to carry over.
    """
    if os.environ.get(CONFIG_DIR_ENV):
        return ()
    return tuple(Path(user_config_path(name, appauthor=False)) for name in LEGACY_APP_NAMES)


def migrate_legacy_config(*, legacy: Path | None = None, target: Path | None = None) -> Path | None:
    """Copy a pre-rename config directory into the current one, once.

    When ``legacy`` is not given, each previous name (see :data:`LEGACY_APP_NAMES`) is
    tried in order and the first directory that actually holds files is migrated from.

    Returns the target directory when a migration happened, or ``None`` when there was
    nothing to do (no legacy directory, or the new one already holds a ``profiles.toml``).

    The copy is deliberately conservative:

    * **never a move** — the legacy directory is left exactly as it was, so a user who
      downgrades keeps working;
    * **never an overwrite** — a file that already exists under the new name wins;
    * **never a crash** — a failure here must not stop the app from starting (the caller
      swallows it), because a config convenience is not worth a refusing-to-run tool.

    OS-keyring secrets are *not* migrated: the keyring service name is derived from
    :data:`APP_NAME`, so stored secrets must be re-entered once. Only the file that holds
    the (non-secret) references is copied.
    """
    source = legacy
    if source is None:
        source = next((d for d in legacy_config_dirs() if d.is_dir()), None)
    if source is None or not source.is_dir():
        return None
    destination = target if target is not None else config_dir()
    if source.resolve() == destination.resolve():
        return None
    if (destination / "profiles.toml").exists():
        return None
    destination.mkdir(parents=True, exist_ok=True)
    copied = False
    for name in _MIGRATED_FILES:
        origin = source / name
        destination_file = destination / name
        if origin.is_file() and not destination_file.exists():
            shutil.copy2(origin, destination_file)
            copied = True
    return destination if copied else None


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
        os.chmod(tmp, FILE_MODE)  # owner-only: no secrets, but keep config files tight
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
