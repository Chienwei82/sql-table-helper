"""Keybinding registry: the one declaration every help surface reads (M8, NFR-6).

Textual already resolves a key to an action; what it cannot do is *tell you the whole
list*. The help screen, the footer and the command palette each want a different cut of
the same facts — the full table, the screen's current hints, and the fuzzy-searchable
action names — and keeping those three in step by hand is exactly how a help screen
starts lying.

So the bindings are declared **once**, here, as data: a stable action id, the keys that
trigger it, a human description and the group it belongs to. Three surfaces render that
data and nothing else: the configurable keymap (``keybindings.toml``), the help screen
(:mod:`sql_table_swiss_knife.tui.screens.help`) and the command palette.

**The default key still lives on the widget**, because Textual resolves a keypress
against a widget's own ``BINDINGS`` and nothing else can. This module is therefore the
*reference* copy, and :func:`verify_against_widgets` is what proves the two have not
drifted — the test suite runs it against every real screen, so a binding changed in
``table_editor.py`` without its documentation fails the build rather than quietly
misleading someone at 2am.
"""

from dataclasses import dataclass
from typing import Final

__all__ = [
    "ACTIONS",
    "GROUP_ORDER",
    "ActionId",
    "BindingDoc",
    "action",
    "bindings_for",
    "describe",
    "find",
    "groups",
    "verify_against_widgets",
]


class ActionId:
    """Stable identifiers for the actions a user can invoke.

    Names, not numbers, so a binding can be remapped without renumbering anything, and
    so the help screen can name an action rather than a key the user may have rebound.
    """

    QUIT = "quit"
    HELP = "help"
    COMMAND_PALETTE = "command_palette"
    THEME = "next_theme"
    GO_HOME = "go_home"
    READ_ONLY = "toggle_read_only"

    BACK = "go_back"
    APPLY = "apply"
    DISCARD_ALL = "discard_all"
    UNDO = "undo"
    REDO = "redo"
    EDIT_CELL = "edit_cell"
    EXPAND_CELL = "expand_cell"
    INSERT_ROW = "insert_row"
    DUPLICATE_ROW = "duplicate_row"
    DELETE_ROW = "delete_row"
    PASTE = "paste"
    COPY = "copy_data"
    INSPECTOR = "toggle_inspector"
    SQL_PANEL = "toggle_sql_panel"
    SQL_MODE = "sql_mode"
    COPY_SQL = "copy_sql"
    REFRESH = "reload"
    FETCH_MORE = "fetch_more"
    COLUMNS = "toggle_columns"
    FILTER = "quick_filter"
    SORT = "sort_column"
    GENERATE_SQL = "generate_sql"

    NEW_PROFILE = "new_profile"
    EDIT_PROFILE = "edit_profile"
    DELETE_PROFILE = "delete_profile"
    TEST_PROFILE = "test_profile"
    CONNECT = "connect"
    PICK_DATABASE = "pick_database"


#: Display groups, in the order the help screen lists them.
GROUP_ORDER: Final[tuple[str, ...]] = (
    "Application",
    "Safety",
    "Navigation",
    "Editing & staging",
    "Clipboard & files",
    "View",
    "Connections",
)


@dataclass(frozen=True, slots=True)
class BindingDoc:
    """One documented action: what it is called, how it is triggered, what it does."""

    action: str
    keys: tuple[str, ...]
    description: str
    group: str

    @property
    def key_label(self) -> str:
        """The keys as the help screen shows them (``ctrl+s`` or ``f1 / ?``)."""
        return " / ".join(self.keys)


def _doc(action: str, keys: str, description: str, group: str) -> BindingDoc:
    return BindingDoc(action=action, keys=tuple(keys.split()), description=description, group=group)


