"""Screens for the swiss-knife TUI.

``ConnectionsScreen`` is the entry point; ``TableBrowserScreen`` and
``TableEditorScreen`` follow it. The modal screens (``ProfileEditScreen``,
``DatabasePickerScreen``, ``ConfirmScreen``) are pushed on top of a screen and dismiss
with a result value.
"""

from .apply_confirm import TRANSACTION_NOTE, ApplyConfirmScreen
from .base import AppScreen
from .cell_editor import CellEditorScreen
from .cell_view import CellViewScreen
from .column_picker import ColumnPickerScreen
from .confirm import ConfirmScreen
from .connections import CONNECTIONS_TITLE, ConnectionsScreen
from .database_picker import DatabasePickerScreen
from .help import SAFETY_NOTES, HelpScreen
from .lookup_picker import LookupPickerScreen
from .paste_preview import PastePreviewScreen
from .profile_edit import ProfileEditScreen
from .quick_filter import QuickFilterScreen
from .sql_action import SqlActionScreen
from .table_browser import TABLE_BROWSER_TITLE, TableBrowserScreen
from .table_editor import TableEditorScreen
from .transfer_path import PathPromptScreen

__all__ = [
    "CONNECTIONS_TITLE",
    "SAFETY_NOTES",
    "TABLE_BROWSER_TITLE",
    "TRANSACTION_NOTE",
    "AppScreen",
    "ApplyConfirmScreen",
    "CellEditorScreen",
    "CellViewScreen",
    "ColumnPickerScreen",
    "ConfirmScreen",
    "ConnectionsScreen",
    "DatabasePickerScreen",
    "HelpScreen",
    "LookupPickerScreen",
    "PastePreviewScreen",
    "PathPromptScreen",
    "ProfileEditScreen",
    "QuickFilterScreen",
    "SqlActionScreen",
    "TableBrowserScreen",
    "TableEditorScreen",
]
