"""Reusable widgets: app header, key hints, status line and the table tree.

Kept separate from the screens so the same chrome appears on every screen and can be
tested on its own (DESIGN §9.1).
"""

from .header import AppHeader
from .hints import KeyHint, KeyHints
from .status import StatusLine
from .table_tree import TableTree, badges_for, node_label

__all__ = [
    "AppHeader",
    "Badge",
    "KeyHint",
    "KeyHints",
    "StatusLine",
    "TableTree",
    "badges_for",
    "node_label",
]
