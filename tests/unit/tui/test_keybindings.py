"""Tests for the keybinding registry and the keymap file (M8, NFR-6).

The registry is only worth having if it cannot drift from the widgets that actually
resolve keys, so the drift check is run against the real screens here: change a binding
in a screen without updating the documentation and this file fails.
"""

from pathlib import Path
from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.screen import Screen

from sql_table_swiss_knife.tui.app import SwissKnifeApp
from sql_table_swiss_knife.tui.keybindings import (
    ACTIONS,
    GROUP_ORDER,
    ActionId,
    BindingDoc,
    action,
    bindings_for,
    describe,
    find,
    groups,
    verify_against_widgets,
)
from sql_table_swiss_knife.tui.keymap import load_keymap, write_template
from sql_table_swiss_knife.tui.screens import (
    ConnectionsScreen,
    DatabasePickerScreen,
    TableBrowserScreen,
    TableEditorScreen,
)

#: Every screen that declares bindings the registry documents.
SCREENS = (
    SwissKnifeApp,
    ConnectionsScreen,
    DatabasePickerScreen,
    TableBrowserScreen,
    TableEditorScreen,
)


# -- the registry -----------------------------------------------------------


def test_every_action_is_documented() -> None:
    assert len(ACTIONS) >= 30
    assert all(isinstance(doc, BindingDoc) for doc in ACTIONS)


def test_action_names_are_unique() -> None:
    """Two actions sharing a name would make the registry ambiguous."""
    names = [doc.action for doc in ACTIONS]
    assert len(names) == len(set(names))


def test_every_action_belongs_to_a_declared_group() -> None:
    assert {doc.group for doc in ACTIONS} <= set(GROUP_ORDER)


def test_every_action_has_a_key_and_a_description() -> None:
    assert all(doc.keys for doc in ACTIONS)
    assert all(doc.description for doc in ACTIONS)


def test_find_returns_the_documentation() -> None:
    doc = find(ActionId.APPLY)
    assert doc is not None and doc.action == ActionId.APPLY


def test_the_insert_row_action_is_documented_as_its_new_key() -> None:
    """The CRUD-first editor stages a row with plain ``n``; the registry must agree."""
    doc = find(ActionId.INSERT_ROW)
    assert doc is not None and "n" in doc.keys


def test_find_returns_none_for_an_unknown_action() -> None:
    assert find("no_such_action") is None


def test_the_safety_actions_are_documented() -> None:
    """The rules that protect a production database must be discoverable."""
    for name in (ActionId.READ_ONLY, ActionId.APPLY, ActionId.DISCARD_ALL):
        assert find(name) is not None


def test_groups_are_returned_in_display_order() -> None:
    assert [group for group, _ in groups()] == [
        "Application",
        "Safety",
        "Navigation",
        "Editing & staging",
        "Clipboard & files",
        "View",
        "Connections",
    ]


def test_bindings_for_a_group_preserves_declaration_order() -> None:
    assert [doc.action for doc in bindings_for("Safety")] == [
        ActionId.READ_ONLY,
        ActionId.APPLY,
        ActionId.DISCARD_ALL,
    ]


def test_action_returns_the_first_documented_key() -> None:
    assert action(ActionId.APPLY) == "ctrl+s"


def test_an_override_replaces_the_documented_key() -> None:
    assert action(ActionId.APPLY, {"apply": "ctrl+g"}) == "ctrl+g"


def test_an_unknown_action_yields_no_key_rather_than_raising() -> None:
    """A documentation gap must not stop the help screen from opening."""
    assert action("no_such_action") == ""


def test_describe_joins_the_key_and_the_description() -> None:
    assert describe(ActionId.UNDO).startswith("ctrl+z — ")


def test_describe_of_an_unknown_action_is_empty() -> None:
    assert describe("no_such_action") == ""


def test_the_key_label_reads_as_a_list() -> None:
    doc = find(ActionId.HELP)
    assert doc is not None and doc.key_label == "f1 / ?"


# -- the drift check --------------------------------------------------------


def test_the_registry_matches_every_widget_binding() -> None:
    """Every documented key is really bound by some screen (NFR-6)."""
    assert verify_against_widgets(*SCREENS) == []


def test_the_drift_check_actually_catches_a_mismatch() -> None:
    """A check that cannot fail is not a check."""

    class Drifted(Screen[None]):
        BINDINGS: ClassVar[list[BindingType]] = [Binding("ctrl+alt+z", "apply", "Apply")]

    problems = verify_against_widgets(Drifted)
    assert problems
    assert "apply" in problems[0]


def test_a_documented_action_implemented_without_a_binding_is_not_a_problem() -> None:
    """Some actions are plain methods; only a *wrong* key is a drift."""

    class Quiet(Screen[None]):
        BINDINGS: ClassVar[list[BindingType]] = []

    assert verify_against_widgets(Quiet) == []


