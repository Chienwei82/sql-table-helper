"""Microsoft SQL Server provider: T-SQL dialect + pyodbc-backed metadata read path (M2)."""

from .connection import build_connection_string, sanitize_connection_string
from .dialect import TSqlDialect
from .provider import MssqlConnection, MssqlProvider

__all__ = [
    "MssqlConnection",
    "MssqlProvider",
    "TSqlDialect",
    "build_connection_string",
    "sanitize_connection_string",
]
