"""Live output while a pipeline runs: each job's output in its own buffer, shown as
tagged lines, as one group per finished job, or as blocks redrawn in place.

The executor only feeds bytes in (``Console.job() -> Job.source() -> Sink.write()``)
and never knows the mode or the unit. Output never interleaves mid-line: the lines and
grouped consoles write whole lines under one lock, and in blocks mode a single renderer
thread is the only writer of the terminal.
"""

from __future__ import annotations

import codecs
import os
import re
import sys
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Literal, get_args

from clichain import render
from clichain.errors import explain_exit

Mode = Literal["capture", "lines", "grouped", "blocks", "auto"]
MODES: tuple[str, ...] = get_args(Mode)
GROUP_LIMIT = 10_000  # lines kept per job for grouped output (the rest is counted)
_LINE_LIMIT = 16 * 1024  # characters kept of any one line
_LABEL_WIDTH = 48
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass(frozen=True)
class Live:
    """How a run shows its output while it runs (``run(live=...)``).

    mode: "capture" (nothing live), "lines", "grouped", "blocks", or "auto" (blocks on
        a terminal, ``fallback`` elsewhere).
    unit: "job" (a run, or one each() line) or "process" per block / group / prefix.
    tail: lines kept visible per running block.
    stdout: also show the output that reaches Python, not only stderr.
    stream: where to draw (default: sys.stderr at run time).
    columns: width override (default: the terminal's).
    fps: redraws per second in blocks mode.
    """

    mode: Mode = "auto"
    fallback: Literal["lines", "grouped"] = "grouped"
    unit: Literal["job", "process"] = "job"
    tail: int = 5
    stdout: bool = False
    stream: IO[str] | None = None
    columns: int | None = None
    fps: float = 10.0

    def __post_init__(self) -> None:
        problems = [
            f"mode must be one of {MODES}" if self.mode not in MODES else "",
            "fallback must be 'lines' or 'grouped'" if self.fallback not in _FALLBACKS else "",
            "unit must be 'job' or 'process'" if self.unit not in _UNITS else "",
            "tail must be at least 1" if self.tail < 1 else "",
            "fps must be positive" if not self.fps > 0 else "",
            "columns must be at least 10" if self.columns is not None and self.columns < 10 else "",
        ]
        if errors := [p for p in problems if p]:
            raise ValueError(f"invalid live settings: {'; '.join(errors)}")


_FALLBACKS = ("lines", "grouped")
_UNITS = ("job", "process")


# -- Text cleanup -------------------------------------------------------------------------

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Any other escape: ESC, optional intermediates, optional final byte (a lone ESC too),
# never the start of a colour code that was deliberately kept.
_ESC = re.compile(r"\x1b(?!\[[0-9;]*m)[ -/]*[0-~]?")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]")
_CONTROL_ANY = re.compile(r"[\x00-\x1f\x7f]")
_TOKEN = re.compile(r"\x1b\[[0-9;]*m|.", re.S)


def _display(line: str, keep_colour: bool) -> str:
    """What to show for one raw line: the last carriage-return rewrite, capped, with
    everything that could move the cursor removed (colour kept if asked)."""
    line = next((seg for seg in reversed(line.split("\r")) if seg), "")[:_LINE_LIMIT]
    if "\x1b" in line:
        line = _OSC.sub("", line)
        line = _CSI.sub(lambda m: m[0] if keep_colour and m[0].endswith("m") else "", line)
        line = _ESC.sub("", line)
    return _CONTROL.sub("", line)


def _cells(char: str) -> int:
    """Terminal columns a character takes: 2 for wide (CJK, emoji), 0 for combining."""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def _fit(text: str, width: int) -> str:
    """Truncate to ``width`` terminal cells, keeping colour codes balanced."""
    if text.isascii() and "\x1b" not in text:
        return text[:width]
    out: list[str] = []
    used, coloured = 0, False
    for match in _TOKEN.finditer(text):
        token = match[0]
        if token.startswith("\x1b"):
            out.append(token)
            coloured = True
            continue
        cells = _cells(token)
        if used + cells > width:
            break
        out.append(token)
        used += cells
    return "".join(out) + ("\x1b[0m" if coloured else "")


def _short(label: str) -> str:
    """One line, control characters shown escaped (``\\n``), at most ``_LABEL_WIDTH``."""
    label = _CONTROL_ANY.sub(lambda m: repr(m[0])[1:-1], label)
    return label if len(label) <= _LABEL_WIDTH else label[: _LABEL_WIDTH - 1] + "…"