# -- the keymap file --------------------------------------------------------


def test_an_absent_file_means_no_overrides(tmp_path: Path) -> None:
    """First run is not an error."""
    keymap = load_keymap(tmp_path / "keybindings.toml")
    assert keymap.overrides == {}
    assert keymap.warnings == ()
    assert not keymap


def test_overrides_are_read(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    path.write_text('[keys]\napply = "ctrl+g"\n', encoding="utf-8")
    keymap = load_keymap(path)
    assert keymap.overrides == {"apply": "ctrl+g"}
    assert keymap.get("apply") == "ctrl+g"
    assert bool(keymap)


def test_whitespace_around_a_key_is_trimmed(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    path.write_text('[keys]\napply = "  ctrl+g  "\n', encoding="utf-8")
    assert load_keymap(path).overrides == {"apply": "ctrl+g"}


def test_an_unknown_action_is_reported_not_ignored(tmp_path: Path) -> None:
    """A typo would otherwise silently do nothing — the worst outcome for this file."""
    path = tmp_path / "keybindings.toml"
    path.write_text('[keys]\naply = "ctrl+g"\n', encoding="utf-8")
    keymap = load_keymap(path)
    assert keymap.overrides == {}
    assert "aply" in keymap.warnings[0]


def test_a_non_string_value_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    path.write_text("[keys]\napply = 7\n", encoding="utf-8")
    keymap = load_keymap(path)
    assert keymap.overrides == {}
    assert "must be a key string" in keymap.warnings[0]


def test_a_malformed_file_falls_back_to_defaults(tmp_path: Path) -> None:
    """Refusing to start over a typo in an optional file is the wrong trade."""
    path = tmp_path / "keybindings.toml"
    path.write_text("this is not toml ===", encoding="utf-8")
    keymap = load_keymap(path)
    assert keymap.overrides == {}
    assert "using defaults" in keymap.warnings[0]


def test_an_unknown_section_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    path.write_text('[keys]\napply = "ctrl+g"\n[bogus]\nx = 1\n', encoding="utf-8")
    keymap = load_keymap(path)
    assert keymap.overrides == {"apply": "ctrl+g"}  # the valid part still applies
    assert any("bogus" in warning for warning in keymap.warnings)


def test_a_keys_section_that_is_not_a_table_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    path.write_text('keys = "oops"\n', encoding="utf-8")
    keymap = load_keymap(path)
    assert keymap.overrides == {}
    assert "must be a table" in keymap.warnings[0]


def test_the_template_is_written_once(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    write_template(path)
    assert path.exists()
    assert "[keys]" in path.read_text(encoding="utf-8")
    # A second call must not clobber the user's edits.
    path.write_text('[keys]\napply = "ctrl+g"\n', encoding="utf-8")
    write_template(path)
    assert "ctrl+g" in path.read_text(encoding="utf-8")


def test_the_template_is_a_valid_empty_keymap(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    write_template(path)
    assert load_keymap(path).overrides == {}


def test_the_app_writes_the_template_on_first_run(tmp_path: Path) -> None:
    """Discoverability: the format must be findable without reading the source.

    The documented promise is "an empty keybindings.toml appears in the config dir", so
    this asserts the app does it rather than trusting the docstring.
    """
    from sql_table_swiss_knife.storage import EphemeralSecretStore, ProfileStore, SettingsStore
    from sql_table_swiss_knife.tui.app import SwissKnifeApp

    config = tmp_path / "config"
    overrides = config / "keybindings.toml"
    app = SwissKnifeApp(
        profiles=ProfileStore(config / "profiles.toml"),
        secrets=EphemeralSecretStore(),
        settings=SettingsStore(config / "settings.toml"),
        keybindings=overrides,
    )
    del app
    assert overrides.exists()
    assert "[keys]" in overrides.read_text(encoding="utf-8")


def test_a_second_run_does_not_clobber_the_users_keys(tmp_path: Path) -> None:
    """The template is a starting point, not something the app keeps rewriting."""
    from sql_table_swiss_knife.storage import EphemeralSecretStore, ProfileStore, SettingsStore
    from sql_table_swiss_knife.tui.app import SwissKnifeApp

    config = tmp_path / "config"
    overrides = config / "keybindings.toml"
    overrides.parent.mkdir(parents=True)
    overrides.write_text('[keys]\napply = "ctrl+g"\n', encoding="utf-8")
    SwissKnifeApp(
        profiles=ProfileStore(config / "profiles.toml"),
        secrets=EphemeralSecretStore(),
        settings=SettingsStore(config / "settings.toml"),
        keybindings=overrides,
    )
    assert "ctrl+g" in overrides.read_text(encoding="utf-8")
