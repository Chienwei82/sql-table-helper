"""Screens for the swiss-knife TUI.

``ConnectionsScreen`` is the entry point; ``TableBrowserScreen`` and
``TableEditorScreen`` follow it. The modal screens (``ProfileEditScreen``,
``DatabasePickerScreen``, ``ConfirmScreen``) are pushed on top of a screen and dismiss
with a result value.
"""

from .base import AppScreen
from .confirm import ConfirmScreen
from .connections import CONNECTIONS_TITLE, ConnectionsScreen
from .database_picker import DatabasePickerScreen
from .profile_edit import ProfileEditScreen
from .table_browser import TABLE_BROWSER_TITLE, TableBrowserScreen
from .table_editor import TableEditorScreen

__all__ = [
    "CONNECTIONS_TITLE",
    "TABLE_BROWSER_TITLE",
    "AppScreen",
    "ConfirmScreen",
    "ConnectionsScreen",
    "DatabasePickerScreen",
    "ProfileEditScreen",
    "TableBrowserScreen",
    "TableEditorScreen",
]
