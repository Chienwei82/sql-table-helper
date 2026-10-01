"""Services: the layer the TUI talks to (DESIGN §2).

Services own orchestration — profile lifecycle, connections, catalog reads, staging and
apply — and return domain objects. They are the only layer that touches ``storage`` and
``providers``; the TUI never imports a provider or a driver directly, and never touches
the filesystem.

This package re-exports the service surface the screens consume, so a screen imports from
``services`` rather than reaching into a submodule. What each one is responsible for:

* :mod:`~sql_table_swiss_knife.services.connection` — profile CRUD, connect/disconnect,
  and the session state the UI shows.
* :mod:`~sql_table_swiss_knife.services.catalog` — databases, tables/views and table
  metadata, with a per-session cache.
* :mod:`~sql_table_swiss_knife.services.data` — paged row fetching, deterministic
  ordering and "fetch more" (FR-3.8).
* :mod:`~sql_table_swiss_knife.services.changes` — the staging buffer. It has no
  connection and cannot reach a database: nothing is written before Apply.
* :mod:`~sql_table_swiss_knife.services.sqlpreview` — the SQL panel's text, produced from
  the same ``SqlStatement`` objects the provider executes, so the preview cannot drift
  from what runs (FR-5.4).
* :mod:`~sql_table_swiss_knife.services.clipboard` — copy encoding and paste *planning*:
  what will be UPDATED and what INSERTed, decided before anything is staged (FR-4.1-4.7).
* :mod:`~sql_table_swiss_knife.services.lookup` — the foreign-key candidate list.
* :mod:`~sql_table_swiss_knife.services.safety` — the environment posture that the header
  badge and the write gate both read, so they cannot disagree.
* :mod:`~sql_table_swiss_knife.services.validation` — live per-cell type checking.
* :mod:`~sql_table_swiss_knife.services.inspector` — the schema panel's view model.

Decisions belong here as pure functions over plain values: "is this Apply allowed?", "what
will this paste do?" Each module's own docstring is the authority for its behaviour.
"""

from .catalog import (
    CatalogService,
    SchemaGroup,
    filter_summaries,
    group_by_schema,
)
from .changes import (
    ApplyError,
    CellStatus,
    ChangeService,
    IdentityInsertWarning,
    MappedError,
    StagedEdit,
    map_database_error,
)
from .clipboard import (
    CellPlan,
    ClipboardBlock,
    ClipboardOptions,
    ClipboardParseError,
    PasteBlockFormat,
    PasteMode,
    PastePlan,
    PasteRowPlan,
    PasteTarget,
    RowOutcome,
    choose_mode,
    detect_format,
    encode_block,
    normalize_text,
    parse_block,
    plan_paste,
    value_to_text,
)
from .connection import (
    ConnectionService,
    ConnectionState,
    SessionInfo,
    duplicate_profile,
)
from .data import DEFAULT_LIMIT, DataService, RowWindow, status_text
from .inspector import (
    ColumnBadge,
    ColumnDetail,
    InspectorService,
    Severity,
    Warning,
    WarningCode,
    build_warnings,
    column_badges,
    column_detail,
    header_label,
    table_summary_rows,
)
from .lookup import (
    LookupChoice,
    LookupResult,
    LookupService,
    description_column,
    lookup_predicates,
)
from .safety import (
    PRODUCTION_CONFIRM_WORD,
    ApplyVerdict,
    BlockedReason,
    SafetyError,
    SafetyPolicy,
    TypedConfirmation,
    WritePermission,
    affected_tables,
    describe_plan,
)
from .transfer import (
    TransferError,
    export_rows,
    read_block,
    render_rows_for_export,
)
from .validation import Hint, HintLevel, ParsedValue, validate_input
from .view import FilterMode, GridView, QuickFilter, toggle_sort, visible_columns

__all__ = [
    "DEFAULT_LIMIT",
    "PRODUCTION_CONFIRM_WORD",
    "ApplyError",
    "ApplyVerdict",
    "BlockedReason",
    "CatalogService",
    "CellPlan",
    "CellStatus",
    "ChangeService",
    "ClipboardBlock",
    "ClipboardOptions",
    "ClipboardParseError",
    "ColumnBadge",
    "ColumnDetail",
    "ConnectionService",
    "ConnectionState",
    "DataService",
    "FilterMode",
    "GridView",
    "Hint",
    "HintLevel",
    "IdentityInsertWarning",
    "InspectorService",
    "LookupChoice",
    "LookupResult",
    "LookupService",
    "MappedError",
    "ParsedValue",
    "PasteBlockFormat",
    "PasteMode",
    "PastePlan",
    "PasteRowPlan",
    "PasteTarget",
    "QuickFilter",
    "RowOutcome",
    "RowWindow",
    "SafetyError",
    "SafetyPolicy",
    "SchemaGroup",
    "SessionInfo",
    "Severity",
    "StagedEdit",
    "TransferError",
    "TypedConfirmation",
    "Warning",
    "WarningCode",
    "WritePermission",
    "affected_tables",
    "build_warnings",
    "choose_mode",
    "column_badges",
    "column_detail",
    "describe_plan",
    "description_column",
    "detect_format",
    "duplicate_profile",
    "encode_block",
    "export_rows",
    "filter_summaries",
    "group_by_schema",
    "header_label",
    "lookup_predicates",
    "map_database_error",
    "normalize_text",
    "parse_block",
    "plan_paste",
    "read_block",
    "render_rows_for_export",
    "status_text",
    "table_summary_rows",
    "toggle_sort",
    "validate_input",
    "value_to_text",
    "visible_columns",
]
