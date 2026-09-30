"""SQL preview panel: the statements, the three renderings and the copy actions (FR-5).

The panel is a pure renderer over
:mod:`sql_table_swiss_knife.services.sqlpreview`. Every *decision* — which rendering is
shown, what a copy action yields, whether something can be generated for a row — lives in
that module, so the widget only draws and reports key presses. That is what lets the whole
panel be tested without a terminal and keeps the copy targets and the displayed text from
drifting apart (FR-5.4).

Layout (top to bottom):

1. MODE TABS — Parameterized / Literal / Script, with the current mode's one-line hint and
   the ``preview only`` note, so the user always knows whether looking writes anything.
2. STATEMENT LIST — one row per pending change, in apply order, with its kind and row key.
3. SQL BODY — the selected statement (or the whole script) with SQL syntax highlighting.

Syntax highlighting comes from Rich's ``Syntax`` with a theme picked from the app's own
palette, so the panel follows the active Textual theme instead of shipping fixed colours
that would be unreadable on one of the three themes (FR-3.7).
"""

from collections.abc import Sequence
from typing import ClassVar

from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.reactive import var
from textual.widgets import ListItem, ListView, Static, Tab, Tabs

from ...services.sqlpreview import PREVIEW_ONLY_NOTE, SqlEntry, SqlMode, SqlPreview

__all__ = ["ModeChosen", "SqlPanel", "syntax_theme"]

#: Rich syntax theme per app theme, chosen for contrast on that theme's background. A fixed
#: theme would be unreadable on the light theme, and vice versa.
_SYNTAX_THEMES = {
    "default-dark": "monokai",
    "default-light": "default",
    "solarized": "solarized-dark",
}

#: Kind → colour for the statement list. The kind is also spelled out as a word, so the row
#: is readable without colour (FR-3.7).
_KIND_COLOURS = {"INSERT": "green", "UPDATE": "yellow", "DELETE": "red"}


class ModeChosen(Message):
    """The user picked a rendering by clicking a tab (``mode`` is its lowercase name).

    A plain class rather than a dataclass: Textual's ``Message`` machinery re-enters its own
    ``__init__`` through a ``super()`` closure, which a ``slots=True`` dataclass re-creation
    invalidates (``TypeError: obj is not an instance or subtype of type``). Every Textual
    message therefore has to stay a straightforward subclass.
    """

    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode


def syntax_theme(theme_name: str) -> str:
    """The Rich syntax theme to use for an app theme name."""
    return _SYNTAX_THEMES.get(theme_name, "monokai")


