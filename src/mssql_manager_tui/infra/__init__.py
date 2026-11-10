"""Infrastructure helpers: the error hierarchy and the clipboard backend chain."""

from .clipboard import (
    ClipboardBackend,
    ClipboardService,
    CopyOutcome,
    NativeToolBackend,
    Osc52Backend,
    PlatformCopyTools,
    PyperclipBackend,
    default_backends,
    osc52_sequence,
)
from .errors import AppError

__all__ = [
    "AppError",
    "ClipboardBackend",
    "ClipboardService",
    "CopyOutcome",
    "NativeToolBackend",
    "Osc52Backend",
    "PlatformCopyTools",
    "PyperclipBackend",
    "default_backends",
    "osc52_sequence",
]
