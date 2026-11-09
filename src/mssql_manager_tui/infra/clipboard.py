"""Clipboard backends: native first, OSC 52 as the fallback that survives SSH (FR-4.3).

Copying out of a terminal application has two workable mechanisms and neither is
universal:

* a **native** backend — ``pyperclip`` when installed, otherwise the platform's own
  command line tool (``pbcopy``, ``xclip``/``wl-copy``, ``clip.exe``). These write the
  *system* clipboard, but only when a graphical session is reachable;
* **OSC 52** — ``ESC ] 52 ; c ; <base64> BEL``. The terminal emulator writes it into its
  own clipboard, so it works over SSH and inside tmux, which is where this app is most
  often used.

So :func:`default_backends` orders them native-first (a system clipboard is the least
surprising place to find the text afterwards) with OSC 52 as the always-available
fallback. Reading is different: there is no portable *read* escape sequence, so
:meth:`ClipboardService.read` only uses native backends and reports "no clipboard"
when none of them works — the caller then says so instead of silently pasting nothing.

Every backend takes an injectable command runner, so the chain is testable without a
terminal, a display server or ``pyperclip`` installed.
"""

import base64
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Protocol, cast

__all__ = [
    "ClipboardBackend",
    "ClipboardService",
    "CommandRunner",
    "CopyOutcome",
    "NativeToolBackend",
    "Osc52Backend",
    "PlatformCopyTools",
    "PyperclipBackend",
    "PyperclipModule",
    "default_backends",
    "osc52_sequence",
]

#: How a command is executed: ``(argv, stdin) -> exit code 0``. Injected by tests.
CommandRunner = Callable[[Sequence[str], str | None], bool]


def _run_command(argv: Sequence[str], stdin: str | None = None) -> bool:
    """Run ``argv`` feeding ``stdin``; True when it exited 0.

    Never raises: a missing tool or a failed spawn is just "this backend is unavailable",
    which is exactly what the chain needs in order to try the next one.
    """
    try:
        # argv comes from the fixed COMMANDS table, never from user input.
        completed = subprocess.run(
            list(argv), input=stdin, capture_output=True, text=True, check=False
        )
    except OSError, ValueError:
        return False
    return completed.returncode == 0


class ClipboardBackend(Enum):
    """Which mechanism a copy went through — reported to the user, never assumed."""

    PYPERCLIP = "pyperclip"
    PLATFORM = "platform"
    OSC52 = "osc52"


@dataclass(frozen=True, slots=True)
class CopyOutcome:
    """Result of one copy attempt chain.

    ``backend`` is ``None`` when nothing could copy, which is worth saying out loud:
    silently pretending a copy worked is how users lose data.
    """

    text: str
    backend: ClipboardBackend | None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.backend is not None

    def describe(self) -> str:
        """One line for the status bar: which mechanism took the text."""
        if self.backend is ClipboardBackend.OSC52:
            return "copied via OSC 52 (the terminal's clipboard)"
        if self.backend is ClipboardBackend.PYPERCLIP:
            return "copied via pyperclip"
        if self.backend is ClipboardBackend.PLATFORM:
            return f"copied via {self.detail or 'the platform clipboard'}"
        return self.detail or "could not reach the clipboard"


#: The slice of ``pyperclip`` this module uses, as a Protocol: the optional extra is not
#: installed in most environments, so it cannot be imported for type checking.
class PyperclipModule(Protocol):
    def determine_clipboard(self) -> object: ...
    def copy(self, text: str) -> object: ...
    def paste(self) -> object: ...


def _load_pyperclip() -> PyperclipModule | None:
    """Import ``pyperclip`` if the optional extra is installed, else ``None``."""
    try:
        import pyperclip  # type: ignore[import-untyped]
    except Exception:  # pragma: no cover - depends on the host's extras
        return None
    return cast("PyperclipModule", pyperclip)


class PyperclipBackend:
    """``pyperclip`` when it happens to be installed (it is an optional extra)."""

    def __init__(self, module: PyperclipModule | None = None) -> None:
        self._module = module if module is not None else _load_pyperclip()

    @property
    def available(self) -> bool:
        """True when pyperclip imported *and* reports a working clipboard."""
        module = self._module
        if module is None:
            return False
        try:
            return bool(module.determine_clipboard())
        except Exception:
            return False

    def copy(self, text: str) -> bool:
        module = self._module
        if module is None:
            return False
        try:
            module.copy(text)
        except Exception:
            return False
        return True

    def read(self) -> str | None:
        module = self._module
        if module is None:
            return None
        try:
            return str(module.paste())
        except Exception:
            return None