class SqlPanel(VerticalScroll):
    """The SQL preview panel (F3)."""

    DEFAULT_CSS = """
    SqlPanel {
        height: 1fr;
        max-height: 50%;
        border-top: solid $primary;
        background: $surface;
    }
    SqlPanel.-hidden {
        display: none;
    }
    #sql-panel-head {
        height: 1;
        padding: 0 1;
    }
    #sql-panel-hint {
        height: 1;
        color: $text-muted;
    }
    #sql-panel-summary {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    #sql-panel-list {
        height: auto;
        max-height: 8;
        background: $panel;
    }
    #sql-panel-body {
        height: auto;
        padding: 0 1;
    }
    #sql-panel-params {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    """

    #: The preview being rendered. Replacing it re-renders every part of the panel.
    preview: var[SqlPreview | None] = var(None, init=False)
    #: 0-based index into the entries; the extra row past the end is the whole script.
    selected: var[int] = var(-1, init=False)

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("down,j", "next_entry", "Next statement", show=False),
        Binding("up,k", "previous_entry", "Previous statement", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="sql-panel-head"):
            yield Tabs(*(Tab(mode.label, id=f"sql-mode-{mode.name.lower()}") for mode in SqlMode))
        yield Static("", id="sql-panel-hint")
        yield Static("", id="sql-panel-summary")
        yield ListView(id="sql-panel-list")
        yield Static("", id="sql-panel-body")
        yield Static("", id="sql-panel-params")

    def on_mount(self) -> None:
        self._sync_tabs()
        self._render_all()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        """A tab click switched the rendering.

        The tab strip and the mode key are two doors to one value, so the panel only reports
        the choice; the screen owns the mode and rebuilds the preview from it.
        """
        if event.tab is not None and event.tab.id:
            self.post_message(ModeChosen(event.tab.id.removeprefix("sql-mode-")))

    # -- reactive plumbing --------------------------------------------------
    def watch_preview(self) -> None:
        self._render_all()

    def watch_selected(self) -> None:
        self._render_body()

    def _render_all(self) -> None:
        """Redraw every part of the panel from the current preview.

        One entry point on purpose: a partial redraw is how a panel ends up showing a
        statement list from one mode and a body from another.
        """
        self._sync_tabs()
        self._render_hint()
        self._render_summary()
        self._render_list()
        self._render_body()

    def _render_hint(self) -> None:
        preview = self.preview
        if preview is None:
            return
        self.query_one("#sql-panel-hint", Static).update(
            Content(f"{preview.mode.label} — {preview.mode.hint}  ·  {PREVIEW_ONLY_NOTE}")
        )

    def _render_summary(self) -> None:
        preview = self.preview
        if preview is None:
            return
        self.query_one("#sql-panel-summary", Static).update(Content(preview.summary()))

    def _render_list(self) -> None:
        preview = self.preview
        if preview is None:
            return
        listing = self.query_one("#sql-panel-list", ListView)
        listing.clear()
        if preview.generated is not None:
            listing.append(ListItem(Static(Content(preview.generated_title or "statement"))))
            return
        if not preview.entries:
            return
        for entry in preview.entries:
            listing.append(ListItem(Static(self._entry_label(entry))))
        # The script is the last, all-inclusive choice: it is what "copy script" copies.
        listing.append(ListItem(Static(Content("▶ whole script"))))

    def _entry_label(self, entry: SqlEntry) -> Text:
        """The statement-list row: ``1  UPDATE  dbo.Country (Code='DE')``.

        The kind is both a word and a colour, so the row never relies on colour alone
        (FR-3.7).
        """
        kind = entry.kind.value.upper()
        row = "" if entry.row_key is None else f"  {entry.row_key[0][0]}={entry.row_key[0][1]!r}"
        return Text.assemble(
            (f"{entry.index}  ", "dim"),
            (f"{kind}  ", _KIND_COLOURS[kind]),
            (entry.table, ""),
            (row, "dim"),
        )

    def _render_body(self) -> None:
        preview = self.preview
        if preview is None:
            return
        text = self.current_text()
        self.query_one("#sql-panel-body", Static).update(self._highlight(text, preview.mode))
        # The legend belongs to the parameterized *mode*, not to a selection: FR-5.2a says
        # that rendering comes with its parameter values, and the whole-body view is the
        # one a user reads when they want to see every value at once.
        legend = preview.parameters() if preview.mode is SqlMode.PARAMETERIZED else ""
        self.query_one("#sql-panel-params", Static).update(Content(legend))

    def current_text(self) -> str:
        """The text of the current selection in the current mode (also the copy source)."""
        preview = self.preview
        if preview is None:
            return ""
        if preview.generated is not None:
            return preview.generated
        if preview.is_empty:
            # Nothing staged: the hint, not an empty box. Checked before the script mode,
            # because an empty change set has no script either.
            return preview.body()
        if preview.mode is SqlMode.SCRIPT:
            return preview.script()
        if self.selected < 0:
            return preview.body()
        entry = preview.entry(self.selected + 1)
        return entry.text(preview.mode) if entry is not None else preview.body()

    def _highlight(self, text: str, mode: SqlMode) -> Syntax:
        """Syntax-highlighted SQL, themed to match the app.

        The script mode uses the ``tsql`` lexer (the full TRY/CATCH envelope is legal T-SQL);
        a single statement uses the plainer ``sql`` lexer. A hint is left unhighlighted so
        an empty state never looks like broken SQL.
        """
        if not text or text.startswith("no pending changes"):
            return Syntax(text, "sql", theme=syntax_theme(self._theme_name()), word_wrap=True)
        lexer = "tsql" if mode is SqlMode.SCRIPT else "sql"
        return Syntax(
            text,
            lexer,
            theme=syntax_theme(self._theme_name()),
            background_color="default",
            word_wrap=True,
        )

    def _theme_name(self) -> str:
        """The active app theme, or the default before one is applied."""
        try:
            return str(self.app.theme)
        except Exception:  # pragma: no cover - no app attached (bare widget in a test)
            return "default-dark"

    # -- keys ---------------------------------------------------------------
    def action_next_entry(self) -> None:
        """Select the next statement; the row past the last statement is the whole script."""
        total = len(self._rows())
        if total:
            self.selected = -1 if self.selected + 1 >= total else self.selected + 1

    def action_previous_entry(self) -> None:
        """Select the previous statement (wraps round to the script)."""
        total = len(self._rows())
        if total:
            self.selected = -1 if self.selected <= 0 else self.selected - 1

    def _rows(self) -> Sequence[object]:
        """The selectable rows: the entries, then the whole-script row."""
        preview = self.preview
        if preview is None or preview.generated is not None:
            return ()
        return (*preview.entries, "script")

    def _sync_tabs(self) -> None:
        """Point the tab strip at the preview's mode."""
        preview = self.preview
        if preview is None:
            return
        tabs = self.query_one("#sql-panel-head Tabs", Tabs)
        tabs.active = f"sql-mode-{preview.mode.name.lower()}"
