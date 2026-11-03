"""Error codes, their explanations, and the exceptions clichain raises."""

from __future__ import annotations

import re
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clichain.result import Result

TIMEOUT_EXIT = 124  # same convention as coreutils `timeout`
DESCRIBE_ENV = "CLICHAIN_DESCRIBE"

# Plain-language descriptions where they say more than the platform's own strsignal().
SIGNAL_DESCRIPTIONS: dict[int, str] = {
    signal.SIGINT: "interrupted (ctrl+c)",
    signal.SIGKILL: "killed",
    signal.SIGPIPE: "broken pipe — downstream closed before upstream finished",
    signal.SIGTERM: "terminated",
}

EXIT_CODES: dict[int, str] = {
    1: "general error",
    2: "misuse of shell builtin / invalid arguments",
    TIMEOUT_EXIT: "timed out",
    126: "command found but not executable (permission denied?)",
    127: "command not found",
    128: "invalid exit argument",
    255: "exit status out of range",
}

ERROR_DETAIL: dict[str, str] = {
    "S13": (
        "This usually means a downstream command (like head or grep -m)\n"
        "  closed its input before the upstream command finished writing.\n"
        "  clichain ignores this by default; run(ignore_sigpipe=False) reports it."
    ),
    "S9": (
        "The process was forcefully killed. This can happen from an OOM\n"
        "  killer, a timeout, or an explicit kill command."
    ),
    "S2": (
        "The process was interrupted, typically by ctrl+c or a CI runner\n  cancelling the job."
    ),
    "S15": (
        "The process was asked to terminate gracefully. This is the\n"
        "  standard shutdown signal from process managers and CI systems."
    ),
    "S11": (
        "The process crashed due to a memory access violation. This is\n"
        "  a bug in the tool itself, not in your pipeline."
    ),
    "X127": (
        "The binary was not found in any directory listed in $PATH.\n"
        "  Check that the tool is installed and its location is in your PATH."
    ),
    "X126": (
        "The binary was found but could not be executed. Check file\n"
        "  permissions (chmod +x) and that it's a valid executable."
    ),
    "X124": (
        "The pipeline ran longer than run(timeout=...) allowed. Every\n"
        "  process was terminated (SIGTERM, then SIGKILL after a grace period)."
    ),
    "X1": (
        "The command returned a general error. Check stderr output above\n"
        "  for details from the command itself."
    ),
    "X2": (
        "The command received invalid arguments. Check the flags and\n"
        "  parameters passed to the command."
    ),
}

_CODE = re.compile(r"([SX])(\d+)")


def split_returncode(returncode: int) -> tuple[str, int]:
    """``-13`` or ``141`` -> ``("S", 13)``; ``127`` or ``255`` -> ``("X", ...)``.

    Above 128 a shell reports "killed by signal N" as 128+N, but only when N is a signal.
    """
    if returncode < 0:
        return "S", -returncode
    if returncode > 128 and signal_info(returncode - 128) is not None:
        return "S", returncode - 128
    return "X", returncode


def error_code(returncode: int) -> str:
    kind, number = split_returncode(returncode)
    return f"{kind}{number}"


def parse_code(code: str) -> tuple[str, int] | None:
    """Split an error code like ``S13`` or ``x127`` into its kind and number."""
    match = _CODE.fullmatch(code.strip().upper())
    return (match[1], int(match[2])) if match else None


def signal_info(number: int) -> tuple[str, str] | None:
    """``13`` -> ``("SIGPIPE", "broken pipe — ...")``; None for numbers that are no signal."""
    try:
        name = signal.Signals(number).name
    except ValueError:
        return None
    desc = SIGNAL_DESCRIPTIONS.get(number)
    if desc is None:  # the platform's text, without the number macOS appends
        desc = re.sub(r":\s*\d+$", "", signal.strsignal(number) or "").lower()
    return name, desc


def explain_exit(code: int) -> str:
    """Translate an exit code into a one-line explanation."""
    kind, number = split_returncode(code)
    if kind == "X":
        return f"exit {code} — {EXIT_CODES[code]}" if code in EXIT_CODES else f"exit {code}"
    info = signal_info(number)
    if info is None:  # only reachable for negative codes
        return f"signal {number}"
    text = f"signal {number} ({info[0]}: {info[1]})"
    return text if code < 0 else f"exit {code} — {text}"


def explain_code_lines(kind: str, number: int) -> list[str]:
    """The text of ``clichain explain <code>``."""
    if kind == "S":
        info = signal_info(number)
        headline = f"signal {number}" + (f" ({info[0]})" if info else "")
        desc = info[1] if info else "unknown signal"
    else:
        headline, desc = f"exit {number}", EXIT_CODES.get(number, "unknown exit code")
    code = f"{kind}{number}"
    lines = [f"error[{code}]: {headline}", f"  {desc}"]
    if code in ERROR_DETAIL:
        lines += ["", f"  {ERROR_DETAIL[code]}"]
    return lines


class PipelineError(Exception):
    """Raised by ``run(check=True)`` when the pipeline fails. Carries the ``Result``."""

    def __init__(self, result: Result) -> None:
        super().__init__(f"pipeline failed: {explain_exit(result.returncode)}")
        self.result = result


class DescribeExit(SystemExit):
    """Raised by ``run()`` in ``CLICHAIN_DESCRIBE`` mode after printing the description.

    A ``SystemExit`` with code 0, so scripts stop cleanly; hosts can catch it by type.
    """

    def __init__(self) -> None:
        super().__init__(0)