class PlatformCopyTools:
    """Per-platform copy/read command lines, in preference order.

    Several tools are listed per platform because "the clipboard command" is genuinely
    ambiguous: a Wayland session has ``wl-copy`` and not ``xclip``, an X11 session inside
    WSL has the reverse, and macOS only has ``pbcopy``.
    """

    COMMANDS: ClassVar[dict[str, tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]]] = {
        "darwin": ((("pbcopy",), ("pbpaste",)),),
        "win32": ((("clip",), ("powershell", "-NoProfile", "-Command", "Get-Clipboard")),),
        "linux": (
            (("wl-copy",), ("wl-paste", "--no-newline")),
            (("xclip", "-selection", "clipboard"), ("xclip", "-selection", "clipboard", "-o")),
            (("xsel", "--clipboard", "--input"), ("xsel", "--clipboard", "--output")),
        ),
    }

    def __init__(self, platform: str, runner: CommandRunner = _run_command) -> None:
        self._platform = platform
        self._runner = runner

    def pairs(self) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
        return self.COMMANDS.get(self._platform, ())

    def copy(self, text: str) -> tuple[bool, str]:
        """First working copy command; returns ``(ok, command)``."""
        for copy_argv, _ in self.pairs():
            if shutil.which(copy_argv[0]) is None:
                continue
            if self._runner(copy_argv, text):
                return True, " ".join(copy_argv)
        return False, ""

    def read(self) -> str | None:
        """Output of the first working read command, or ``None``."""
        for _, read_argv in self.pairs():
            if shutil.which(read_argv[0]) is None:
                continue
            try:
                result = subprocess.run(
                    list(read_argv), capture_output=True, text=True, check=False
                )
            except OSError, ValueError:
                continue
            if result.returncode == 0:
                return result.stdout
        return None


class NativeToolBackend:
    """Platform command-line clipboard, wrapped to match the backend protocol."""

    def __init__(self, tools: PlatformCopyTools) -> None:
        self._tools = tools

    @property
    def available(self) -> bool:
        return bool(self._tools.pairs())

    def copy(self, text: str) -> tuple[bool, str]:
        return self._tools.copy(text)

    def read(self) -> str | None:
        return self._tools.read()


class Osc52Backend:
    """The terminal escape sequence, via Textual's own ``copy_to_clipboard``."""

    def __init__(self, copy_to_clipboard: Callable[[str], None]) -> None:
        self._copy = copy_to_clipboard

    @property
    def available(self) -> bool:
        return True  # a terminal that ignores the sequence is not detectable from here

    def copy(self, text: str) -> bool:
        self._copy(text)
        return True

    def read(self) -> str | None:
        return None  # there is no portable way to *read* a clipboard over OSC 52


def osc52_sequence(text: str) -> str:
    """The raw OSC 52 sequence for ``text`` (used by tests and by the fallback path)."""
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\x1b]52;c;{payload}\a"


def default_backends(
    osc52: Callable[[str], None],
    *,
    platform: str | None = None,
    pyperclip_module: PyperclipModule | None = None,
) -> tuple[object, ...]:
    """The backends in chain order: pyperclip, platform tools, then OSC 52."""
    resolved = sys.platform if platform is None else platform
    return (
        PyperclipBackend(pyperclip_module),
        NativeToolBackend(PlatformCopyTools(resolved)),
        Osc52Backend(osc52),
    )


class ClipboardService:
    """Copy/read over the backend chain, plus the outcome text the UI shows."""

    def __init__(self, backends: Sequence[object], *, read_fallback: bool = False) -> None:
        """Args:
        backends: Objects exposing ``copy(text)`` (returning ``bool`` or
            ``(bool, detail)``) and optionally ``read()``, in the order to try them.
        read_fallback: When False, :meth:`read` refuses immediately. This is the
            ``clipboard_read_fallback`` setting (DESIGN §13.2): reading the user's
            clipboard is more invasive than writing to it, so it stays opt-in.
        """
        self._backends = tuple(backends)
        self._read_fallback = read_fallback

    @property
    def backends(self) -> tuple[object, ...]:
        return self._backends

    @property
    def read_fallback(self) -> bool:
        return self._read_fallback

    def copy(self, text: str) -> CopyOutcome:
        """Try each backend in turn and report which one took the text."""
        if not text:
            return CopyOutcome("", None, "nothing to copy")
        tried: list[str] = []
        for backend in self._backends:
            result = backend.copy(text)  # type: ignore[attr-defined]
            ok, detail = result if isinstance(result, tuple) else (bool(result), "")
            if ok:
                return CopyOutcome(text, _backend_kind(backend), detail)
            tried.append(_backend_label(backend))
        reason = (
            f"no clipboard backend accepted the text ({', '.join(tried)})"
            if tried
            else "no clipboard backend is available"
        )
        return CopyOutcome(text, None, reason)

    def read(self) -> str | None:
        """Read the system clipboard, or ``None`` when disabled/unavailable."""
        if not self._read_fallback:
            return None
        for backend in self._backends:
            reader = getattr(backend, "read", None)
            if reader is None:
                continue
            value: str | None = reader()
            if value is not None:
                return value
        return None


def _backend_kind(backend: object) -> ClipboardBackend:
    if isinstance(backend, PyperclipBackend):
        return ClipboardBackend.PYPERCLIP
    if isinstance(backend, Osc52Backend):
        return ClipboardBackend.OSC52
    return ClipboardBackend.PLATFORM


def _backend_label(backend: object) -> str:
    if isinstance(backend, PyperclipBackend):
        return "pyperclip"
    if isinstance(backend, Osc52Backend):
        return "osc52"
    if isinstance(backend, NativeToolBackend):
        return "platform clipboard"
    return type(backend).__name__
