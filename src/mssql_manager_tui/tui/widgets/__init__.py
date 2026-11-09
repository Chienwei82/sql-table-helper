"""Reusable widgets: app chrome, the data grid, the inspector panel and the table tree.

Kept separate from the screens so the same chrome appears on every screen and can be
tested on its own (DESIGN §9.1).
"""

from .data_grid import STATE_GLYPHS, CellState, DataGrid, row_key_for
from .header import ENV_BADGE_CLASS, AppHeader, EnvBadge, badge_text
from .hints import KeyHint, KeyHints
from .inspector import InspectorPanel
from .sql_panel import SqlPanel, syntax_theme
from .status import StatusLine
from .table_tree import TableTree, badges_for, node_label

__all__ = [
    "ENV_BADGE_CLASS",
    "STATE_GLYPHS",
    "AppHeader",
    "Badge",
    "CellState",
    "DataGrid",
    "EnvBadge",
    "InspectorPanel",
    "KeyHint",
    "KeyHints",
    "SqlPanel",
    "StatusLine",
    "TableTree",
    "badge_text",
    "badges_for",
    "node_label",
    "row_key_for",
    "syntax_theme",
]
