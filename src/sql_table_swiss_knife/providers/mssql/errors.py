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

#: The driver echoes the SQLSTATE at the start of its message, e.g. ``[42000] [Microsoft]...``.
_SQLSTATE_PREFIX_RE = re.compile(r"^\[([0-9A-Z]{5})\]")

#: The vendor number appears in parentheses before the function name, e.g. ``(18456)``.
_VENDOR_CODE_RE = re.compile(r"\((\d{3,5})\)")

#: Fragments that must never reach a log or the UI, stripped defensively.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(pwd|password)\s*=\s*[^;\s]+"), r"\1=***"),
    (re.compile(r"(?i)(uid|user\s*id)\s*=\s*[^;\s]+"), r"\1=***"),
    (re.compile(r"(?i)trusted_connection\s*=\s*[^;\s]+"), "Trusted_Connection=***"),
)


#: Substrings the driver uses when the TLS handshake itself is what failed.
_TLS_MARKERS = ("ssl provider", "certificate", "tls", "handshake", "trust server certificate")


def _looks_like_tls_failure(message: str) -> bool:
    """Whether a connection error is about the TLS handshake rather than reachability."""
    lowered = message.lower()
    return any(marker in lowered for marker in _TLS_MARKERS)


def sanitize_driver_message(message: str) -> str:
    """Strip connection-string secrets and collapse whitespace from a driver message."""
    cleaned = message
    for pattern, replacement in _REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    return " ".join(cleaned.split())


def _parts(exc: BaseException) -> tuple[str | None, int | None, str]:
    """Extract ``(sqlstate, vendor_code, message)`` from a pyodbc error's args tuple.

    pyodbc puts the SQLSTATE in ``args[0]`` and the full driver text in ``args[1]`` — the
    vendor number is only *inside* that text, as in ``... (18456) (SQLDriverConnect)``.
    Taking ``args[1]`` as the vendor number therefore never fires, and a failed login came
    back as an opaque ``ProviderError: InterfaceError: 28000`` instead of ``AuthError``.
    So the vendor code and the repeated SQLSTATE prefix are parsed out of the message.
    """
    args: Sequence[object] = getattr(exc, "args", ())
    sqlstate: str | None = None
    if args and isinstance(args[0], str):
        sqlstate = args[0]
    message = sanitize_driver_message(
        args[2]
        if len(args) > 2 and isinstance(args[2], str)
        else (
            args[1]
            if len(args) > 1 and isinstance(args[1], str)
            else (str(args[0]) if args else exc.__class__.__name__)
        )
    )
    if sqlstate is None:
        prefix = _SQLSTATE_PREFIX_RE.match(message)
        if prefix is not None:
            sqlstate = prefix.group(1)
    vendor: int | None = None
    # Some drivers pass the vendor code as its own arg; pyodbc itself does not.
    if len(args) > 1 and isinstance(args[1], str) and args[1].strip().lstrip("-").isdigit():
        vendor = int(args[1])
    else:
        vendor_match = _VENDOR_CODE_RE.search(message)
        vendor = int(vendor_match.group(1)) if vendor_match is not None else None
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
        # A TLS handshake failure also arrives as 08001, so the generic wording below
        # would tell the user to check the firewall when the real cause is the server's
        # certificate. Keep the driver's own reason, sanitized.
        if _looks_like_tls_failure(message):
            return ConnectError(
                f"TLS handshake failed ({sqlstate}): {sanitize_driver_message(message)}"
            )
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
    # "Can't open lib ..." means the ODBC driver is not installed on this machine, which is
    # a connection-setup problem the user can act on, not an opaque provider failure.
    if "can't open lib" in message.lower() or "driver manager" in message.lower():
        return ConnectError(f"ODBC driver not available: {message}")
    if isinstance(exc, (OSError, TimeoutError)):
        return ConnectError(detail)
    return ProviderError(detail)
