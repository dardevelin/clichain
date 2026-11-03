"""Human-readable output: where it goes, and the explain / report / describe texts."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import IO, TYPE_CHECKING, Any, Final, cast

from clichain.errors import ERROR_DETAIL, error_code, explain_exit
from clichain.steps import Step, step_callable, step_kind

if TYPE_CHECKING:
    from clichain.result import MeterStats, Result

Dest = IO[str] | Callable[[str], None] | None


class _Sentinel:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


DEFAULT: Final = _Sentinel("DEFAULT")  # "whatever set_output() says when emitting"
_STDERR: Final = _Sentinel("STDERR")  # sys.stderr as it is at emit time
Output = Dest | _Sentinel

_output: Dest | _Sentinel = _STDERR
_lock = threading.Lock()  # one line at a time, whichever thread emits it
WIDTH = 72


def set_output(dest: Dest) -> None:
    """Send all library output (checks, peek, meter, reports) to ``dest``.

    A file-like object, a callable receiving each line, or None for silence.
    """
    global _output
    _output = dest


def resolve(out: Output) -> Dest:
    """Pin an output choice to a concrete destination."""
    if out is DEFAULT:
        out = _output
    if out is _STDERR:
        return sys.stderr
    return out  # type: ignore[return-value]


def emitter(out: Output) -> Callable[[str], None]:
    """A thread-safe line writer bound to ``out`` as resolved now."""
    dest = resolve(out)
    if dest is None:
        return lambda _msg: None
    write = getattr(dest, "write", None)
    if write is not None:
        flush = getattr(dest, "flush", None)

        def to_file(msg: str) -> None:
            with _lock:
                write(msg + "\n")
                if flush is not None:
                    flush()

        return to_file

    call: Callable[[str], None] = dest  # type: ignore[assignment]

    def to_callable(msg: str) -> None:
        with _lock:
            call(msg)

    return to_callable


def emit_lines(lines: Iterable[str], out: Output = DEFAULT) -> None:
    emit = emitter(out)
    for line in lines:
        emit(line)


def wrap(prefix: str, text: str, width: int, pad: str) -> list[str]:
    """prefix + text, wrapped at `` | `` boundaries to fit ``width``."""
    if len(text) <= width:
        return [prefix + text]
    lines: list[str] = []
    line, current = prefix, ""
    for part in text.split(" | "):
        candidate = f"{current} | {part}" if current else part
        if current and len(candidate) > width:
            lines.append(line + current)
            line, current = pad, "| " + part
        else:
            current = candidate
    if current:
        lines.append(line + current)
    return lines


# -- explain ------------------------------------------------------------------------


def _source_of(step: Step) -> str:
    """`` at file.py:12`` for a step's callback (from its code object: no file reads)."""
    code = getattr(step_callable(step), "__code__", None)
    if code is None:
        return ""
    return f" at {os.path.basename(code.co_filename)}:{code.co_firstlineno}"


def _step_line(index: int, kind: str, title: str) -> str:
    return f"[{index}] {kind:<10s} {title}"


def trace_line(index: int, step: Step, failed: bool = False) -> str:
    marker = "  <--" if failed else ""
    return f"    {_step_line(index, step_kind(step), step.title())}{_source_of(step)}{marker}"


def _file_lines(data: dict[str, Any]) -> list[str]:
    return [f"  < {f}" for f in data.get("files_read", [])] + [
        f"  > {f}" for f in data.get("files_written", [])
    ]


def explain_lines(result: Result) -> list[str]:
    if result.ok:
        return ["no errors"]
    code = error_code(result.returncode)
    lines = [f"error[{code}]: {explain_exit(result.returncode)}"]
    if result._failed_step is not None and result._steps:
        lines += ["", "  execution trace:"]
        lines += [trace_line(i, s, i == result._failed_step) for i, s in enumerate(result._steps)]
    if result._source_location:
        lines += ["", f"  source: {result._source_location}"]
    if code in ERROR_DETAIL:
        lines += ["", f"  {ERROR_DETAIL[code]}"]
    return lines


# -- report ---------------------------------------------------------------------------