#: Every documented action. This table *is* the help screen's content.
ACTIONS: Final[tuple[BindingDoc, ...]] = (
    # -- application --------------------------------------------------------
    _doc(ActionId.QUIT, "ctrl+q", "Close the connection and quit", "Application"),
    _doc(ActionId.HELP, "f1 ?", "Show this help", "Application"),
    _doc(
        ActionId.COMMAND_PALETTE,
        "ctrl+p",
        "Command palette (fuzzy search over every action)",
        "Application",
    ),
    _doc(ActionId.THEME, "ctrl+t", "Cycle the colour theme", "Application"),
    _doc(ActionId.GO_HOME, "ctrl+h", "Back to the connection list", "Application"),
    # -- safety ------------------------------------------------------------
    _doc(ActionId.READ_ONLY, "f5", "Toggle read-only mode for this session (S-9)", "Safety"),
    _doc(
        ActionId.APPLY,
        "ctrl+s",
        "Commit every staged change in one transaction (asks first)",
        "Safety",
    ),
    _doc(
        ActionId.DISCARD_ALL,
        "ctrl+shift+u",
        "Discard all staged changes (asks first)",
        "Safety",
    ),
    # -- navigation --------------------------------------------------------
    _doc(ActionId.BACK, "escape", "Back one level", "Navigation"),
    _doc(ActionId.REFRESH, "r", "Reload the rows (keeps staged changes)", "Navigation"),
    _doc(ActionId.FETCH_MORE, "m", "Fetch the next page of rows", "Navigation"),
    # -- editing -----------------------------------------------------------
    _doc(ActionId.EDIT_CELL, "enter f4", "Edit the focused cell", "Editing & staging"),
    _doc(
        ActionId.EXPAND_CELL,
        "w",
        "Expand the focused cell (wrapped text, hex dump for binary)",
        "Editing & staging",
    ),
    _doc(ActionId.INSERT_ROW, "n", "Stage a new row", "Editing & staging"),
    _doc(ActionId.DUPLICATE_ROW, "ctrl+d", "Stage a copy of the focused row", "Editing & staging"),
    _doc(ActionId.DELETE_ROW, "delete", "Mark the focused row for deletion", "Editing & staging"),
    _doc(ActionId.UNDO, "ctrl+z", "Undo the last staging action", "Editing & staging"),
    _doc(ActionId.REDO, "ctrl+shift+z", "Redo the last undone action", "Editing & staging"),
    # -- clipboard ---------------------------------------------------------
    _doc(ActionId.COPY, "ctrl+c", "Copy the cell / row / column / selection", "Clipboard & files"),
    _doc(
        ActionId.PASTE,
        "ctrl+v",
        "Paste a block (previewed before anything is staged)",
        "Clipboard & files",
    ),
    # -- view --------------------------------------------------------------
    _doc(ActionId.INSPECTOR, "f2", "Show/hide the schema inspector", "View"),
    _doc(ActionId.SQL_PANEL, "f3", "Show/hide the SQL panel", "View"),
    _doc(ActionId.SQL_MODE, "v", "Cycle the SQL rendering", "View"),
    _doc(
        ActionId.COPY_SQL,
        "y",
        "Copy the SQL (whole script, or the selected statement)",
        "View",
    ),
    _doc(ActionId.COLUMNS, "c", "Choose the visible columns", "View"),
    _doc(ActionId.FILTER, "f", "Quick-filter the rows", "View"),
    _doc(ActionId.SORT, "s", "Sort by the focused column", "View"),
    _doc(
        ActionId.GENERATE_SQL,
        "g",
        "Generate SQL for the focused row or the current filter",
        "View",
    ),
    # -- connections -------------------------------------------------------
    _doc(ActionId.NEW_PROFILE, "n", "New connection profile", "Connections"),
    _doc(ActionId.EDIT_PROFILE, "e", "Edit the selected profile", "Connections"),
    _doc(ActionId.DELETE_PROFILE, "x", "Delete the selected profile", "Connections"),
    _doc(ActionId.TEST_PROFILE, "t", "Test the selected profile", "Connections"),
    _doc(ActionId.CONNECT, "enter", "Connect with the selected profile", "Connections"),
    _doc(ActionId.PICK_DATABASE, "b", "Switch database on the live session", "Connections"),
)

#: ``action id → its documentation``.
_BY_ACTION: Final[dict[str, BindingDoc]] = {doc.action: doc for doc in ACTIONS}


def find(action_name: str) -> BindingDoc | None:
    """The documentation for ``action_name``, or ``None`` if it is undocumented."""
    return _BY_ACTION.get(action_name)


def bindings_for(group: str) -> tuple[BindingDoc, ...]:
    """Every documented action in ``group``, in declaration order."""
    return tuple(doc for doc in ACTIONS if doc.group == group)


def groups() -> tuple[tuple[str, tuple[BindingDoc, ...]], ...]:
    """``(group name, actions)`` in :data:`GROUP_ORDER`, skipping empty groups.

    A group with nothing in it is omitted rather than shown empty, so the help screen
    stays as short as the action set actually is.
    """
    return tuple((group, bindings_for(group)) for group in GROUP_ORDER if bindings_for(group))


def action(name: str, keymap: dict[str, str] | None = None) -> str:
    """The *first* key bound to ``name``, honouring a user's override.

    Args:
        name: An :class:`ActionId` value.
        keymap: User overrides from ``keybindings.toml`` (``action id → key``).

    Returns:
        The key to show. An unknown action yields ``""`` rather than raising: a
        documentation gap must never stop the help screen from opening.
    """
    if keymap and name in keymap:
        return keymap[name]
    doc = _BY_ACTION.get(name)
    return doc.keys[0] if doc is not None else ""


def describe(action_name: str, keymap: dict[str, str] | None = None) -> str:
    """``"ctrl+s — Apply every staged change…"``, for one action.

    Returns an empty string for an undocumented action, so a caller rendering a list
    filters on truthiness instead of pre-checking the registry.
    """
    doc = _BY_ACTION.get(action_name)
    if doc is None:
        return ""
    return f"{action(action_name, keymap)} — {doc.description}"


def verify_against_widgets(*widgets: object) -> list[str]:
    """Report documented bindings that no widget actually declares.

    This is the drift check. Textual resolves a keypress against a widget's own
    ``BINDINGS``, so a key changed in a screen without changing this table would leave
    the help screen describing a key that does nothing. The test suite calls this with
    every real screen, so the documentation cannot quietly go stale.

    Args:
        widgets: Screen (or app) classes or instances carrying ``BINDINGS``.

    Returns:
        A list of human-readable mismatches; empty means the registry is in sync.
    """
    declared: dict[str, set[str]] = {}
    for widget in widgets:
        bindings = getattr(widget, "BINDINGS", ()) or ()
        for binding in bindings:
            keys = getattr(binding, "key", None)
            name = getattr(binding, "action", None)
            if not keys or not name:
                continue  # a lambda/callable binding has no stable action id
            declared.setdefault(str(name), set()).update(
                {part.strip() for part in str(keys).split(",")}
            )
    problems: list[str] = []
    for doc in ACTIONS:
        actual = declared.get(doc.action)
        if actual is None:
            continue  # implemented as a plain method, not a named binding
        if not actual.intersection(doc.keys):
            problems.append(
                f"{doc.action}: documented as {'/'.join(doc.keys)}, "
                f"but the widget binds {'/'.join(sorted(actual))}"
            )
    return problems
