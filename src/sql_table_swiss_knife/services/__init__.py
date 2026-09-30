"""Services: the layer the TUI talks to (DESIGN §2).

Services own I/O orchestration — profile lifecycle, connections, catalog reads —
and return domain objects. They are the only layer that touches ``storage`` and
``providers``; the TUI never imports a provider or a driver directly.

M3 lands two services:

* :class:`~sql_table_swiss_knife.services.connection.ConnectionService` — profile
  CRUD, test/connect/disconnect, and the session state shown in the UI.
* :class:`~sql_table_swiss_knife.services.catalog.CatalogService` — databases,
  tables/views and table metadata, with a per-session cache.
"""

from .catalog import (
    CatalogService,
    SchemaGroup,
    filter_summaries,
    group_by_schema,
)
from .connection import (
    ConnectionService,
    ConnectionState,
    SessionInfo,
    duplicate_profile,
)

__all__ = [
    "CatalogService",
    "ConnectionService",
    "ConnectionState",
    "SchemaGroup",
    "SessionInfo",
    "duplicate_profile",
    "filter_summaries",
    "group_by_schema",
]
