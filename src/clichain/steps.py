"""Pipeline steps: immutable descriptions of what to run.

Each step knows its own label, manifest node and whether it can stream; the executor
(``execute.py``) is the only place that knows how to run one.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Any, ClassVar

from clichain.checks import DEFAULT_VERSION_ARGS, CheckResult, OnFail, binary_check

if TYPE_CHECKING:
    from clichain.pipeline import Pipeline
    from clichain.result import MeterStats

_PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*)\}")
DEFAULT_ENCODING = "utf-8"
DEFAULT_ERRORS = "surrogateescape"  # decodes any bytes; encoding back restores them


def _fn_name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__name__", "<callable>")


@dataclass(frozen=True)
class CmdStep:
    binary: str
    args: tuple[str, ...] = ()
    merge_stderr: bool = False
    version: str | None = None
    version_args: tuple[str, ...] = DEFAULT_VERSION_ARGS
    ok_codes: tuple[int, ...] = (0,)  # exit statuses that are not failures
    tty: bool = False  # stderr is a pseudo-terminal
    on_fail: OnFail = "error"
    msg: str = ""
    env: tuple[tuple[str, str], ...] = ()
    inherit_env: bool = True
    cwd: str | None = None
    streamable: ClassVar[bool] = True

    def argv(self, ctx: Mapping[str, str]) -> list[str]:
        """The command line, with ``{name}`` replaced for names captured so far.

        Any other braces are literal, so awk programs and ``find -exec {}`` survive.
        """
        if not ctx:
            return [self.binary, *self.args]

        def sub(match: re.Match[str]) -> str:
            return ctx.get(match[1], match[0])

        return [self.binary, *(_PLACEHOLDER.sub(sub, a) for a in self.args)]

    def as_check(self) -> Callable[[], CheckResult]:
        return binary_check(self.binary, self.version, self.on_fail, self.msg, self.version_args)

    def title(self) -> str:
        return shlex.join([self.binary, *self.args])

    def node(self) -> dict[str, Any]:
        node: dict[str, Any] = {"cmd": self.title()}
        if self.merge_stderr:
            node["merge_stderr"] = True
        if self.ok_codes != (0,):
            node["ok_codes"] = list(self.ok_codes)
        if self.tty:
            node["tty"] = True
        if self.cwd:
            node["cwd"] = self.cwd
        return node


@dataclass(frozen=True)
class FilterStep:
    fn: Callable[[str], bool]
    streamable: ClassVar[bool] = True

    def title(self) -> str:
        return "filter"

    def node(self) -> dict[str, Any]:
        return {"fn": _fn_name(self.fn)}


@dataclass(frozen=True)
class PeekStep:
    label: str | None = None
    fn: Callable[[str], None] | None = None
    streamable: ClassVar[bool] = True

    def title(self) -> str:
        return f"peek({self.label})" if self.label else "peek"

    def node(self) -> dict[str, Any]:
        return {"label": self.label} if self.label else {}


@dataclass(frozen=True)
class MeterStep:
    label: str | None = None
    to: Callable[[MeterStats], None] | IO[str] | None = None
    interval: float = 1.0
    streamable: ClassVar[bool] = True

    def title(self) -> str:
        return f"meter({self.label})" if self.label else "meter"

    def node(self) -> dict[str, Any]:
        return {"label": self.label} if self.label else {}


@dataclass(frozen=True)
class FeedStep:
    data: str | bytes
    streamable: ClassVar[bool] = True

    def size(self) -> int:
        """Bytes fed (text counted as UTF-8, the default encoding)."""
        if isinstance(self.data, bytes):
            return len(self.data)
        return len(self.data.encode(DEFAULT_ENCODING, DEFAULT_ERRORS))

    def title(self) -> str:
        return f"feed ({self.size()} bytes)"

    def node(self) -> dict[str, Any]:
        return {"bytes": self.size()}


@dataclass(frozen=True)
class FromFileStep:
    path: str
    block_size: int = 0  # 0 = hand the file descriptor straight to the process
    streamable: ClassVar[bool] = True

    def title(self) -> str:
        return f"< {self.path}"

    def node(self) -> dict[str, Any]:
        node: dict[str, Any] = {"path": self.path}
        if self.block_size:
            node["block_size"] = self.block_size
        return node


@dataclass(frozen=True)
class RedirectStep:
    """Sink: writes stdout and/or the commands' stderr to files (``>``, ``2>``, ``>>``).

    Directly after streaming steps it streams to the file; what it writes is consumed.
    """

    stdout: str | None = None
    stderr: str | None = None
    append: bool = False
    streamable: ClassVar[bool] = False

    def title(self) -> str:
        targets = [f">{self.stdout}"] if self.stdout else []
        if self.stderr:
            targets.append(f"2>{self.stderr}")
        return f"redirect {' '.join(targets)}"

    def node(self) -> dict[str, Any]:
        node: dict[str, Any] = {}
        if self.stdout:
            node["stdout"] = self.stdout
        if self.stderr:
            node["stderr"] = self.stderr
        if self.append:
            node["append"] = True
        return node


@dataclass(frozen=True)
class EachStep:
    fn: Callable[[str], Pipeline]
    workers: int = 1
    streamable: ClassVar[bool] = False

    def title(self) -> str:
        return f"each workers={self.workers}" if self.workers > 1 else "each"

    def node(self) -> dict[str, Any]:
        return {"fn": _fn_name(self.fn), "workers": self.workers}


@dataclass(frozen=True)
class CollectStep:
    streamable: ClassVar[bool] = False

    def title(self) -> str:
        return "collect"

    def node(self) -> dict[str, Any]:
        return {}


@dataclass(frozen=True)
class CaptureStep:
    name: str
    reject_options: bool = False
    streamable: ClassVar[bool] = False

    def title(self) -> str:
        return f"capture({self.name})"

    def node(self) -> dict[str, Any]:
        return {"name": self.name}


Step = (
    CmdStep
    | FilterStep
    | PeekStep
    | MeterStep
    | FeedStep
    | FromFileStep
    | RedirectStep
    | EachStep
    | CollectStep
    | CaptureStep
)
SOURCE_STEPS = (FeedStep, FromFileStep)


def pipeline_title(steps: tuple[Step, ...]) -> str:
    """``echo hi | grep h``: how a run of steps is named in profiles, labels and repr."""
    return " | ".join(s.title() for s in steps)


def step_kind(step: Step) -> str:
    """Short type name for traces: ``CmdStep`` -> ``Cmd``."""
    return type(step).__name__.removesuffix("Step")


def step_callable(step: Step) -> Callable[..., Any] | None:
    """The user callback a step runs, for source locations in traces."""
    return step.fn if isinstance(step, (FilterStep, PeekStep, EachStep)) else None
