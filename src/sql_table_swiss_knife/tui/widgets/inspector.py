"""Inspector side panel: summary, warnings banner, column list, column detail (FR-6).

The panel is a pure renderer over :mod:`sql_table_swiss_knife.services.inspector`: it asks
the model for the rows/badges/warnings and turns them into Textual content. All *decisions*
(which warnings exist, what a column's badges are, whether a cell is read-only) live in the
service layer, so they can be tested without a terminal and cannot drift between themes.

Layout (top to bottom):

1. TABLE SUMMARY — schema.name, kind, row count, PK columns, incoming FK count.
2. WARNINGS — coloured by severity, ``INSTEAD OF`` on top, disabled triggers dimmed.
3. COLUMN LIST — every column with its badges; the focused one is highlighted.
4. COLUMN DETAIL — exact type, nullability, default, identity, computed, checks, keys.

Collapsing the panel is the screen's job (``F2``); this widget only renders.
"""

from collections.abc import Mapping

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ...services.inspector import (
    SEVERITIES,
    ColumnBadge,
    ColumnDetail,
    InspectorService,
    Severity,
    Warning,
    type_hint,
)

__all__ = ["InspectorPanel"]

#: Semantic colour role per severity — a role, never a literal colour (FR-3.7).
SEVERITY_ROLES: dict[Severity, str] = {
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.INFO: "muted",
}

#: Fallback colours for the roles, used before the app has an active theme.
_ROLE_FALLBACKS: dict[str, str] = {
    "error": "red",
    "warning": "yellow",
    "muted": "white",
    "pk": "yellow",
    "fk": "magenta",
    "identity": "green",
    "computed": "cyan",
    "nullable": "white",
    "pending": "blue",
}

#: Semantic colour role per badge glyph, falling back to the badge's own role.
_BADGE_ROLES: dict[str, str] = {
    "🔑": "pk",
    "🔗": "fk",
    "#": "identity",
    "ƒ": "computed",
    "⏱": "computed",
    "∅": "nullable",
    "✱": "warning",
    "D": "pending",
    "U": "pk",
    "✓": "warning",
}


class InspectorPanel(VerticalScroll):
    """The collapsible schema-inspector panel."""

    DEFAULT_CSS = """
    InspectorPanel {
        width: 46;
        min-width: 30;
        height: 1fr;
        border-left: solid $panel;
        padding: 0 1;
    }
    InspectorPanel.-collapsed {
        display: none;
    }
    InspectorPanel > .inspector-section {
        height: auto;
        padding: 0 0 1 0;
    }
    InspectorPanel > .inspector-heading {
        height: auto;
        text-style: bold;
        color: $accent;
    }
    InspectorPanel > .inspector-warning {
        height: auto;
    }
    InspectorPanel > .inspector-warning.-dimmed {
        text-style: dim;
    }
    InspectorPanel > .inspector-column.-focused {
        background: $primary 25%;
    }
    """

    def __init__(
        self,
        *,
        inspector: InspectorService | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._inspector = inspector

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("reading metadata…", id="inspector-body")

    # -- public API ---------------------------------------------------------

    def bind_inspector(self, inspector: InspectorService) -> None:
        """Attach the model and render it."""
        self._inspector = inspector
        self.refresh_content()

    def palette(self) -> dict[str, str]:
        """Semantic role → colour of the active theme, with fallbacks.

        Rich styles need real colours, Textual CSS needs role names, so the panel reads
        the theme once per render and hands concrete colours to ``Text``. Every theme
        therefore themes the same widget without any colour literal in this module.
        """
        colors = dict(_ROLE_FALLBACKS)
        if self.is_mounted:
            variables = self.app.theme_variables
            colors.update({role: variables[role] for role in _ROLE_FALLBACKS if role in variables})
        return colors

    def refresh_content(self) -> None:
        """Re-render every section from the model (cheap; no I/O)."""
        if self._inspector is None:
            return
        self.query_one("#inspector-body", Static).update(self.render_markup())

    def render_markup(self) -> Text:
        """The whole panel as one Rich ``Text`` (also what the tests assert on)."""
        inspector = self._inspector
        if inspector is None:
            return Text("reading metadata…")
        colors = self.palette()
        text = Text()
        _section(text, "TABLE SUMMARY")
        for label, value in inspector.summary_rows():
            text.append(f"  {label}: ", style="bold")
            text.append(f"{value}\n")
        _section(text, "WARNINGS")
        warnings = inspector.warnings()
        if not warnings:
            text.append("  • nothing risky detected\n", style="dim")
        else:
            for warning in sorted(warnings, key=lambda w: SEVERITIES.index(w.severity)):
                _warning(text, warning, colors)
        _section(text, "COLUMN LIST")
        focused = inspector.focused_column_name
        for column, badges in inspector.columns():
            text.append(f"  {column.name}", style="bold")
            text.append(f"  {type_hint(column)}\n")
            text.append("    ")
            _badges(text, badges, colors)
            if column.name == focused:
                text.append("  ← focused", style="italic")
            text.append("\n")
        _section(text, "COLUMN DETAIL")
        detail = inspector.detail()
        if detail is None:
            text.append("  no column focused\n", style="dim")
        else:
            _detail(text, detail)
        return text

    # -- collapse -----------------------------------------------------------

    @property
    def collapsed(self) -> bool:
        """True when the panel is hidden."""
        return self.has_class("-collapsed")

    def set_collapsed(self, collapsed: bool) -> None:
        """Show or hide the panel (``F2``)."""
        self.set_class(collapsed, "-collapsed")


def _section(text: Text, title: str) -> None:
    """Write a section heading, preceded by a blank line for readability."""
    text.append(f"\n{title}\n", style="bold")


def _warning(text: Text, warning: Warning, colors: Mapping[str, str]) -> None:
    """Write one warnings-banner entry: glyph + title, message and indented notes."""
    role = SEVERITY_ROLES[warning.severity]
    color = colors.get(role, "white")
    style = color if not warning.dimmed else f"dim {color}"
    text.append(f"  {warning.glyph} {warning.title}\n", style=f"bold {style}")
    text.append(f"    {warning.message}\n", style=style)
    for note in warning.notes:
        text.append(f"      · {note}\n", style=style)


def _badges(text: Text, badges: tuple[ColumnBadge, ...], colors: Mapping[str, str]) -> None:
    """Write a badge row: one glyph per fact, coloured by the role it stands for."""
    for index, badge in enumerate(badges):
        if index:
            text.append(" ")
        role = _BADGE_ROLES.get(badge.glyph, badge.role)
        text.append(badge.glyph, style=f"bold {colors.get(role, 'white')}")


def _detail(text: Text, detail: ColumnDetail) -> None:
    """Write the COLUMN DETAIL fact list of the focused column."""
    text.append(f"  {detail.column.name}\n", style="bold")
    for label, value in detail.rows:
        text.append(f"  {label}: ", style="bold")
        text.append(f"{value}\n")
