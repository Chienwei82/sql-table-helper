"""Live cell validation: parse a typed value against the column's declared type.

FR-3.5 asks for typed values to be validated *before* they are staged. This module is the
pure part of that: it takes the raw text of an in-progress edit plus the column metadata and
returns a list of :class:`Hint` objects — what is wrong (if anything), plus the counters the
user needs while typing (length, precision/scale).

Deliberate boundaries:

* Nothing here talks to the database, and no CHECK expression or UNIQUE index is
  evaluated. Those risks are reported as hints marked ``verified_by_database`` so the UI
  can say "will be verified by the database" instead of pretending to know the answer.
* The parsers cover the types the milestone names (int, decimal, date, uniqueidentifier,
  bit) plus the obvious neighbours. An unknown type is accepted as text rather than
  rejected — refusing to type into a column because the app does not know its type would
  be worse than letting the server judge it.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import IntEnum

from ..domain.catalog import Column, Table
from .inspector import checks_for, fks_for, uniques_for

__all__ = [
    "VERIFIED_BY_DATABASE",
    "Hint",
    "HintLevel",
    "ParsedValue",
    "parse_value",
    "validate_input",
]

#: Suffix appended to every hint the database, not the app, has the final word on.
VERIFIED_BY_DATABASE = "will be verified by the database"

#: Types whose value is a signed/unsigned integer.
_INT_TYPES = {"tinyint", "smallint", "int", "bigint"}
_DECIMAL_TYPES = {"decimal", "numeric", "money", "smallmoney"}
_FLOAT_TYPES = {"float", "real"}
_DATE_TYPES = {"date"}
_DATETIME_TYPES = {"datetime", "datetime2", "smalldatetime", "datetimeoffset"}
_TIME_TYPES = {"time"}
_STRING_TYPES = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
_BIT_TRUE = {"1", "true", "yes", "y", "on"}
_BIT_FALSE = {"0", "false", "no", "n", "off"}
_UNSIGNED_INT = re.compile(r"^\d+$")


#: Glyph per hint level — the hint stays legible without colour (FR-3.7).
_HINT_GLYPHS: dict[HintLevel, str] = {}


class HintLevel(IntEnum):
    """Severity of a validation hint: blocking, advisory or purely informational."""

    OK = 0
    INFO = 1
    WARNING = 2
    ERROR = 3


_HINT_GLYPHS.update(
    {
        HintLevel.OK: "✓",
        HintLevel.INFO: "•",
        HintLevel.WARNING: "⚠",
        HintLevel.ERROR: "⛔",
    }
)


@dataclass(frozen=True, slots=True)
class Hint:
    """One live hint shown under the edited cell."""

    level: HintLevel
    text: str

    @property
    def is_error(self) -> bool:
        """True when the hint must block staging (FR-3.5)."""
        return self.level is HintLevel.ERROR

    @property
    def glyph(self) -> str:
        """Marker so the state is readable without colour (FR-3.7)."""
        return _HINT_GLYPHS[self.level]


@dataclass(frozen=True, slots=True)
class ParsedValue:
    """Outcome of parsing one edited cell.

    ``value`` is the Python object to stage (``None`` means SQL NULL); it is only
    meaningful when ``ok`` is True. ``hints`` is the ordered list the UI shows.
    """

    ok: bool
    value: object
    hints: tuple[Hint, ...]

    @property
    def errors(self) -> tuple[Hint, ...]:
        return tuple(hint for hint in self.hints if hint.is_error)

    @property
    def message(self) -> str:
        """One-line summary: the first error, else the most interesting remaining hint."""
        if self.errors:
            return self.errors[0].text
        for hint in reversed(self.hints):
            if hint.level is HintLevel.WARNING:
                return hint.text
        return self.hints[0].text if self.hints else ""


def _type_name(column: Column) -> str:
    """The declared type, lowercased, so the parser tables can be plain sets."""
    return column.data_type.lower()


def _int_limits(column: Column) -> tuple[int, int]:
    """Inclusive value range of an integer column from its declared precision.

    ``tinyint … bigint`` map to 8/16/32/64 bits by name; a column declaring an explicit
    precision is trusted only for ``int``-sized types, otherwise the type's own range
    applies. ``bit`` is handled separately (0/1).
    """
    kind = _type_name(column)
    if kind == "tinyint":
        return 0, 255
    if kind == "smallint":
        return -(2**15), 2**15 - 1
    if kind == "bigint":
        return -(2**63), 2**63 - 1
    if column.precision is not None and 1 <= column.precision <= 64:
        if kind == "tinyint":
            return 0, 2**column.precision - 1
        return -(2 ** (column.precision - 1)), 2 ** (column.precision - 1) - 1
    return -(2**31), 2**31 - 1


def _digits(value: Decimal) -> tuple[int, int]:
    """``(total digits, fractional digits)`` of a decimal, sign excluded."""
    exponent = value.as_tuple().exponent
    fraction = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    total = len(value.as_tuple().digits) + max(0, fraction - len(value.as_tuple().digits))
    return max(total, len(value.as_tuple().digits)), fraction


def _length_limit(column: Column) -> int | None:
    """Maximum number of *characters* for a character column, or ``None``.

    ``max_length`` is a byte count from ``sys.columns``; ``nvarchar``/``nchar`` declare
    their length in characters, so it is halved (same rule as the type formatter).
    """
    if column.max_length is None:
        return None
    kind = _type_name(column)
    if kind in {"nvarchar", "nchar"}:
        return max(1, column.max_length // 2)
    return column.max_length


def parse_value(column: Column, text: str) -> tuple[object, tuple[Hint, ...]]:
    """Parse ``text`` for ``column``.

    Returns the Python value (``None`` for SQL NULL) and the *blocking* hints found. An
    empty string and the literal ``NULL`` both mean SQL NULL; whether that is allowed is
    decided by :func:`validate_input`, which owns the column-level rules.

    Raises:
        ValueError: when the text cannot be represented in the column's type; the message
            is the user-facing hint text.
    """
    stripped = text.strip()
    if stripped == "" or stripped.upper() == "NULL":
        return None, ()
    kind = _type_name(column)
    if kind == "bit":
        lowered = stripped.lower()
        if lowered in _BIT_TRUE:
            return True, ()
        if lowered in _BIT_FALSE:
            return False, ()
        raise ValueError(f"{column.name} is bit: use 0/1, true/false (got {text!r})")
    if kind in _INT_TYPES:
        try:
            number = int(stripped)
        except ValueError:
            raise ValueError(f"{column.name} is {kind}: {text!r} is not a whole number") from None
        low, high = _int_limits(column)
        if not low <= number <= high:
            raise ValueError(f"{column.name} is {kind}: {number} is outside {low}…{high}")
        return number, ()
    if kind in _DECIMAL_TYPES | _FLOAT_TYPES:
        try:
            numeric = Decimal(stripped)
        except InvalidOperation:
            raise ValueError(f"{column.name} is {kind}: {text!r} is not a number") from None
        return (float(numeric) if kind in _FLOAT_TYPES else numeric), ()
    if kind in _DATE_TYPES:
        try:
            return date.fromisoformat(stripped), ()
        except ValueError:
            raise ValueError(f"{column.name} is date: use YYYY-MM-DD (got {text!r})") from None
    if kind in _DATETIME_TYPES:
        try:
            return datetime.fromisoformat(stripped.replace("Z", "+00:00")), ()
        except ValueError:
            raise ValueError(
                f"{column.name} is {kind}: use YYYY-MM-DD[ HH:MM:SS] (got {text!r})"
            ) from None
    if kind in _TIME_TYPES:
        try:
            return time.fromisoformat(stripped), ()
        except ValueError:
            raise ValueError(f"{column.name} is time: use HH:MM[:SS] (got {text!r})") from None
    if kind == "uniqueidentifier":
        try:
            return str(uuid.UUID(stripped)), ()
        except ValueError:
            raise ValueError(f"{column.name} is uniqueidentifier: not a GUID ({text!r})") from None
    if kind in {"binary", "varbinary"}:
        body = stripped[2:] if stripped.lower().startswith("0x") else stripped
        try:
            return bytes.fromhex(body), ()
        except ValueError:
            raise ValueError(f"{column.name} is {kind}: not hex digits ({text!r})") from None
    return text, ()


def validate_input(table: Table, column: Column, text: str) -> ParsedValue:
    """Validate ``text`` for ``column`` and return the value plus every live hint.

    Args:
        table: The owning table, used for the CHECK/UNIQUE/FK hints.
        column: The column being edited.
        text: Raw text from the cell editor.

    Returns:
        A :class:`ParsedValue`; ``ok`` is False when a hint blocks staging.
    """
    hints: list[Hint] = []
    if column.is_server_managed:
        return ParsedValue(
            ok=False,
            value=None,
            hints=(Hint(HintLevel.ERROR, f"{column.name} is read-only (S-3): not editable"),),
        )
    try:
        value, extra = parse_value(column, text)
    except ValueError as exc:
        return ParsedValue(ok=False, value=None, hints=(Hint(HintLevel.ERROR, str(exc)),))
    hints.extend(extra)

    if value is None:
        if not column.nullable:
            return ParsedValue(
                ok=False,
                value=None,
                hints=(
                    *hints,
                    Hint(HintLevel.ERROR, f"{column.name} is NOT NULL — NULL is rejected"),
                ),
            )
        hints.append(Hint(HintLevel.INFO, f"{column.name} accepts NULL"))
        return ParsedValue(ok=True, value=None, hints=tuple(hints))

    hints.extend(_value_hints(column, value))
    hints.extend(_constraint_hints(table, column))
    return ParsedValue(ok=not any(hint.is_error for hint in hints), value=value, hints=tuple(hints))


def _value_hints(column: Column, value: object) -> list[Hint]:
    """Counter/range hints for a successfully parsed value (length, precision, scale)."""
    hints: list[Hint] = []
    limit = _length_limit(column)
    if isinstance(value, str) and limit is not None and _type_name(column) in _STRING_TYPES:
        length = len(value)
        if length > limit:
            hints.append(
                Hint(
                    HintLevel.ERROR,
                    f"{column.name} holds at most {limit} characters (now {length})",
                )
            )
        else:
            hints.append(Hint(HintLevel.INFO, f"{length}/{limit} characters"))
    if isinstance(value, Decimal):
        total, fraction = _digits(value)
        precision = column.precision
        scale = column.scale or 0
        if precision is not None:
            if fraction > scale:
                hints.append(
                    Hint(
                        HintLevel.ERROR,
                        f"{column.name} has scale {scale}: {fraction} decimal places given",
                    )
                )
            elif total > precision:
                hints.append(
                    Hint(
                        HintLevel.ERROR,
                        f"{column.name} holds {precision} digits: {total} given",
                    )
                )
            else:
                hints.append(
                    Hint(HintLevel.INFO, f"{total}/{precision} digits, {fraction}/{scale} scale")
                )
    return hints


def _constraint_hints(table: Table, column: Column) -> list[Hint]:
    """Risks the app cannot decide: CHECK expressions, UNIQUE keys and foreign keys."""
    hints: list[Hint] = []
    uniques = uniques_for(table, column.name)
    if uniques:
        hints.append(
            Hint(
                HintLevel.WARNING,
                f"{uniques[0].name} requires a unique value here — {VERIFIED_BY_DATABASE}",
            )
        )
    for check in checks_for(table, column.name):
        hints.append(
            Hint(
                HintLevel.WARNING,
                f"{check} may reject this value — {VERIFIED_BY_DATABASE}",
            )
        )
    for fk in fks_for(table, column.name):
        target = f"{fk.referenced_schema}.{fk.referenced_table}." + ".".join(fk.referenced_columns)
        hints.append(
            Hint(
                HintLevel.INFO,
                f"must exist in {target} or the INSERT/UPDATE fails — {VERIFIED_BY_DATABASE}",
            )
        )
    return hints
