"""User keybinding overrides, read from ``keybindings.toml`` (M8, NFR-6).

The defaults in :mod:`sql_table_swiss_knife.tui.keybindings` are deliberately chosen to
work everywhere. An override file exists for the people they do not work for — a
muscle-memory `ctrl+w` for expand, a different Apply key, a numeric keypad without a
function row — and it is a *reference* copy: Textual still resolves the real keypress
from each widget's ``BINDINGS``, so an override changes what the help screen and the
footer show rather than pretending to rebind the app. That is the honest trade for a
file a user can edit without reading the source, and the file says so at the top.

The format is deliberately minimal::

    # sql-table-swiss-knife keybindings
    [keys]
    expand_cell = "w"
    apply = "ctrl+g"

Every entry names an :class:`~sql_table_swiss_knife.tui.keybindings.ActionId`. An
unknown name is **reported, not ignored**: a typo would otherwise silently do nothing,
which is the worst possible outcome for a file whose whole purpose is to change things.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .keybindings import ACTIONS

__all__ = [
    "Keymap",
    "KeymapError",
    "load_keymap",
    "write_template",
]


class KeymapError(Exception):
    """The keybindings file exists but cannot be used."""


#: The action names a file may mention — anything else is a typo worth reporting.
_KNOWN_ACTIONS: Final[frozenset[str]] = frozenset(doc.action for doc in ACTIONS)

#: Written into a fresh file so the format is discoverable without reading this source.
_TEMPLATE: Final[str] = """\
# sql-table-swiss-knife key bindings
#
# Remap any action by its name. Names are the ones the help screen (F1) shows;
# an unknown name is reported rather than ignored, so a typo never silently
# does nothing.
#
# This file changes what the help screen and the footer *show*. The key that
# actually fires is resolved by each screen from its own built-in bindings.

[keys]
# expand_cell = "w"
# apply = "ctrl+g"
"""


@dataclass(frozen=True, slots=True)
class Keymap:
    """Resolved user overrides: ``action id → key``."""

    overrides: dict[str, str]
    #: Non-fatal problems, surfaced as a toast rather than blocking startup.
    warnings: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.overrides)

    def get(self, action_name: str) -> str | None:
        """The user's key for ``action_name``, or ``None`` for the default."""
        return self.overrides.get(action_name)


def write_template(path: Path) -> None:
    """Write a commented starting-point file, if there is none yet.

    A commented example beats an empty file: the format is the part people get wrong,
    and this is cheaper than a documentation lookup.
    """
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TEMPLATE, encoding="utf-8")


def load_keymap(path: Path) -> Keymap:
    """Read the overrides from ``path``.

    An absent file means "no overrides" — the first run is not an error. A malformed
    file or an unknown action name is reported through ``warnings`` rather than raised,
    because refusing to start over a typo in an optional convenience file is the wrong
    trade; the app must still run on the documented defaults.
    """
    if not path.exists():
        return Keymap({})
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        return Keymap({}, (f"{path.name} could not be read ({exc}); using defaults",))

    unknown_keys = set(raw) - {"keys"}
    warnings: list[str] = []
    if unknown_keys:
        warnings.append(f"{path.name}: ignoring unknown section(s) {sorted(unknown_keys)}")

    table = raw.get("keys", {})
    if not isinstance(table, dict):
        return Keymap({}, (*warnings, f"{path.name}: [keys] must be a table"))

    overrides: dict[str, str] = {}
    for name, value in table.items():
        if name not in _KNOWN_ACTIONS:
            warnings.append(f"{path.name}: unknown action {name!r} — ignored")
            continue
        if not isinstance(value, str) or not value.strip():
            warnings.append(f"{path.name}: {name!r} must be a key string — ignored")
            continue
        overrides[name] = value.strip()
    return Keymap(overrides, tuple(warnings))