class _Assembler:
    """Bytes -> lines. Decodes incrementally, so a split multi-byte character is fine."""

    def __init__(self, encoding: str, keep_colour: bool) -> None:
        self._decode = codecs.getincrementaldecoder(encoding)("replace").decode
        self._keep_colour = keep_colour
        self.partial = ""  # raw: the line being written right now

    def feed(self, data: bytes, keep: int | None) -> list[str]:
        """Complete lines, cleaned; only the last ``keep`` when the caller shows no more."""
        *complete, partial = (self.partial + self._decode(data)).split("\n")
        if "\r" in partial:  # progress output: only the latest rewrite matters
            partial = "\r".join(partial.split("\r")[-2:])
        self.partial = partial[-_LINE_LIMIT:]
        if keep is not None:
            complete = complete[-keep:]
        return [_display(line, self._keep_colour) for line in complete]

    def flush(self) -> list[str]:
        rest = self.partial + self._decode(b"", final=True)
        self.partial = ""
        line = _display(rest, self._keep_colour)
        return [line] if line else []


# -- Jobs and sinks ---------------------------------------------------------------------------


class Sink:
    """Where output goes for display. This one shows nothing."""

    def write(self, data: bytes) -> None:
        pass

    def done(self, returncode: int) -> None:
        pass


class Job:
    """A unit of work shown together. This one shows nothing (capture mode)."""

    def source(self, label: str) -> Sink:
        """Where one process writes."""
        return _QUIET

    def stdout(self) -> Sink | None:
        """Where the job's final output goes, or None when stdout is not shown."""
        return None

    def finish(self, returncode: int) -> None:
        pass

    def __enter__(self) -> Job:
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        if exc_type is not None:  # an exception ended the job: show it as failed
            self.finish(1)


_QUIET = Sink()
_QUIET_JOB = Job()


class _Block(Job):
    """A job that is shown: a block, a group, or a line prefix."""

    def __init__(self, console: Console, label: str) -> None:
        self.console = console
        self.label = _short(label)
        self.start = time.monotonic()
        self.end: float | None = None
        self.returncode: int | None = None
        self.lines: deque[str] = deque(maxlen=console.keep)
        self.dropped = 0
        self.active: Source | None = None  # its partial line is the one shown (blocks)

    def source(self, label: str) -> Sink:
        return Source(self, owns_job=False)

    def stdout(self) -> Sink | None:
        return Source(self, owns_job=False) if self.console.live.stdout else None

    def finish(self, returncode: int) -> None:
        with self.console.lock:
            if self.returncode is not None:
                return
            self.returncode, self.end = returncode, time.monotonic()
            self.console._finished(self)

    def summary(self) -> str:
        elapsed = (self.end or time.monotonic()) - self.start
        status = "✓" if self.returncode == 0 else "✗"
        code = f"  {explain_exit(self.returncode)}" if self.returncode else ""
        return f"{status} {self.label}{code}  {elapsed:.1f}s"


