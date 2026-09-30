"""Translation of ``pyodbc.Error`` into the provider error hierarchy (DESIGN §5.2).

The TUI never sees a raw driver exception, and no message ever carries a connection
string or password: messages are rebuilt from the SQLSTATE/vendor code plus a short,
whitelisted explanation.
"""

import re
from collections.abc import Sequence

from ...infra.errors import AppError
from ..errors import AuthError, ConnectError, MetadataError, ProviderError, QueryError

__all__ = ["map_pyodbc_error", "sanitize_driver_message"]

#: SQL Server error numbers that mean "credentials rejected".
_AUTH_ERROR_NUMBERS = frozenset({18456, 18452, 4060, 40615, 18470, 18488})

#: SQLSTATE classes that mean the server could not be reached in time.
_TIMEOUT_SQLSTATES = frozenset({"HYT00", "HYT01", "08S01"})

#: Vendor numbers in the 23xxx class are constraint violations.
_CONSTRAINT_CLASS = "23"

#: Known SQL Server constraint-violation vendor codes (the SQLSTATE class is "23xxx").
_CONSTRAINT_VENDOR_CODES = frozenset({515, 547, 2601, 2627, 2714})

_CONSTRAINT_NAME_RE = re.compile(
    r"(?:constraint|foreign key|primary key|unique constraint|check constraint)\s+'([^']+)'",
    re.IGNORECASE,
)

#: Fragments that must never reach a log or the UI, stripped defensively.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(pwd|password)\s*=\s*[^;\s]+"), r"\1=***"),
    (re.compile(r"(?i)(uid|user\s*id)\s*=\s*[^;\s]+"), r"\1=***"),
    (re.compile(r"(?i)trusted_connection\s*=\s*[^;\s]+"), "Trusted_Connection=***"),
)


def sanitize_driver_message(message: str) -> str:
    """Strip connection-string secrets and collapse whitespace from a driver message."""
    cleaned = message
    for pattern, replacement in _REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    return " ".join(cleaned.split())


def _parts(exc: BaseException) -> tuple[str | None, int | None, str]:
    """Extract ``(sqlstate, vendor_code, message)`` from a pyodbc error's args tuple."""
    args: Sequence[object] = getattr(exc, "args", ())
    sqlstate: str | None = None
    if args and isinstance(args[0], str):
        sqlstate = args[0]
    vendor: int | None = None
    if len(args) > 1 and isinstance(args[1], str) and args[1].strip().lstrip("-").isdigit():
        vendor = int(args[1])
    message = sanitize_driver_message(
        args[2]
        if len(args) > 2 and isinstance(args[2], str)
        else (str(args[0]) if args else exc.__class__.__name__)
    )
    return sqlstate, vendor, message


def map_pyodbc_error(exc: BaseException) -> AppError:
    """Map any exception raised by the driver to an ``AppError`` subtype.

    Non-driver exceptions (programming errors) pass through unchanged so bugs are not
    disguised as connection problems.
    """
    if isinstance(exc, AppError):
        return exc
    sqlstate, vendor, message = _parts(exc)
    detail = f"{exc.__class__.__name__}: {message}"

    if vendor in _AUTH_ERROR_NUMBERS:
        return AuthError("login failed — check username, password and the login's server role")
    if sqlstate in _TIMEOUT_SQLSTATES:
        return ConnectError("connection timed out — check the host, port and firewall")
    if sqlstate and sqlstate.startswith("08"):
        return ConnectError(f"cannot reach the server ({sqlstate})")
    is_constraint = bool(sqlstate and sqlstate.startswith(_CONSTRAINT_CLASS)) or (
        vendor is not None and vendor in _CONSTRAINT_VENDOR_CODES
    )
    if is_constraint:
        name_match = _CONSTRAINT_NAME_RE.search(message)
        suffix = f" [{name_match.group(1)}]" if name_match else ""
        return QueryError(
            f"constraint violation{suffix}: {message}", sqlstate=sqlstate, vendor_code=vendor
        )
    if sqlstate in {"42S02", "42S01", "3701"}:
        return MetadataError(f"object not found: {message}")
    if isinstance(exc, (OSError, TimeoutError)):
        return ConnectError(detail)
    return ProviderError(detail)
