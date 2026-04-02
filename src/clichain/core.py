"""Core primitives: Cmd, Pipeline, Result, tool()."""

from __future__ import annotations

import inspect
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import IO

from clichain.checks import (
    CheckResult,
    OnFail,
    _detect_version,
    binary_check,
    run_checks,
)

# -- Profiling ----------------------------------------------------------------


@dataclass
class MeterStats:
    label: str = ""
    bytes: int = 0
    lines: int = 0
    elapsed: float = 0.0
    bytes_per_sec: float = 0.0
    lines_per_sec: float = 0.0


@dataclass
class StepProfile:
    name: str
    elapsed: float = 0.0
    lines_in: int = 0
    lines_out: int = 0
    spawns: int = 0
    bytes_in: int | None = None
    bytes_out: int | None = None


def _count_lines(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return len(stripped.splitlines())


def _step_name(step: Step) -> str:
    if isinstance(step, CmdStep):
        return shlex.join([step.binary, *step.args])
    if isinstance(step, FilterStep):
        return "filter"
    if isinstance(step, EachStep):
        w = f" workers={step.workers}" if step.workers > 1 else ""
        return f"each{w}"
    if isinstance(step, PeekStep):
        return f"peek({step.label})" if step.label else "peek"
    if isinstance(step, CollectStep):
        return "collect"
    if isinstance(step, CaptureStep):
        return f"capture({step.name})"
    if isinstance(step, RedirectStep):
        targets = []
        if step.stdout:
            targets.append(f">{step.stdout}")
        if step.stderr:
            targets.append(f"2>{step.stderr}")
        return f"redirect {' '.join(targets)}"
    if isinstance(step, FeedStep):
        return f"feed ({len(step.data)} bytes)"
    if isinstance(step, FromFileStep):
        return f"< {step.path}"
    if isinstance(step, MeterStep):
        return f"meter({step.label})" if step.label else "meter"
    return "unknown"


def _group_name(group: list[Step]) -> str:
    return " | ".join(_step_name(s) for s in group)


# -- Result -------------------------------------------------------------------


@dataclass
class Result:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    checks: list[CheckResult] = field(default_factory=list)
    profile: list[StepProfile] = field(default_factory=list)
    elapsed: float = 0.0
    _steps: list[Step] = field(default_factory=list, repr=False)
    _timestamp: str = ""
    _failed_step: int | None = field(default=None, repr=False)
    _source_location: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def lines(self) -> list[str]:
        stripped = self.stdout.strip()
        if not stripped:
            return []
        return stripped.splitlines()

    def explain(self) -> None:
        """Print detailed error explanation with execution trace."""
        if self.ok:
            _emit("no errors")
            return

        code = _error_code(self.returncode)
        short = _explain_exit(self.returncode)

        _emit(f"error[{code}]: {short}")

        if self._failed_step is not None and self._steps:
            _emit("")
            _emit("  execution trace:")
            for i, step in enumerate(self._steps):
                is_failed = i == self._failed_step
                line = _step_trace_line(i, step, failed=is_failed)
                _emit(line)

        if self._source_location:
            _emit("")
            _emit(f"  source: {self._source_location}")

        detail = _ERROR_DETAIL.get(code, "")
        if detail:
            _emit("")
            _emit(f"  {detail}")

    def report(self) -> None:
        W = 72
        data = self.sbom()
        gen = data["generator"]
        env = data["environment"]
        status = "OK" if self.ok else f"FAILED  {_explain_exit(self.returncode)}"

        # Header
        sep = "=" * W
        div = "-" * W
        v, py = gen["version"], gen["python"]
        commit = gen.get("commit", "")
        ver = f"{v} ({commit})" if commit else v
        plat = f"{env['os']} {env['os_version']} {env['arch']}"
        _emit(sep)
        _emit(f"  clichain {ver}  |  python {py}  |  {plat}")
        _emit(f"  {env['hostname']}  {env['cwd']}")
        _emit(f"  {data['timestamp']}")
        _emit(f"  {status}  {data['elapsed']}s")
        _emit(sep)

        # Tools
        _emit(f"  {'tool':<20s} {'version':>10s}   path")
        _emit(div)
        for t in data["tools"]:
            ver = t.get("version", "-")
            path = t.get("path", "not found")
            _emit(f"  {t['name']:<20s} {ver:>10s}   {path}")

        # Files
        has_files = "files_read" in data or "files_written" in data
        if has_files:
            _emit(div)
            for f in data.get("files_read", []):
                _emit(f"  < {f}")
            for f in data.get("files_written", []):
                _emit(f"  > {f}")

        # Execution profile
        if self.profile:
            has_bytes = any(p.bytes_in is not None for p in self.profile)
            _emit(div)
            if has_bytes:
                prefix_w = 51
                hdr = "  {:>3s}  {:>8s}  {:>7s}  {:>7s}  {:>5s}  {:>10s}  {}"
                _emit(
                    hdr.format(
                        "#",
                        "time",
                        "in",
                        "out",
                        "spawn",
                        "bytes",
                        "command",
                    )
                )
            else:
                prefix_w = 39
                hdr = "  {:>3s}  {:>8s}  {:>7s}  {:>7s}  {:>5s}  {}"
                _emit(
                    hdr.format(
                        "#",
                        "time",
                        "in",
                        "out",
                        "spawn",
                        "command",
                    )
                )
            _emit(div)

            cmd_w = W - prefix_w
            pad = " " * prefix_w

            for i, p in enumerate(self.profile):
                e = f"{p.elapsed:7.3f}s"
                li = f"{p.lines_in:7d}"
                lo = f"{p.lines_out:7d}"
                sp = f"{p.spawns:5d}"
                if has_bytes:
                    b = _fmt_bytes(p.bytes_in) if p.bytes_in is not None else "-"
                    prefix = f"  {i:3d}  {e}  {li}  {lo}  {sp}  {b:>10s}  "
                else:
                    prefix = f"  {i:3d}  {e}  {li}  {lo}  {sp}  "
                _emit_wrapped(prefix, p.name, cmd_w, pad)

            _emit(div)
            e = f"{self.elapsed:7.3f}s"
            if has_bytes:
                _emit(f"  {'':3s}  {e}  {'':7s}  {'':7s}  {'':5s}  {'':10s}  total")
            else:
                _emit(f"  {'':3s}  {e}  {'':7s}  {'':7s}  {'':5s}  total")

        # Checks
        if self.checks:
            _emit("-" * W)
            for c in self.checks:
                mark = "ok" if c.ok else "!!"
                _emit(f"  [{mark}] {c.name}: {c.found}")

        _emit("=" * W)

    def sbom(self) -> dict:

        # Collect unique tools
        tools: list[dict] = []
        seen: set[str] = set()
        files_read: list[str] = []
        files_written: list[str] = []

        for step in self._steps:
            if isinstance(step, CmdStep) and step.binary not in seen:
                seen.add(step.binary)
                path = shutil.which(step.binary)
                version = _detect_version(step.binary) if path else None
                entry: dict = {"name": step.binary}
                if path:
                    entry["path"] = path
                if version:
                    entry["version"] = version
                tools.append(entry)

            if isinstance(step, FromFileStep):
                files_read.append(step.path)

            if isinstance(step, RedirectStep):
                if step.stdout:
                    files_written.append(step.stdout)
                if step.stderr:
                    files_written.append(step.stderr)

        # Build execution call tree
        call_tree: list[dict] = []
        for i, step in enumerate(self._steps):
            node: dict = {"step": i, "type": type(step).__name__}
            if isinstance(step, CmdStep):
                node["cmd"] = shlex.join([step.binary, *step.args])
                if step.capture:
                    node["capture"] = step.capture
                if step.merge_stderr:
                    node["merge_stderr"] = True
            elif isinstance(step, FilterStep):
                node["fn"] = step.fn.__name__ if hasattr(step.fn, "__name__") else "<lambda>"
            elif isinstance(step, EachStep):
                node["fn"] = step.fn.__name__ if hasattr(step.fn, "__name__") else "<lambda>"
                node["workers"] = step.workers
            elif isinstance(step, PeekStep):
                if step.label:
                    node["label"] = step.label
            elif isinstance(step, CaptureStep):
                node["name"] = step.name
            elif isinstance(step, RedirectStep):
                if step.stdout:
                    node["stdout"] = step.stdout
                if step.stderr:
                    node["stderr"] = step.stderr
                if step.append:
                    node["append"] = True
            elif isinstance(step, FeedStep):
                node["bytes"] = len(step.data)
            elif isinstance(step, FromFileStep):
                node["path"] = step.path
                if step.block_size:
                    node["block_size"] = step.block_size
            elif isinstance(step, MeterStep):
                if step.label:
                    node["label"] = step.label
            call_tree.append(node)

        import platform

        gen: dict = {
            "name": "clichain",
            "version": _version,
            "python": sys.version.split()[0],
        }
        if _git_hash:
            gen["commit"] = _git_hash

        sbom: dict = {
            "generator": gen,
            "environment": {
                "os": platform.system(),
                "os_version": platform.release(),
                "arch": platform.machine(),
                "hostname": platform.node(),
                "cwd": os.getcwd(),
            },
            "timestamp": self._timestamp,
            "elapsed": round(self.elapsed, 4),
            "exit_code": self.returncode,
            "tools": tools,
            "call_tree": call_tree,
        }

        if files_read:
            sbom["files_read"] = files_read
        if files_written:
            sbom["files_written"] = files_written

        return sbom

    def sbom_json(self, indent: int = 2) -> str:
        return json.dumps(self.sbom(), indent=indent)


# -- Output destination -------------------------------------------------------

_SIGNALS: dict[int, tuple[str, str]] = {
    1: ("SIGHUP", "terminal hangup"),
    2: ("SIGINT", "interrupted (ctrl+c)"),
    3: ("SIGQUIT", "quit"),
    4: ("SIGILL", "illegal instruction"),
    6: ("SIGABRT", "aborted"),
    8: ("SIGFPE", "floating point error"),
    9: ("SIGKILL", "killed"),
    11: ("SIGSEGV", "segmentation fault"),
    13: ("SIGPIPE", "broken pipe — downstream closed before upstream finished"),
    14: ("SIGALRM", "timeout (alarm)"),
    15: ("SIGTERM", "terminated"),
}

_EXIT_CODES: dict[int, str] = {
    1: "general error",
    2: "misuse of shell builtin / invalid arguments",
    126: "command found but not executable (permission denied?)",
    127: "command not found",
    128: "invalid exit argument",
    255: "exit status out of range",
}


_ERROR_DETAIL: dict[str, str] = {
    "S13": (
        "This usually means a downstream command (like head or grep -m)\n"
        "  closed its input before the upstream command finished writing.\n"
        "  This is often normal behavior, not a real error."
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
    "X1": (
        "The command returned a general error. Check stderr output above\n"
        "  for details from the command itself."
    ),
    "X2": (
        "The command received invalid arguments. Check the flags and\n"
        "  parameters passed to the command."
    ),
}


def _error_code(returncode: int) -> str:
    if returncode < 0:
        return f"S{abs(returncode)}"
    if returncode > 128:
        return f"S{returncode - 128}"
    return f"X{returncode}"


def _step_trace_line(i: int, step: Step, failed: bool = False) -> str:
    step_type = type(step).__name__.replace("Step", "")
    name = _step_name(step)

    # Try to get source location for callables
    loc = ""
    fn = None
    if isinstance(step, (FilterStep, EachStep)) or (isinstance(step, PeekStep) and step.fn):
        fn = step.fn

    if fn is not None:
        try:
            src_file = inspect.getfile(fn)
            _, src_lineno = inspect.getsourcelines(fn)
            short_file = os.path.basename(src_file)
            loc = f" at {short_file}:{src_lineno}"
        except (TypeError, OSError):
            pass

    marker = "  <--" if failed else ""
    return f"    [{i}] {step_type:<10s} {name}{loc}{marker}"


def _get_caller_location() -> str:
    """Get the source location of the .run() call.
    Walk up the stack until we leave clichain code."""
    try:
        this_file = os.path.abspath(__file__)
        this_dir = os.path.dirname(this_file)
        for frame_info in inspect.stack():
            if not os.path.abspath(frame_info.filename).startswith(this_dir):
                return f"{frame_info.filename}:{frame_info.lineno}"
    except (OSError, IndexError):
        pass
    return ""


def _explain_exit(code: int) -> str:
    """Translate an exit code into a human-readable explanation."""
    if code == 0:
        return "exit 0"

    # Negative codes are signals (Python subprocess convention)
    if code < 0:
        sig = abs(code)
        if sig in _SIGNALS:
            name, desc = _SIGNALS[sig]
            return f"signal {sig} ({name}: {desc})"
        return f"signal {sig}"

    # 128+N convention (shell reports signals this way)
    if code > 128 and (code - 128) in _SIGNALS:
        sig = code - 128
        name, desc = _SIGNALS[sig]
        return f"exit {code} — signal {sig} ({name}: {desc})"

    # Known exit codes
    if code in _EXIT_CODES:
        return f"exit {code} — {_EXIT_CODES[code]}"

    return f"exit {code}"


_output: IO[str] | Callable[[str], None] | None = sys.stderr
_git_hash: str = ""


def _get_version() -> str:
    try:
        from importlib.metadata import version

        return version("cmdchain")
    except Exception:
        return "0.0.0"


_version: str = _get_version()


def _detect_git_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


_git_hash = _detect_git_hash()


def set_output(dest: IO[str] | Callable[[str], None] | None) -> None:
    global _output
    _output = dest


def _emit(msg: str) -> None:
    dest = _output
    if dest is None:
        return
    write = getattr(dest, "write", None)
    if write is not None:
        write(msg + "\n")
        getattr(dest, "flush", lambda: None)()
    elif callable(dest):
        dest(msg)


def _emit_wrapped(prefix: str, text: str, width: int, pad: str) -> None:
    """Emit prefix + text, wrapping text at pipe boundaries to fit width."""
    if len(text) <= width:
        _emit(prefix + text)
        return

    # Split on pipe boundaries for readable wrapping
    parts = text.split(" | ")
    line = prefix
    current = ""

    for part in parts:
        candidate = (current + " | " + part) if current else part
        if current and len(candidate) > width:
            _emit(line + current)
            line = pad
            current = "| " + part
        else:
            current = candidate

    if current:
        _emit(line + current)


# -- Steps -------------------------------------------------------------------


@dataclass
class CmdStep:
    binary: str
    args: list[str]
    capture: str | None = None
    merge_stderr: bool = False
    version: str | None = None
    on_fail: OnFail = "error"
    msg: str = ""

    def resolve_args(self, ctx: dict[str, str]) -> list[str]:
        if not ctx:
            return [self.binary, *self.args]
        return [self.binary, *(a.format_map(ctx) for a in self.args)]

    def as_check(self) -> Callable[[], CheckResult]:
        return binary_check(self.binary, version=self.version, on_fail=self.on_fail, msg=self.msg)


@dataclass
class FilterStep:
    fn: Callable[[str], bool]


@dataclass
class EachStep:
    fn: Callable[[str], Cmd]
    workers: int = 1


@dataclass
class PeekStep:
    label: str | None = None
    fn: Callable[[str], None] | None = None


@dataclass
class CollectStep:
    pass


@dataclass
class CaptureStep:
    name: str


@dataclass
class RedirectStep:
    stdout: str | None = None
    stderr: str | None = None
    append: bool = False


@dataclass
class FeedStep:
    data: str


@dataclass
class FromFileStep:
    path: str
    block_size: int = 0  # 0 = let kernel handle it


@dataclass
class MeterStep:
    label: str | None = None
    to: Callable[[MeterStats], None] | IO[str] | None = None
    interval: float = 1.0
    _stats: MeterStats = field(default_factory=MeterStats)


Step = (
    CmdStep
    | FilterStep
    | EachStep
    | PeekStep
    | CollectStep
    | CaptureStep
    | RedirectStep
    | FeedStep
    | FromFileStep
    | MeterStep
)

StreamableStep = CmdStep | FilterStep | PeekStep | FromFileStep | MeterStep


def _is_streamable(step: Step) -> bool:
    return isinstance(step, (CmdStep, FilterStep, PeekStep, FromFileStep, MeterStep))


# -- Streaming helpers --------------------------------------------------------


def _default_peek_fn(label: str | None) -> Callable[[str], None]:
    def _peek(line: str) -> None:
        if label:
            _emit(f"[{label}] {line.rstrip()}")
        else:
            _emit(line.rstrip())

    return _peek


def _thread_filter(src: IO[str], dest: IO[str], fn: Callable[[str], bool]) -> None:
    try:
        for line in src:
            stripped = line.rstrip("\n")
            if fn(stripped):
                dest.write(line)
                dest.flush()
    finally:
        dest.close()


def _thread_peek(src: IO[str], dest: IO[str], fn: Callable[[str], None]) -> None:
    try:
        for line in src:
            fn(line)
            dest.write(line)
            dest.flush()
    finally:
        dest.close()


def _thread_block_read(
    path: str,
    dest: IO[bytes],
    block_size: int,
) -> None:
    """Read a file in fixed-size chunks and write to a binary pipe."""
    try:
        with open(path, "rb") as src:
            while True:
                chunk = src.read(block_size)
                if not chunk:
                    break
                dest.write(chunk)
                dest.flush()
    finally:
        dest.close()


def _emit_meter_stats(stats: MeterStats, to: Callable[[MeterStats], None] | IO[str] | None) -> None:
    msg = (
        f"[{stats.label or 'meter'}] "
        f"{_fmt_bytes(stats.bytes)}  "
        f"{_fmt_bytes(int(stats.bytes_per_sec))}/s  "
        f"{stats.lines} lines"
    )
    if to is None:
        _emit(msg)
    elif hasattr(to, "write"):
        dest: IO[str] = to  # type: ignore[assignment]
        dest.write(msg + "\n")
        dest.flush()
    elif callable(to):
        to(stats)


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _thread_meter(
    src: IO[str],
    dest: IO[str],
    step: MeterStep,
) -> None:
    stats = step._stats
    stats.label = step.label or ""
    start = time.monotonic()
    last_emit = start

    try:
        for line in src:
            stats.bytes += len(line.encode())
            stats.lines += 1
            dest.write(line)
            dest.flush()

            now = time.monotonic()
            stats.elapsed = now - start
            if step.interval == 0 or (now - last_emit) >= step.interval:
                stats.bytes_per_sec = stats.bytes / stats.elapsed if stats.elapsed > 0 else 0
                stats.lines_per_sec = stats.lines / stats.elapsed if stats.elapsed > 0 else 0
                _emit_meter_stats(stats, step.to)
                last_emit = now

        # Final stats
        stats.elapsed = time.monotonic() - start
        stats.bytes_per_sec = stats.bytes / stats.elapsed if stats.elapsed > 0 else 0
        stats.lines_per_sec = stats.lines / stats.elapsed if stats.elapsed > 0 else 0
    finally:
        dest.close()


# -- Process group ------------------------------------------------------------

_GRACEFUL_TIMEOUT = 5


class ProcessGroup:
    """Tracks child processes and threads for coordinated cleanup.

    On Unix, uses os.setpgrp so children share a process group.
    On Windows, tracks processes manually.
    Installs signal handlers for SIGINT/SIGTERM during execution.
    """

    def __init__(self) -> None:
        self._procs: list[subprocess.Popen[str]] = []
        self._threads: list[threading.Thread] = []
        self._file_handles: list[IO[str]] = []
        self._lock = threading.RLock()
        self._interrupted = False
        self._prev_sigint: object = None
        self._prev_sigterm: object = None

    def __enter__(self) -> ProcessGroup:
        if threading.current_thread() is threading.main_thread():
            self._prev_sigint = signal.getsignal(signal.SIGINT)
            self._prev_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
        return self

    def __exit__(self, *_: object) -> None:
        if threading.current_thread() is threading.main_thread():
            if self._prev_sigint is not None:
                signal.signal(
                    signal.SIGINT,
                    self._prev_sigint,  # type: ignore[arg-type]
                )
            if self._prev_sigterm is not None:
                signal.signal(
                    signal.SIGTERM,
                    self._prev_sigterm,  # type: ignore[arg-type]
                )
        # Clean up anything still running
        if self._procs or self._file_handles:
            self._cleanup()

    @property
    def interrupted(self) -> bool:
        return self._interrupted

    def spawn(
        self,
        args: list[str],
        **kwargs: object,
    ) -> subprocess.Popen[str]:
        proc = subprocess.Popen(args, **kwargs)  # type: ignore[call-overload]
        with self._lock:
            self._procs.append(proc)
        return proc

    def add_thread(self, t: threading.Thread) -> None:
        with self._lock:
            self._threads.append(t)

    def add_file(self, fh: IO[str]) -> None:
        with self._lock:
            self._file_handles.append(fh)

    def _handle_signal(self, signum: int, _frame: object) -> None:
        self._interrupted = True
        self._cleanup()
        # Re-raise so the caller sees it
        sys.exit(128 + signum)

    def _cleanup(self) -> None:
        # Terminate all child processes gracefully
        with self._lock:
            procs = list(self._procs)
            threads = list(self._threads)
            handles = list(self._file_handles)

        for proc in procs:
            if proc.poll() is None:
                proc.terminate()

        # Wait for graceful exit
        deadline = time.monotonic() + _GRACEFUL_TIMEOUT
        for proc in procs:
            remaining = max(0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        # Join threads
        for t in threads:
            t.join(timeout=1)

        # Close file handles
        import contextlib

        for fh in handles:
            with contextlib.suppress(OSError):
                fh.close()

        with self._lock:
            self._procs.clear()
            self._threads.clear()
            self._file_handles.clear()


# -- Pipeline -----------------------------------------------------------------


class Pipeline:
    def __init__(self, steps: list[Step]) -> None:
        self._steps = steps

    # -- Builder methods ------------------------------------------------------

    def pipe(self, cmd: Cmd) -> Pipeline:
        self._steps.append(
            CmdStep(
                cmd._binary,
                list(cmd._args),
                version=cmd._version,
                on_fail=cmd._on_fail,
                msg=cmd._msg,
            )
        )
        return self

    def filter(self, fn: Callable[[str], bool]) -> Pipeline:
        self._steps.append(FilterStep(fn))
        return self

    def each(self, fn: Callable[[str], Cmd], workers: int = 1) -> Pipeline:
        self._steps.append(EachStep(fn, workers=workers))
        return self

    def peek(self, label: str | None = None, fn: Callable[[str], None] | None = None) -> Pipeline:
        self._steps.append(PeekStep(label=label, fn=fn))
        return self

    def meter(
        self,
        label: str | None = None,
        to: Callable[[MeterStats], None] | IO[str] | None = None,
        interval: float = 1.0,
    ) -> Pipeline:
        self._steps.append(MeterStep(label=label, to=to, interval=interval))
        return self

    def collect(self) -> Pipeline:
        self._steps.append(CollectStep())
        return self

    def capture(self, name: str) -> Pipeline:
        self._steps.append(CaptureStep(name))
        return self

    def merge_stderr(self) -> Pipeline:
        for step in reversed(self._steps):
            if isinstance(step, CmdStep):
                step.merge_stderr = True
                break
        return self

    def redirect(
        self,
        stdout: str | None = None,
        stderr: str | None = None,
        append: bool = False,
    ) -> Pipeline:
        self._steps.append(RedirectStep(stdout=stdout, stderr=stderr, append=append))
        return self

    def feed(self, data: str) -> Pipeline:
        self._steps.insert(0, FeedStep(data))
        return self

    def from_file(self, path: str, block_size: int = 0) -> Pipeline:
        self._steps.insert(0, FromFileStep(path, block_size=block_size))
        return self

    # -- Validation -----------------------------------------------------------

    def check(self) -> list[CheckResult]:
        checks = []
        seen: set[str] = set()
        for step in self._steps:
            if not isinstance(step, CmdStep):
                continue
            if step.binary in seen:
                continue
            seen.add(step.binary)
            checks.append(step.as_check())
        results = run_checks(checks)
        for r in results:
            if r.should_report:
                _emit(r.format())
        return results

    # -- Describe -------------------------------------------------------------

    def describe(self) -> None:
        """Show what this pipeline does without executing."""
        W = 72
        sep = "=" * W
        div = "-" * W

        _emit(sep)
        _emit(f"  pipeline: {len(self._steps)} steps")
        _emit(div)

        for i, step in enumerate(self._steps):
            _emit(f"  [{i}] {type(step).__name__.replace('Step', ''):<10s} {_step_name(step)}")

        # Tools
        seen: set[str] = set()
        tools: list[CmdStep] = []
        for step in self._steps:
            if isinstance(step, CmdStep) and step.binary not in seen:
                seen.add(step.binary)
                tools.append(step)

        if tools:
            _emit(div)
            _emit("  requires:")
            checks = run_checks([t.as_check() for t in tools])
            for t, c in zip(tools, checks, strict=True):
                ver = f" {t.version}" if t.version else ""
                status = "ok" if c.ok else "!!"
                _emit(f"    {t.binary}{ver:<12s} {c.found:<30s} {status}")

        # Files
        files_read = [s.path for s in self._steps if isinstance(s, FromFileStep)]
        files_written: list[str] = []
        for s in self._steps:
            if isinstance(s, RedirectStep):
                if s.stdout:
                    files_written.append(s.stdout)
                if s.stderr:
                    files_written.append(s.stderr)

        if files_read or files_written:
            _emit(div)
            for f in files_read:
                _emit(f"  < {f}")
            for f in files_written:
                _emit(f"  > {f}")

        _emit(sep)

    def _describe_dict(self) -> dict:
        """Build a description dict from the step list without executing."""
        import platform

        seen: set[str] = set()
        tools: list[dict] = []
        for step in self._steps:
            if isinstance(step, CmdStep) and step.binary not in seen:
                seen.add(step.binary)
                path = shutil.which(step.binary)
                version = _detect_version(step.binary) if path else None
                entry: dict = {"name": step.binary}
                if step.version:
                    entry["requires"] = step.version
                if path:
                    entry["path"] = path
                if version:
                    entry["version"] = version
                entry["found"] = path is not None
                tools.append(entry)

        files_read = [s.path for s in self._steps if isinstance(s, FromFileStep)]
        files_written: list[str] = []
        for s in self._steps:
            if isinstance(s, RedirectStep):
                if s.stdout:
                    files_written.append(s.stdout)
                if s.stderr:
                    files_written.append(s.stderr)

        steps = []
        for i, step in enumerate(self._steps):
            node: dict = {
                "step": i,
                "type": type(step).__name__,
                "name": _step_name(step),
            }
            steps.append(node)

        desc_gen: dict = {
            "name": "clichain",
            "version": _version,
            "python": sys.version.split()[0],
        }
        if _git_hash:
            desc_gen["commit"] = _git_hash

        result: dict = {
            "generator": desc_gen,
            "environment": {
                "os": platform.system(),
                "arch": platform.machine(),
            },
            "steps": steps,
            "tools": tools,
        }

        if files_read:
            result["files_read"] = files_read
        if files_written:
            result["files_written"] = files_written

        return result

    # -- Execution ------------------------------------------------------------

    def run(
        self,
        validate: bool = True,
        pre: list[Callable[[], CheckResult]] | None = None,
    ) -> Result:
        describe_mode = os.environ.get("CLICHAIN_DESCRIBE")
        if describe_mode == "json":
            # Reuse sbom structure but pre-run (no timing/exit code)
            _emit(json.dumps(self._describe_dict(), indent=2))
            sys.exit(0)
        elif describe_mode:
            self.describe()
            sys.exit(0)

        all_checks: list[CheckResult] = []

        if validate or pre:
            if validate:
                all_checks.extend(self.check())

            if pre:
                extra = run_checks(pre)
                for r in extra:
                    if r.should_report:
                        _emit(r.format())
                all_checks.extend(extra)

            if any(c.should_stop for c in all_checks):
                errors = [c for c in all_checks if c.should_stop]
                return Result(
                    stdout="",
                    stderr="\n".join(c.format() for c in errors),
                    returncode=1,
                    checks=all_checks,
                )

        # Execute pipeline with profiling
        source_location = _get_caller_location()
        groups = self._plan()
        ctx: dict[str, str] = {}
        output = ""
        all_stderr: list[str] = []
        all_profiles: list[StepProfile] = []
        returncode = 0
        failed_step: int | None = None
        timestamp = datetime.now(timezone.utc).isoformat()
        pipeline_start = time.monotonic()

        # Map group steps back to their index in self._steps
        step_offset = 0

        pg = ProcessGroup()
        with pg:
            for group in groups:
                lines_in = _count_lines(output)
                t0 = time.monotonic()

                output, rc, errs, spawns = self._exec_group(
                    group,
                    output,
                    ctx,
                    pg,
                )

                elapsed = time.monotonic() - t0
                lines_out = _count_lines(output)

                # Extract byte counts from any meter in this group
                bytes_in: int | None = None
                bytes_out: int | None = None
                for s in group:
                    if isinstance(s, MeterStep):
                        bytes_in = s._stats.bytes
                        bytes_out = s._stats.bytes

                all_profiles.append(
                    StepProfile(
                        name=_group_name(group),
                        elapsed=elapsed,
                        lines_in=lines_in,
                        lines_out=lines_out,
                        spawns=spawns,
                        bytes_in=bytes_in,
                        bytes_out=bytes_out,
                    )
                )

                all_stderr.extend(errs)
                if rc != 0:
                    returncode = rc
                    failed_step = step_offset + len(group) - 1
                    for j, s in enumerate(group):
                        if isinstance(s, CmdStep):
                            cmd_name = shlex.join([s.binary, *s.args])
                            if any(cmd_name in e for e in errs):
                                failed_step = step_offset + j
                                break
                    break

                step_offset += len(group)

            if pg.interrupted:
                returncode = 130  # standard bash convention for SIGINT

        pipeline_elapsed = time.monotonic() - pipeline_start

        return Result(
            stdout=output,
            stderr="\n".join(all_stderr),
            returncode=returncode,
            checks=all_checks,
            profile=all_profiles,
            elapsed=pipeline_elapsed,
            _steps=list(self._steps),
            _timestamp=timestamp,
            _failed_step=failed_step,
            _source_location=source_location,
        )

    # -- Planning: group streamable steps together ----------------------------

    def _plan(self) -> list[list[Step]]:
        groups: list[list[Step]] = []
        current: list[Step] = []

        for step in self._steps:
            if _is_streamable(step):
                current.append(step)
                continue
            if current:
                groups.append(current)
                current = []
            groups.append([step])

        if current:
            groups.append(current)

        return groups

    def _exec_group(
        self,
        group: list[Step],
        input_data: str,
        ctx: dict[str, str],
        pg: ProcessGroup,
    ) -> tuple[str, int, list[str], int]:
        """Returns (output, returncode, stderr_parts, spawn_count)."""
        first = group[0]

        if _is_streamable(first):
            return self._exec_stream_group(group, input_data, ctx, pg)

        if isinstance(first, EachStep):
            out, rc, errs, spawns = self._exec_each(first, input_data)
            return out, rc, errs, spawns

        if isinstance(first, CollectStep):
            return input_data, 0, [], 0

        if isinstance(first, CaptureStep):
            ctx[first.name] = input_data.strip()
            return input_data, 0, [], 0

        if isinstance(first, RedirectStep):
            return self._exec_redirect(first, input_data), 0, [], 0

        if isinstance(first, FeedStep):
            return first.data, 0, [], 0

        if isinstance(first, FromFileStep):
            with open(first.path) as f:
                return f.read(), 0, [], 0

        return input_data, 0, [], 0

    # -- Streaming execution: Popen + threads ---------------------------------

    def _exec_stream_group(
        self,
        steps: list[Step],
        input_data: str,
        ctx: dict[str, str],
        pg: ProcessGroup,
    ) -> tuple[str, int, list[str], int]:
        if len(steps) == 1 and isinstance(steps[0], CmdStep):
            out, rc, errs = self._exec_single_cmd(steps[0], input_data, ctx, pg)
            return out, rc, errs, 1

        if len(steps) == 1 and isinstance(steps[0], FromFileStep):
            with open(steps[0].path) as f:
                return f.read(), 0, [], 0

        procs: list[subprocess.Popen[str]] = []
        all_stderr: list[str] = []
        captures: list[tuple[int, str]] = []
        prev_stdout: IO[str] | None = None

        for step in steps:
            if isinstance(step, FromFileStep):
                if step.block_size > 0:
                    read_fd, write_fd = os.pipe()
                    try:
                        read_end = os.fdopen(read_fd, "rb")
                        write_end = os.fdopen(write_fd, "wb")
                        t = threading.Thread(
                            target=_thread_block_read,
                            args=(step.path, write_end, step.block_size),
                            daemon=True,
                        )
                        t.start()
                    except Exception:
                        os.close(read_fd)
                        os.close(write_fd)
                        raise
                    pg.add_thread(t)
                    prev_stdout = read_end  # type: ignore[assignment]
                else:
                    fh = open(step.path)  # noqa: SIM115
                    pg.add_file(fh)
                    prev_stdout = fh
                continue

            if isinstance(step, CmdStep):
                args = step.resolve_args(ctx)
                stdin_src = prev_stdout if prev_stdout else subprocess.PIPE
                stderr_dst = subprocess.STDOUT if step.merge_stderr else subprocess.PIPE

                try:
                    proc = pg.spawn(
                        args,
                        stdin=stdin_src,
                        stdout=subprocess.PIPE,
                        stderr=stderr_dst,
                        text=True,
                    )
                except FileNotFoundError:
                    return (
                        "",
                        127,
                        [f"{step.binary}: command not found"],
                        len(procs),
                    )
                procs.append(proc)

                if step.capture:
                    captures.append((len(procs) - 1, step.capture))

                if prev_stdout and prev_stdout is not proc.stdin:
                    prev_stdout.close()

                prev_stdout = proc.stdout

            elif isinstance(step, (FilterStep, PeekStep, MeterStep)):
                read_fd, write_fd = os.pipe()
                try:
                    read_end = os.fdopen(read_fd, "r")
                    write_end = os.fdopen(write_fd, "w")

                    if isinstance(step, FilterStep):
                        t = threading.Thread(
                            target=_thread_filter,
                            args=(prev_stdout, write_end, step.fn),
                            daemon=True,
                        )
                    elif isinstance(step, MeterStep):
                        t = threading.Thread(
                            target=_thread_meter,
                            args=(prev_stdout, write_end, step),
                            daemon=True,
                        )
                    else:
                        peek_fn = step.fn or _default_peek_fn(step.label)
                        t = threading.Thread(
                            target=_thread_peek,
                            args=(prev_stdout, write_end, peek_fn),
                            daemon=True,
                        )

                    t.start()
                except Exception:
                    os.close(read_fd)
                    os.close(write_fd)
                    raise
                pg.add_thread(t)
                prev_stdout = read_end

        first_proc_stdin = procs[0].stdin if procs else None
        if input_data and first_proc_stdin:

            def _feed() -> None:
                try:
                    first_proc_stdin.write(input_data)  # type: ignore[union-attr]
                finally:
                    first_proc_stdin.close()  # type: ignore[union-attr]

            feed_thread = threading.Thread(target=_feed, daemon=True)
            feed_thread.start()
            pg.add_thread(feed_thread)
        elif first_proc_stdin:
            first_proc_stdin.close()

        stdout = prev_stdout.read() if prev_stdout else ""
        if prev_stdout:
            prev_stdout.close()

        # Wait for threads and processes in this group
        for t in pg._threads:
            t.join()

        for proc in procs:
            proc.wait()
            if proc.stderr:
                err = proc.stderr.read()
                if err:
                    all_stderr.append(err)

        for idx, name in captures:
            if idx == len(procs) - 1:
                ctx[name] = stdout.strip()

        for proc in procs:
            if proc.returncode != 0:
                if isinstance(proc.args, list):
                    cmd_name = shlex.join([str(a) for a in proc.args])
                else:
                    cmd_name = str(proc.args)
                all_stderr.append(f"{cmd_name}: {_explain_exit(proc.returncode)}")
                return stdout, proc.returncode, all_stderr, len(procs)

        return stdout, 0, all_stderr, len(procs)

    def _exec_single_cmd(
        self,
        step: CmdStep,
        input_data: str,
        ctx: dict[str, str],
        pg: ProcessGroup,
    ) -> tuple[str, int, list[str]]:
        args = step.resolve_args(ctx)
        stderr_dst = subprocess.STDOUT if step.merge_stderr else subprocess.PIPE
        try:
            proc = pg.spawn(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_dst,
                text=True,
            )
        except FileNotFoundError:
            return "", 127, [f"{step.binary}: command not found"]
        stdout, stderr = proc.communicate(input=input_data or None)
        proc_result_stdout = stdout or ""
        proc_result_stderr = stderr or ""
        errs: list[str] = []
        if proc_result_stderr:
            errs.append(proc_result_stderr)
        if proc.returncode != 0:
            errs.append(f"{shlex.join(args)}: {_explain_exit(proc.returncode)}")
        if step.capture:
            ctx[step.capture] = proc_result_stdout.strip()
        return proc_result_stdout, proc.returncode, errs

    # -- Each -----------------------------------------------------------------

    def _exec_each(self, step: EachStep, input_data: str) -> tuple[str, int, list[str], int]:
        stripped = input_data.strip()
        lines = stripped.splitlines() if stripped else []
        if not lines:
            return "", 0, [], 0

        if step.workers <= 1:
            out, rc, errs = self._exec_each_sequential(step, lines)
            return out, rc, errs, len(lines) if rc == 0 else 0

        out, rc, errs, spawns = self._exec_each_parallel(step, lines)
        return out, rc, errs, spawns

    def _exec_each_sequential(self, step: EachStep, lines: list[str]) -> tuple[str, int, list[str]]:
        parts: list[str] = []
        all_stderr: list[str] = []
        for line in lines:
            r = step.fn(line).run(validate=False)
            if r.stderr:
                all_stderr.append(r.stderr)
            if not r.ok:
                return r.stdout, r.returncode, all_stderr
            parts.append(r.stdout.rstrip("\n"))
        return "\n".join(parts) + "\n", 0, all_stderr

    def _exec_each_parallel(
        self,
        step: EachStep,
        lines: list[str],
    ) -> tuple[str, int, list[str], int]:
        results: list[Result | None] = [None] * len(lines)
        cancel = threading.Event()

        def run_one(idx: int, line: str) -> None:
            if cancel.is_set():
                return
            r = step.fn(line).run(validate=False)
            results[idx] = r
            if not r.ok:
                cancel.set()

        with ThreadPoolExecutor(max_workers=step.workers) as pool:
            futures = [pool.submit(run_one, i, line) for i, line in enumerate(lines)]
            for f in futures:
                f.result()

        all_stderr: list[str] = []
        parts: list[str] = []
        spawns = 0
        for r in results:
            if r is None:
                break
            spawns += 1
            if r.stderr:
                all_stderr.append(r.stderr)
            if not r.ok:
                return r.stdout, r.returncode, all_stderr, spawns
            parts.append(r.stdout.rstrip("\n"))

        return "\n".join(parts) + "\n", 0, all_stderr, spawns

    # -- Redirect -------------------------------------------------------------

    def _exec_redirect(self, step: RedirectStep, output: str) -> str:
        mode = "a" if step.append else "w"
        if step.stdout:
            with open(step.stdout, mode) as f:
                f.write(output)
        if step.stderr:
            with open(step.stderr, mode) as f:
                f.write(output)
        return output


# -- Cmd ----------------------------------------------------------------------


class Cmd:
    def __init__(self, binary: str = "", *args: str) -> None:
        self._binary = binary
        self._args = list(args)
        self._version: str | None = None
        self._on_fail: OnFail = "error"
        self._msg: str = ""

    def __call__(self, *args: str) -> Cmd:
        cmd = Cmd(self._binary, *self._args, *args)
        cmd._version = self._version
        cmd._on_fail = self._on_fail
        cmd._msg = self._msg
        return cmd

    def _as_step(self, capture: str | None = None) -> CmdStep:
        return CmdStep(
            self._binary,
            list(self._args),
            capture=capture,
            version=self._version,
            on_fail=self._on_fail,
            msg=self._msg,
        )

    def _to_pipeline(self, *extra: Step) -> Pipeline:
        return Pipeline([self._as_step(), *extra])

    def pipe(self, cmd: Cmd) -> Pipeline:
        return self._to_pipeline(cmd._as_step())

    def filter(self, fn: Callable[[str], bool]) -> Pipeline:
        return self._to_pipeline(FilterStep(fn))

    def each(self, fn: Callable[[str], Cmd], workers: int = 1) -> Pipeline:
        return self._to_pipeline(EachStep(fn, workers=workers))

    def peek(self, label: str | None = None, fn: Callable[[str], None] | None = None) -> Pipeline:
        return self._to_pipeline(PeekStep(label=label, fn=fn))

    def meter(
        self,
        label: str | None = None,
        to: Callable[[MeterStats], None] | IO[str] | None = None,
        interval: float = 1.0,
    ) -> Pipeline:
        return self._to_pipeline(MeterStep(label=label, to=to, interval=interval))

    def collect(self) -> Pipeline:
        return self._to_pipeline(CollectStep())

    def capture(self, name: str) -> Pipeline:
        return self._to_pipeline(CaptureStep(name))

    def merge_stderr(self) -> Pipeline:
        step = self._as_step()
        step.merge_stderr = True
        return Pipeline([step])

    def redirect(
        self,
        stdout: str | None = None,
        stderr: str | None = None,
        append: bool = False,
    ) -> Pipeline:
        return self._to_pipeline(RedirectStep(stdout=stdout, stderr=stderr, append=append))

    def feed(self, data: str) -> Pipeline:
        return Pipeline([FeedStep(data), self._as_step()])

    def from_file(self, path: str, block_size: int = 0) -> Pipeline:
        return Pipeline([FromFileStep(path, block_size=block_size), self._as_step()])

    def run(
        self,
        validate: bool = True,
        pre: list[Callable[[], CheckResult]] | None = None,
    ) -> Result:
        return Pipeline([self._as_step()]).run(
            validate=validate,
            pre=pre,
        )

    def check(self) -> list[CheckResult]:
        return Pipeline([self._as_step()]).check()

    def __repr__(self) -> str:
        return f"Cmd({shlex.join([self._binary, *self._args])})"


# -- tool() factory -----------------------------------------------------------


def tool(
    name: str,
    version: str | None = None,
    on_fail: OnFail = "error",
    msg: str = "",
) -> Cmd:
    cmd = Cmd(name)
    cmd._version = version
    cmd._on_fail = on_fail
    cmd._msg = msg
    return cmd