class _Container(Job):
    """unit="process": not shown itself; each of its processes gets a block of its own."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def source(self, label: str) -> Sink:
        return Source(self.console._block(label), owns_job=True)

    def stdout(self) -> Sink | None:
        return _OnFirstWrite(lambda: self.source("stdout")) if self.console.live.stdout else None


class _OnFirstWrite(Sink):
    """A sink whose block is opened only once something is written to it."""

    def __init__(self, open_sink: Callable[[], Sink]) -> None:
        self._open = open_sink
        self._sink: Sink | None = None

    def write(self, data: bytes) -> None:
        if self._sink is None:
            self._sink = self._open()
        self._sink.write(data)

    def done(self, returncode: int) -> None:
        if self._sink is not None:
            self._sink.done(returncode)


class Source(Sink):
    def __init__(self, job: _Block, owns_job: bool) -> None:
        self._job = job
        self._owns = owns_job
        self._asm = _Assembler(job.console.encoding, job.console.keep_colour)

    @property
    def partial(self) -> str:
        return self._asm.partial

    def write(self, data: bytes) -> None:
        console = self._job.console
        with console.lock:
            console._add(self._job, self._asm.feed(data, console.trim))
            self._job.active = self

    def done(self, returncode: int) -> None:
        """The process exited: flush its last line (and finish its own block, if any)."""
        console = self._job.console
        with console.lock:
            console._add(self._job, self._asm.flush())
            if self._job.active is self:
                self._job.active = None
        if self._owns:
            self._job.finish(returncode)


# -- Consoles ------------------------------------------------------------------------------------


class Console:
    """Base for the live consoles; one per run. Thread-safe."""

    mode = "live"
    keep = 0  # lines a block retains
    trim: int | None = None  # feed only this many lines per write (blocks shows few)

    def __init__(self, live: Live, stream: IO[str], encoding: str) -> None:
        self.live = live
        self.stream = stream
        self.encoding = encoding
        self.keep_colour = _is_terminal(stream)
        self.lock = threading.RLock()
        self._broken = False

    def job(self, label: str) -> Job:
        """A job for a run or an each() line (a container with unit="process")."""
        return _Container(self) if self.live.unit == "process" else self._block(label)

    def _block(self, label: str) -> _Block:
        with self.lock:
            block = _Block(self, label)
            self._started(block)
            return block

    def emitter(self, output: render.Output) -> Callable[[str], None]:
        """How clichain's own messages (checks, peek, meter) reach ``output``."""
        return render.emitter(output)

    def message(self, line: str) -> None:
        with self.lock:
            self._write(line + "\n")

    def columns(self) -> int:
        return self.live.columns or self._size().columns

    def lines(self) -> int:
        return self._size().lines

    def _size(self) -> os.terminal_size:
        """Re-read on every call, so a resized terminal is picked up on the next frame."""
        try:
            return os.get_terminal_size(self.stream.fileno())
        except (AttributeError, OSError, ValueError):
            return os.terminal_size((80, 24))

    def close(self) -> None:
        pass

    def __enter__(self) -> Console:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _write(self, text: str) -> None:
        """Write to the display. If the display fails (closed, broken pipe), live output
        stops; the run itself is never affected."""
        if self._broken:
            return
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            self._broken = True

    def _started(self, job: _Block) -> None:
        pass

    def _add(self, job: _Block, lines: list[str]) -> None:
        pass

    def _finished(self, job: _Block) -> None:
        pass


class CaptureConsole(Console):
    """Nothing is shown live; ``Result.stderr`` still has everything."""

    mode = "capture"

    def job(self, label: str) -> Job:
        return _QUIET_JOB


class LinesConsole(Console):
    """Each complete line as it arrives, tagged ``[job]``; failures add a status line."""

    mode = "lines"

    def _add(self, job: _Block, lines: list[str]) -> None:
        if lines:
            self._write("".join(f"[{job.label}] {line}\n" for line in lines))

    def _finished(self, job: _Block) -> None:
        if job.returncode:
            self._write(f"[{job.label}] {job.summary()}\n")


class GroupedConsole(Console):
    """A job's whole output in one piece when it finishes (``::group::`` on GitHub)."""

    mode = "grouped"
    keep = GROUP_LIMIT

    def __init__(self, live: Live, stream: IO[str], encoding: str) -> None:
        super().__init__(live, stream, encoding)
        self._github = os.environ.get("GITHUB_ACTIONS") == "true"

    def _add(self, job: _Block, lines: list[str]) -> None:
        job.dropped += max(0, len(job.lines) + len(lines) - GROUP_LIMIT)
        job.lines.extend(lines)

    def _finished(self, job: _Block) -> None:
        body = list(job.lines)
        if job.dropped:
            body.insert(0, f"[... {job.dropped} earlier lines not shown ...]")
        if self._github:
            text = [f"::group::{job.summary()}", *body, "::endgroup::"]
        else:
            text = [job.summary(), *(f"│ {line}" for line in body)]
        job.lines.clear()
        self._write("".join(line + "\n" for line in text))


@dataclass
class _Snapshot:
    """What one frame shows, copied under the lock and formatted outside it."""

    blocks: list[tuple[str, float, list[str], str]]  # label, start, tail, raw partial
    done: int
    failed: int


