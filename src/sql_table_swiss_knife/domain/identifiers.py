"""Identifier value objects and validation for domain models."""

from dataclasses import dataclass

#: SQL Server identifier limit (longest allowed name).
MAX_IDENTIFIER_LENGTH = 128


def validate_identifier(value: str, *, kind: str = "identifier") -> str:
    """Validate a single SQL identifier part and return it unchanged.

    Raises:
        ValueError: if the name is empty, longer than 128 characters, or contains NUL.
    """
    if not value:
        raise ValueError(f"{kind} must not be empty")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"{kind} must be at most {MAX_IDENTIFIER_LENGTH} characters, got {len(value)}"
        )
    if "\x00" in value:
        raise ValueError(f"{kind} must not contain NUL characters")
    return value


@dataclass(frozen=True, slots=True)
class TableRef:
    """Schema-qualified reference to a table or view."""

    schema: str
    name: str

    def __post_init__(self) -> None:
        validate_identifier(self.schema, kind="schema name")
        validate_identifier(self.name, kind="table name")

    def __str__(self) -> str:
        return f"{self.schema}.{self.name}"
