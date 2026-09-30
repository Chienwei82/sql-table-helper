"""Services: the layer the TUI talks to (DESIGN §2).

Services own I/O orchestration — profile lifecycle, connections, catalog reads —
and return domain objects. They are the only layer that touches ``storage`` and
``providers``; the TUI never imports a provider or a driver directly.

M3 landed two services:

* :class:`~sql_table_swiss_knife.services.connection.ConnectionService` — profile
  CRUD, test/connect/disconnect, and the session state shown in the UI.
* :class:`~sql_table_swiss_knife.services.catalog.CatalogService` — databases,
  tables/views and table metadata, with a per-session cache.

M4 adds the read side of the workspace:

* :class:`~sql_table_swiss_knife.services.data.DataService` — paged row fetching with
  the deterministic order and the "fetch more" semantics behind FR-3.8/S-8.
* :class:`~sql_table_swiss_knife.services.inspector.InspectorService` — which table and
  column the schema panel is showing, plus the derived view model (summary, warnings,
  badges, column detail) built by the pure functions in that module.
* :mod:`sql_table_swiss_knife.services.validation` — live per-cell type validation for
  the editor that lands with M5.

M6 adds the SQL side of the workspace:

* :mod:`sql_table_swiss_knife.services.sqlpreview` — the three renderings (parameterized,
  literal, script), the panel's copy targets and the "generate SQL for…" builders. Pure
  functions over the same ``SqlStatement`` objects the provider executes, so the preview
  cannot drift from what runs (FR-5.4), and it has no I/O at all: the panel never executes
  anything (S-1).
M7 adds the clipboard, which is the milestone where "edit without writing SQL" starts
including bulk work:

* :mod:`sql_table_swiss_knife.services.clipboard` — pure encoding, parsing and paste
  *planning*: TSV/CSV/JSON out, TSV/CSV/JSON/JSON in, header detection, per-locale
  conversion, and a :class:`~services.clipboard.PastePlan` that says exactly which rows
  will be UPDATED and which INSERTed before anything is staged (FR-4.1-4.7).
* :mod:`sql_table_swiss_knife.services.transfer` — CSV/JSON import and export built on the
  same pipeline, so a file can never be validated differently from a paste.
* :mod:`sql_table_swiss_knife.infra.clipboard` — the copy backend chain (pyperclip,
  platform tools, then OSC 52 so copying works over SSH).
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
