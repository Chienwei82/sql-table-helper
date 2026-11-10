"""Table picker tree: schema groups with kind/PK/trigger/FK badges (FR-2.2, FR-2.3).

Badges are *glyphs*, not just colours: 🔑 has a primary key, ⚡ has triggers, 🔗 has
foreign keys, ⚠ has no primary key (read-only, S-4), 👁 is a view. The glyph travels
with the label so the meaning survives every theme and any colour-vision difference
(FR-3.7); the colours come from the active theme's semantic roles via ``$pk``,
``$fk``, ``$warning`` and friends.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from rich.text import Text
from textual.widgets import Tree
from textual.widgets._tree import TreeNode

from ...domain.catalog import TableKind, TableSummary
from ...services import SchemaGroup

__all__ = ["BADGE_ROLES", "Badge", "TableTree", "badges_for", "node_label"]

#: Data stored on a schema node (leaf nodes carry a TableSummary instead).
SchemaNodeData = str

#: Every colour role a badge may use; also the keys read back from the theme.
BADGE_ROLES = ("pk", "fk", "warning", "nullable")


@dataclass(frozen=True, slots=True)
class Badge:
    """One glyph shown next to a table name."""

    glyph: str
    label: str
    #: CSS colour name — a *semantic role*, never a literal colour.
    role: str


def badges_for(summary: TableSummary) -> tuple[Badge, ...]:
    """Badges describing a table/view, in a fixed, testable order.

    A table without a primary key gets ⚠ (and no 🔑): that is the read-only warning
    of S-4, and it is the one badge that is deliberately alarming.
    """
    badges: list[Badge] = []
    if summary.has_primary_key:
        badges.append(Badge("🔑", "primary key", "pk"))
    else:
        badges.append(Badge("⚠", "no primary key — read-only", "warning"))
    if summary.has_foreign_keys:
        badges.append(Badge("🔗", "foreign key", "fk"))
    if summary.has_triggers:
        badges.append(Badge("⚡", "has triggers", "warning"))
    if summary.kind is TableKind.VIEW:
        badges.append(Badge("👁", "view — read-only", "nullable"))
    return tuple(badges)


def node_label(summary: TableSummary, palette: Mapping[str, str] | None = None) -> Text:
    """Render ``name  badges  rows≈N`` for one table/view row.

    ``palette`` maps a semantic role to the colour of the active theme (see
    :meth:`TableTree.palette`); without one the glyphs are still shown, uncoloured.
    The glyphs carry the meaning and the colour only reinforces it, so the row reads
    the same in every theme and for any colour-vision difference (FR-3.7).
    """
    colors = palette or {}
    badges = badges_for(summary)
    label = Text()
    # The trailing space lives inside the name segment: a leading space on the next
    # segment would be trimmed when the tree renders the label.
    label.append(f"{summary.name} " if badges else summary.name, style="bold")
    for badge in badges:
        label.append_text(Text(" "))  # separator between glyphs
        label.append(badge.glyph, style=_style(colors.get(badge.role), bold=True))
    if summary.approximate_row_count is not None:
        estimate = f"  rows≈{summary.approximate_row_count}"
        label.append(estimate, style=_style(colors.get("nullable")))
    return label


def _style(color: str | None, *, bold: bool = False) -> str:
    """Build a Rich style string from a resolved colour."""
    parts = [part for part in (color, "bold" if bold else None) if part]
    return " ".join(parts)


class TableTree(Tree[TableSummary | SchemaNodeData]):
    """Two-level tree: schema headers (expandable) over their tables and views."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__("tables", **kwargs)  # type: ignore[arg-type]
        self.show_root = False

    def palette(self) -> Mapping[str, str]:
        """Semantic role → colour for the active theme (empty before mount)."""
        if not self.is_mounted:
            return {}
        return {
            role: self.app.theme_variables[role]
            for role in BADGE_ROLES
            if role in self.app.theme_variables
        }

    def populate(self, groups: tuple[SchemaGroup, ...]) -> None:
        """Replace the tree contents with the given schema groups.

        Groups are rebuilt from scratch on every filter keystroke; rebuilding is
        cheap (hundreds of nodes) and avoids stale-node bugs when a schema loses its
        last match (FR-2.2: empty groups disappear).
        """
        self.clear()
        self.root.expand()
        for group in groups:
            node = self.root.add(group.label, data=group.schema, expand=True)
            for summary in group.tables:
                node.add_leaf(node_label(summary, self.palette()), data=summary)

    def selected_summary(self) -> TableSummary | None:
        """The table under the cursor, or ``None`` when a schema header is selected."""
        node = self.cursor_node
        while node is not None:
            data = node.data
            if isinstance(data, TableSummary):
                return data
            node = node.parent
        return None

    def selected_schema(self) -> str | None:
        """The schema under the cursor, or ``None`` when a table is selected."""
        node = self.cursor_node
        while node is not None:
            if isinstance(node.data, str):
                return str(node.data)
            node = node.parent
        return None

    def toggle_schemas(self) -> None:
        """Collapse every schema group, or expand them all if all are collapsed."""
        groups = self.root.children
        if not groups:
            return
        # Mixed state counts as "expanded": the next F5 normalises to collapsed.
        collapse = any(group.is_expanded for group in groups)
        for group in groups:
            group.collapse() if collapse else group.expand()

    def focus_first_table(self) -> TreeNode[TableSummary | SchemaNodeData] | None:
        """Move the cursor to the first table (skipping schema headers).

        Uses :meth:`Tree.move_cursor` rather than ``select_node``: selecting posts a
        ``NodeSelected`` message, which the screen treats as "open this table" — the
        cursor must be able to move on every filter keystroke without opening anything.
        """
        for group in self.root.children:
            for child in group.children:
                self.move_cursor(child)
                self.scroll_to_node(child)
                return child
        return None