class BlocksConsole(Console):
    """One region per running job, redrawn in place; finished jobs collapse to a line.

    The renderer thread is the only writer of the stream. Everything else (sources,
    finished jobs, messages) only updates the model under the lock.
    """

    mode = "blocks"

    def __init__(self, live: Live, stream: IO[str], encoding: str) -> None:
        self.keep = self.trim = max(1, live.tail)
        super().__init__(live, stream, encoding)
        self.keep_colour = True
        self._running: dict[int, _Block] = {}
        self._commits: list[str] = []  # lines to print above the live area
        self._done = self._failed = 0
        self._drawn: list[str] = []  # the live area currently on screen
        self._stop = threading.Event()
        self._renderer = threading.Thread(target=self._loop, name="clichain-live", daemon=True)
        self._renderer.start()

    def emitter(self, output: render.Output) -> Callable[[str], None]:
        """Messages bound for a terminal go above the live area instead of across it."""
        dest = render.resolve(output)
        if dest is self.stream or (hasattr(dest, "isatty") and _is_terminal(dest)):  # type: ignore[arg-type]
            return self.message
        return render.emitter(output)

    def message(self, line: str) -> None:
        with self.lock:
            self._commits.append(line)

    def _started(self, job: _Block) -> None:
        self._running[id(job)] = job

    def _add(self, job: _Block, lines: list[str]) -> None:
        job.lines.extend(lines)

    def _finished(self, job: _Block) -> None:
        self._running.pop(id(job), None)
        self._done += 1
        self._commits.append(job.summary())
        if job.returncode:
            self._failed += 1
            self._commits += [f"│ {line}" for line in job.lines]

    def close(self) -> None:
        """Stop the renderer and leave only the committed lines on screen (idempotent)."""
        if self._stop.is_set():
            return
        self._stop.set()
        self._renderer.join()
        self._render(final=True)

    def _loop(self) -> None:
        while not self._stop.wait(1 / self.live.fps):
            self._render()

    def _snapshot(self) -> _Snapshot:
        blocks = [
            (job.label, job.start, list(job.lines), job.active.partial if job.active else "")
            for job in self._running.values()
        ]
        return _Snapshot(blocks, self._done, self._failed)

    def _frame(self, snap: _Snapshot, width: int, height: int) -> list[str]:
        rows: list[str] = []
        now = time.monotonic()
        spin = _SPINNER[int(now * 10) % len(_SPINNER)]
        # Every row ends in a newline, so the frame must stay below the screen height or
        # the terminal scrolls and the next redraw cannot reach its top. Reserve one row
        # for "+N more running" and one for the totals.
        budget = max(2, height - 3)
        for shown, (label, start, lines, partial) in enumerate(snap.blocks):
            current = _display(partial, True)
            tail = [*lines, current] if current else lines
            block = [f"{spin} {label}  {now - start:.1f}s", *(f"│ {t}" for t in tail[-self.keep :])]
            if len(rows) + len(block) > budget:
                rows.append(f"+{len(snap.blocks) - shown} more running")
                break
            rows += [_fit(row, width) for row in block]
        if snap.done + len(snap.blocks) > 1:
            rows.append(f"{snap.done} done · {len(snap.blocks)} running · {snap.failed} failed")
        return rows

    def _render(self, final: bool = False) -> None:
        with self.lock:
            commits, self._commits = self._commits, []
            snap = None if final else self._snapshot()
        width = max(10, self.columns() - 1)
        frame = [] if snap is None else self._frame(snap, width, self.lines())
        if not commits and frame == self._drawn:
            return  # nothing changed: no redraw
        parts = [f"\x1b[{len(self._drawn)}F\x1b[J"] if self._drawn else []
        parts += [_fit(line, width) + "\n" for line in commits]
        parts += [line + "\n" for line in frame]
        self._write("".join(parts))
        self._drawn = frame


def _is_terminal(stream: IO[str]) -> bool:
    try:
        return stream.isatty() and os.environ.get("TERM", "") != "dumb"
    except (AttributeError, ValueError):
        return False


_CONSOLES: dict[str, type[Console]] = {
    "capture": CaptureConsole,
    "lines": LinesConsole,
    "grouped": GroupedConsole,
    "blocks": BlocksConsole,
}


def console_for(live: Live | str | None, encoding: str = "utf-8") -> Console:
    """The console for ``run(live=...)``: None means capture."""
    if live is None:
        live = Live("capture")
    elif isinstance(live, str):
        live = Live(live)  # type: ignore[arg-type]
    stream = live.stream or sys.stderr
    mode = live.mode
    if mode == "auto":
        mode = "blocks" if _is_terminal(stream) else live.fallback
    return _CONSOLES[mode](live, stream, encoding)
