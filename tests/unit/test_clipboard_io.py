"""The clipboard backend chain: native first, OSC 52 as the SSH-proof fallback (FR-4.3).

Two facts make this worth testing on its own:

* **copying has to work over SSH**, which the platform tools cannot do — so the chain must
  always reach the OSC 52 backend, and the outcome must say which mechanism took the text
  rather than pretending every copy worked;
* **reading is opt-in** (``clipboard_read_fallback``): pulling from the user's clipboard is
  more invasive than pushing to it, and OSC 52 cannot read at all.

Every backend takes an injected command runner, so nothing here spawns a process, opens a
display or needs ``pyperclip`` installed.
"""

from collections.abc import Sequence

import pytest

from sql_table_swiss_knife.infra.clipboard import (
    ClipboardBackend,
    ClipboardService,
    CommandRunner,
    NativeToolBackend,
    Osc52Backend,
    PlatformCopyTools,
    PyperclipBackend,
    default_backends,
    osc52_sequence,
)


class FakeModule:
    """A stand-in for ``pyperclip`` that records what it was asked to do."""

    def __init__(self, *, works: bool = True, clipboard: str = "from the clipboard") -> None:
        self.copied: list[str] = []
        self._works = works
        self._clipboard = clipboard

    def determine_clipboard(self) -> str:
        return "xclip" if self._works else ""

    def copy(self, text: str) -> None:
        if not self._works:
            raise RuntimeError("no mechanism")
        self.copied.append(text)

    def paste(self) -> str:
        return self._clipboard


class RecordingOsc52:
    def __init__(self) -> None:
        self.copied: list[str] = []

    def __call__(self, text: str) -> None:
        self.copied.append(text)


def tools(runner: CommandRunner, platform: str = "linux") -> NativeToolBackend:
    """A platform backend whose commands never touch the filesystem."""
    return NativeToolBackend(PlatformCopyTools(platform, runner=runner))


# -- the OSC 52 sequence ----------------------------------------------------


def test_the_osc52_sequence_is_the_documented_escape() -> None:
    assert osc52_sequence("hi") == "\x1b]52;c;aGk=\a"


def test_osc52_encodes_utf8_as_base64() -> None:
    assert osc52_sequence("é") == "\x1b]52;c;w6k=\a"


# -- the chain --------------------------------------------------------------


def test_pyperclip_is_preferred_when_it_works() -> None:
    """A system clipboard is where the user will look for the text next."""
    module = FakeModule()
    osc52 = RecordingOsc52()
    service = ClipboardService((PyperclipBackend(module), Osc52Backend(osc52)))
    outcome = service.copy("DE\tGermany")
    assert outcome.ok
    assert outcome.backend is ClipboardBackend.PYPERCLIP
    assert module.copied == ["DE\tGermany"]
    assert osc52.copied == []  # not needed


def test_a_broken_pyperclip_falls_through_to_the_next_backend() -> None:
    osc52 = RecordingOsc52()
    service = ClipboardService(
        (
            PyperclipBackend(FakeModule(works=False)),
            tools(lambda argv, stdin: False),
            Osc52Backend(osc52),
        )
    )
    outcome = service.copy("x")
    assert outcome.backend is ClipboardBackend.OSC52
    assert osc52.copied == ["x"]


def test_a_terminal_session_with_no_native_tool_still_copies_over_osc52() -> None:
    """The SSH case: no display server, no clipboard command, copy must still work."""
    osc52 = RecordingOsc52()
    service = ClipboardService((tools(lambda argv, stdin: False), Osc52Backend(osc52)))
    assert service.copy("hello").describe() == "copied via OSC 52 (the terminal's clipboard)"


def test_a_platform_tool_reports_which_command_took_the_text() -> None:
    """The status line names the command, and the text reaches it on stdin."""
    seen: list[tuple[tuple[str, ...], str | None]] = []

    def runner(argv: Sequence[str], stdin: str | None) -> bool:
        seen.append((tuple(argv), stdin))
        return True

    outcome = ClipboardService((tools(runner),)).copy("DE\tGermany")
    assert outcome.backend is ClipboardBackend.PLATFORM
    assert outcome.detail == " ".join(seen[0][0])
    assert seen == [(seen[0][0], "DE\tGermany")]


def test_an_empty_copy_says_so_instead_of_pretending() -> None:
    osc52 = RecordingOsc52()
    outcome = ClipboardService((Osc52Backend(osc52),)).copy("")
    assert not outcome.ok
    assert outcome.detail == "nothing to copy"
    assert osc52.copied == []


def test_a_chain_with_nothing_working_reports_why() -> None:
    service = ClipboardService((PyperclipBackend(None), tools(lambda argv, stdin: False)))
    outcome = service.copy("x")
    assert not outcome.ok
    assert "no clipboard backend accepted the text" in outcome.detail
    assert "pyperclip" in outcome.detail


# -- platform tools ---------------------------------------------------------


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_every_supported_platform_offers_at_least_one_copy_command(platform: str) -> None:
    pairs = PlatformCopyTools(platform).pairs()
    assert pairs and all(copy_argv and read_argv for copy_argv, read_argv in pairs)


def test_an_unknown_platform_offers_nothing_rather_than_guessing() -> None:
    assert PlatformCopyTools("plan9").pairs() == ()
    assert tools(lambda argv, stdin: True, platform="plan9").copy("x") == (False, "")


def test_the_second_tool_is_used_when_the_first_is_missing() -> None:
    """A Wayland session has wl-copy; an X11 one inside WSL has xclip, not the other."""
    attempted: list[str] = []

    def runner(argv: Sequence[str], stdin: str | None) -> bool:
        attempted.append(argv[0])
        return argv[0] == "xclip"

    ok, command = tools(runner).copy("x")
    assert ok
    assert command.startswith("xclip")
    assert attempted  # it walked the list rather than giving up on the first entry


# -- reading ----------------------------------------------------------------


def test_reading_is_refused_unless_the_setting_says_so() -> None:
    """Reading someone's clipboard is opt-in (DESIGN §13.2)."""
    service = ClipboardService((PyperclipBackend(FakeModule()),), read_fallback=False)
    assert service.read() is None


def test_reading_returns_the_system_clipboard_when_enabled() -> None:
    service = ClipboardService(
        (PyperclipBackend(FakeModule(clipboard="DE\tGermany")),), read_fallback=True
    )
    assert service.read() == "DE\tGermany"


def test_osc52_cannot_read_so_a_chain_without_native_backends_returns_nothing() -> None:
    service = ClipboardService((Osc52Backend(RecordingOsc52()),), read_fallback=True)
    assert service.read() is None


# -- the default chain ------------------------------------------------------


def test_the_default_chain_is_native_first_and_osc52_last() -> None:
    backends = default_backends(RecordingOsc52(), platform="linux", pyperclip_module=None)
    assert isinstance(backends[0], PyperclipBackend)
    assert isinstance(backends[1], NativeToolBackend)
    assert isinstance(backends[2], Osc52Backend)


def test_a_missing_pyperclip_extra_is_not_an_error() -> None:
    """The optional extra is normally absent; the backend must simply be inert."""
    backend = PyperclipBackend(None)
    assert not backend.available
    assert backend.copy("x") is False
    assert backend.read() is None