def _fmt_bytes(n: int) -> str:
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def meter_line(stats: MeterStats) -> str:
    rate = _fmt_bytes(int(stats.bytes_per_sec))
    return f"[{stats.label or 'meter'}] {_fmt_bytes(stats.bytes)}  {rate}/s  {stats.lines} lines"


def meter_reporter(
    to: Callable[[MeterStats], None] | IO[str] | None,
    emit: Callable[[str], None],
    emitter_for: Callable[[Output], Callable[[str], None]] = emitter,
) -> Callable[[MeterStats], None]:
    """How a meter reports: a line to the run's output or to a file, or a callback.

    Callbacks get a snapshot: the live statistics keep changing on the meter's thread.
    """
    if to is None:
        return lambda stats: emit(meter_line(stats))
    if hasattr(to, "write"):
        write = emitter_for(cast("IO[str]", to))
        return lambda stats: write(meter_line(stats))
    callback = cast("Callable[[MeterStats], None]", to)
    return lambda stats: callback(replace(stats))


def peek_observer(label: str | None, emit: Callable[[str], None]) -> Callable[[str], None]:
    """The default peek: each line (without trailing whitespace) to the run's output."""
    prefix = f"[{label}] " if label else ""
    return lambda line: emit(prefix + line.rstrip())


def report_lines(result: Result, data: dict[str, Any]) -> list[str]:
    sep, div = "=" * WIDTH, "-" * WIDTH
    gen, env = data["generator"], data["environment"]
    commit = data.get("source_commit")
    status = "OK" if result.ok else f"FAILED  {explain_exit(result.returncode)}"
    out = [
        sep,
        f"  clichain {gen['version']}  |  python {gen['python']}  |  "
        f"{env['os']} {env['os_version']} {env['arch']}",
        f"  {env['hostname']}  {env['cwd']}" + (f"  @ {commit}" if commit else ""),
        f"  {data['timestamp']}",
        f"  {status}  {data['elapsed']}s",
        sep,
        f"  {'tool':<20s} {'version':>10s}   path",
        div,
    ]
    for t in data["tools"]:
        out.append(
            f"  {t['name']:<20s} {t.get('version', '-'):>10s}   {t.get('path', 'not found')}"
        )

    if files := _file_lines(data):
        out += [div, *files]

    if result.profile:
        cols = ["#", "time", "in", "out", "spawn", "bytes"]
        widths = [3, 8, 7, 7, 5, 10]
        head = "  " + "  ".join(f"{c:>{w}s}" for c, w in zip(cols, widths, strict=True))
        prefix_w = len(head) + 2
        out += [div, head + "  command", div]
        for i, p in enumerate(result.profile):
            cells = [f"{i:3d}", f"{p.elapsed:7.3f}s", f"{p.lines_in:7d}", f"{p.lines_out:7d}"]
            cells += [f"{p.spawns:5d}", f"{_fmt_bytes(p.bytes_out):>10s}"]
            out += wrap("  " + "  ".join(cells) + "  ", p.name, WIDTH - prefix_w, " " * prefix_w)
        blanks = "  ".join(" " * w for w in widths[2:])
        out += [div, f"  {'':3s}  {result.elapsed:7.3f}s  {blanks}  total"]

    if result.checks:
        out.append(div)
        out += [f"  [{'ok' if c.ok else '!!'}] {c.name}: {c.found}" for c in result.checks]
    out.append(sep)
    return out


# -- describe ---------------------------------------------------------------------------


def describe_lines(data: dict[str, Any]) -> list[str]:
    """Text form of ``manifest.describe_data()``."""
    sep, div = "=" * WIDTH, "-" * WIDTH
    steps = data["steps"]
    out = [sep, f"  pipeline: {len(steps)} steps", div]
    out += [f"  {_step_line(s['step'], s['type'].removesuffix('Step'), s['name'])}" for s in steps]
    if data["tools"]:
        out += [div, "  requires:"]
        for t in data["tools"]:
            req = f" {t['requires']}" if "requires" in t else ""
            found = t.get("path", "not found")
            out.append(f"    {t['name']}{req:<12s} {found:<30s} {'ok' if t['ok'] else '!!'}")
    if files := _file_lines(data):
        out += [div, *files]
    out.append(sep)
    return out
