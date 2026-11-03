"""Running pipeline steps: plan them into groups, stream each group, buffer between groups.

Resource rules this module keeps:

* Every file descriptor has exactly one owner. The main thread owns what it opens
  until it hands the fd to a child (then closes its copy at once) or to a worker
  thread (which closes it when it finishes). Nothing closes an fd another thread uses.
* Every pipe has a reader that runs concurrently with its writer (stderr included),
  so no process can block forever on a full pipe.
* A worker that stops early closes both ends: downstream sees EOF, upstream gets
  SIGPIPE, and the group drains instead of hanging.
* Blocking pipe I/O polls, so once a stopped run's processes have exited, readers and
  writers give up even if a descendant of theirs still holds the pipe open.
* Memory held per run is bounded except for stdout that reaches Python (``Result``):
  redirected output streams to its file, stderr keeps a bounded tail.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import select
import shlex
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import tty
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import IO, Any

from clichain import render
from clichain.errors import DESCRIBE_ENV, explain_exit
from clichain.live import Console, Job, Sink, console_for
from clichain.process import GRACE, ProcessGroup, Stopped
from clichain.result import MeterStats, StepProfile
from clichain.steps import (
    DEFAULT_ENCODING,
    DEFAULT_ERRORS,
    CaptureStep,
    CmdStep,
    CollectStep,
    EachStep,
    FeedStep,
    FilterStep,
    FromFileStep,
    MeterStep,
    PeekStep,
    RedirectStep,
    Step,
    pipeline_title,
)

CHUNK = 64 * 1024
_POLL_MS = 100  # how often a blocked pipe read/write checks whether to give up
STDERR_LIMIT = 1024 * 1024  # bytes of stderr kept per process (the tail)
_SIGPIPE_CODES = (-signal.SIGPIPE, 128 + signal.SIGPIPE)


_QUIET = console_for(None)


@dataclass(frozen=True)
class RunOptions:
    encoding: str = DEFAULT_ENCODING
    errors: str = DEFAULT_ERRORS
    ignore_sigpipe: bool = True
    emit: Callable[[str], None] = lambda _msg: None  # from console.emitter(output)
    console: Console = _QUIET  # live output; capture shows nothing


@dataclass
class Outcome:
    stdout: str = ""
    returncode: int = 0
    stderr: list[str] = field(default_factory=list)  # command stderr not redirected
    diagnostics: list[str] = field(default_factory=list)  # clichain's own messages
    profile: list[StepProfile] = field(default_factory=list)
    failed_step: int | None = None

    @property
    def spawns(self) -> int:
        return sum(p.spawns for p in self.profile)


@dataclass
class _GroupOutcome:
    output: str = ""
    returncode: int = 0
    failed: int | None = None  # index within the group
    spawns: int = 0
    bytes_in: int | None = None  # None: the bytes handed over by the previous group
    bytes_out: int | None = None  # None: the same bytes passed through


def plan(steps: tuple[Step, ...]) -> list[tuple[Step, ...]]:
    """Group consecutive streamable steps; a redirect right after them is their sink."""
    groups: list[tuple[Step, ...]] = []
    current: list[Step] = []
    for step in steps:
        if step.streamable:
            current.append(step)
        elif isinstance(step, RedirectStep) and current:
            groups.append((*current, step))
            current = []
        else:
            if current:
                groups.append(tuple(current))
                current = []
            groups.append((step,))
    if current:
        groups.append(tuple(current))
    return groups


def _count_lines(text: str) -> int:
    return text.count("\n") + (bool(text) and not text.endswith("\n"))


def execute(steps: tuple[Step, ...], opts: RunOptions, group: ProcessGroup, job: Job) -> Outcome:
    """Run ``steps`` to completion (or until ``group`` is stopped), showing live output
    (if any) under ``job``."""
    run = _Run(opts, group, job)
    outcome = Outcome(stderr=run.stderr, diagnostics=run.diagnostics)
    output, offset, lines, nbytes = "", 0, 0, 0
    groups = plan(steps)
    for position, chunk in enumerate(groups):
        if group.stopped is not None:
            break
        start, lines_in = time.monotonic(), lines
        tee = job.stdout() if position == len(groups) - 1 else None  # the job's final output
        result = run.run_group(chunk, output, tee)
        output, lines = result.output, _count_lines(result.output)
        bytes_in = nbytes if result.bytes_in is None else result.bytes_in
        nbytes = bytes_in if result.bytes_out is None else result.bytes_out
        outcome.profile.append(
            StepProfile(
                name=pipeline_title(chunk),
                elapsed=time.monotonic() - start,
                lines_in=lines_in,
                lines_out=lines,
                spawns=result.spawns,
                bytes_in=bytes_in,
                bytes_out=nbytes,
            )
        )
        if result.returncode != 0:
            outcome.returncode = result.returncode
            failed = len(chunk) - 1 if result.failed is None else result.failed
            outcome.failed_step = offset + failed
            break
        offset += len(chunk)
    outcome.stdout = output
    return outcome


def _child_env(step: CmdStep) -> dict[str, str] | None:
    """The child's environment (None: inherit unchanged). Describe mode never propagates."""
    if step.inherit_env and not step.env and DESCRIBE_ENV not in os.environ:
        return None
    env = dict(os.environ) if step.inherit_env else {}
    env.pop(DESCRIBE_ENV, None)
    env.update(step.env)
    return env


# -- Worker threads -------------------------------------------------------------------


class _Worker(threading.Thread):
    """Runs ``fn`` and then closes the files it owns, whatever happens."""

    def __init__(self, fn: Callable[[], None], owns: tuple[IO[bytes], ...]) -> None:
        super().__init__(daemon=True)
        self._fn = fn
        self.owns = owns
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self._fn()
        except BaseException as exc:  # reported by the main thread after join
            self.error = exc
        finally:
            for f in self.owns:  # output first: downstream sees EOF before upstream EPIPE
                with contextlib.suppress(OSError):
                    f.close()


GiveUp = Callable[[], bool]  # asked while blocked on a pipe: stop waiting now?


def _ready(f: IO[bytes], event: int, give_up: GiveUp) -> bool:
    """Block until ``f`` can be read or written (or its peer is gone); False to give up."""
    poller = select.poll()
    poller.register(f, event)
    while not poller.poll(_POLL_MS):
        if give_up():
            return False
    return True


def _read(src: IO[bytes], give_up: GiveUp) -> bytes:
    """Up to ``CHUNK`` bytes from an unbuffered pipe; b"" at EOF or when giving up."""
    if not _ready(src, select.POLLIN, give_up):
        return b""
    try:
        return src.read(CHUNK) or b""
    except OSError as exc:
        if exc.errno == errno.EIO:  # a pseudo-terminal whose other side closed (Linux)
            return b""
        raise


def _blocks(src: IO[bytes], give_up: GiveUp) -> Iterator[bytes]:
    """Runs of complete lines as soon as they arrive; a partial last line at EOF."""
    pending: list[bytes] = []
    while chunk := _read(src, give_up):
        cut = chunk.rfind(b"\n") + 1
        if not cut:
            pending.append(chunk)
            continue
        if pending:
            pending.append(chunk[:cut])
            yield b"".join(pending)
            pending = []
        else:
            yield chunk[:cut]
        if cut < len(chunk):
            pending.append(chunk[cut:])
    if pending:
        yield b"".join(pending)


def _send(dst: IO[bytes], data: bytes | memoryview, give_up: GiveUp) -> bool:
    """Write all of ``data``; False once the reader has gone away (or when giving up)."""
    view = memoryview(data)
    while view:
        if not _ready(dst, select.POLLOUT, give_up):
            return False
        try:
            written = dst.write(view)
        except BrokenPipeError:
            return False
        view = view[written or 0 :]
    return True


class _Tail:
    """The last ``STDERR_LIMIT`` bytes read from a stream, and how many were dropped."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.dropped = 0

    def drain(self, src: IO[bytes], give_up: GiveUp, forward: Callable[[bytes], None]) -> None:
        while chunk := _read(src, give_up):
            forward(chunk)
            self.data += chunk
            excess = len(self.data) - STDERR_LIMIT
            if excess > 0:
                del self.data[:excess]
                self.dropped += excess

    def text(self, encoding: str, errors: str) -> str:
        body = self.data.decode(encoding, errors)
        if self.dropped:
            return f"[... {self.dropped} bytes of stderr truncated ...]\n{body}"
        return body


# -- One pipeline execution --------------------------------------------------------------


class _Run:
    """State of one pipeline execution; each() lines get their own."""

    def __init__(self, opts: RunOptions, group: ProcessGroup, job: Job) -> None:
        self.opts = opts
        self.group = group
        self.job = job
        self.ctx: dict[str, str] = {}
        self.stderr: list[str] = []
        self.diagnostics: list[str] = []

    def encode(self, text: str) -> bytes:
        return text.encode(self.opts.encoding, self.opts.errors)

    def decode(self, data: bytes) -> str:
        return data.decode(self.opts.encoding, self.opts.errors)

    def fail(self, message: str, code: int = 1, failed: int | None = None) -> _GroupOutcome:
        self.diagnostics.append(message)
        return _GroupOutcome(returncode=code, failed=failed)

    def run_group(
        self, steps: tuple[Step, ...], text: str, tee: Sink | None = None
    ) -> _GroupOutcome:
        """Run one planned group; ``tee`` (if any) shows its output live."""
        returncode = 1  # what the tee reports if the group raises
        try:
            if steps[0].streamable:
                result = _StreamGroup(self, steps, text, tee).run()
            else:
                result = self._run_buffered(steps, text)
                # each() lines already showed their own output: don't show it twice.
                if tee is not None and result.output and not isinstance(steps[0], EachStep):
                    tee.write(self.encode(result.output))
            returncode = result.returncode
            return result
        finally:
            if tee is not None:
                tee.done(returncode)

    def _run_buffered(self, steps: tuple[Step, ...], text: str) -> _GroupOutcome:
        first = steps[0]
        if isinstance(first, EachStep):
            result = self.each(first, text)
            out = result.output
            result.bytes_out = len(out) if out.isascii() else len(self.encode(out))
            return result
        if isinstance(first, CaptureStep):
            value = text.strip()
            if first.reject_options and value.startswith("-"):
                return self.fail(
                    f"capture '{first.name}': value {value[:40]!r} starts with '-' "
                    "and could be read as an option (reject_options=True)"
                )
            self.ctx[first.name] = value
            return _GroupOutcome(text)
        if isinstance(first, RedirectStep):
            return self.redirect_buffered(first, text)
        assert isinstance(first, CollectStep)
        return _GroupOutcome(text)

    def open_sinks(self, step: RedirectStep) -> tuple[IO[bytes] | None, IO[bytes] | None]:
        """Open redirect targets (the same object when both name one path).

        Command stderr collected so far goes to the stderr target first, so a
        redirect writes every command's stderr up to that point.
        """
        mode = "ab" if step.append else "wb"
        out = open(step.stdout, mode) if step.stdout else None  # noqa: SIM115
        try:
            if step.stderr is None:
                err = None
            elif step.stderr == step.stdout:
                err = out
            else:
                err = open(step.stderr, mode)  # noqa: SIM115
            if err is not None and self.stderr:
                err.write(self.encode("".join(self.stderr)))
                err.flush()
                self.stderr.clear()
        except BaseException:
            if out is not None:
                out.close()
            raise
        return out, err

    def redirect_buffered(self, step: RedirectStep, text: str) -> _GroupOutcome:
        try:
            out, err = self.open_sinks(step)
            if err is not None and err is not out:
                err.close()
            if out is None:
                return _GroupOutcome(text)
            with out:
                out.write(self.encode(text))
        except OSError as exc:
            return self.fail(f"{exc.filename or step.stdout}: {exc.strerror}")
        return _GroupOutcome("")  # bytes out: what it wrote, i.e. what came in

    # -- each ---------------------------------------------------------------------

    def each(self, step: EachStep, text: str) -> _GroupOutcome:
        lines = text.split("\n") if text.strip() else []
        if lines and lines[-1] == "":
            lines.pop()
        lines = [line.removesuffix("\r") for line in lines]
        results: list[Outcome | None] = [None] * len(lines)
        cancel = threading.Event()

        def one(index: int) -> None:
            if cancel.is_set() or self.group.stopped is not None:
                return
            child = step.fn(lines[index])
            steps = getattr(child, "steps", None)
            if not isinstance(steps, tuple):
                raise TypeError(
                    f"each() callback must return a Cmd or Pipeline, got {type(child).__name__}"
                )
            # The input line leads: it is what tells the jobs of one each() apart.
            label = f"each[{index + 1}] {lines[index]} → {pipeline_title(steps)}"
            with self.opts.console.job(label) as line_job:
                outcome = execute(steps, self.opts, self.group, line_job)
                line_job.finish(outcome.returncode)
            results[index] = outcome
            if outcome.returncode != 0:
                cancel.set()

        def abort() -> None:  # a callback raised: stop the other lines and their children
            cancel.set()
            self.group.stop("error", 1)

        try:
            if step.workers <= 1:
                for index in range(len(lines)):
                    one(index)
            else:
                with ThreadPoolExecutor(max_workers=step.workers) as pool:
                    futures = [pool.submit(one, i) for i in range(len(lines))]
                    wait(futures, return_when=FIRST_EXCEPTION)
                    for future in futures:  # the first error in input order
                        if future.done() and not future.cancelled() and future.exception():
                            abort()
                            pool.shutdown(cancel_futures=True)
                            future.result()
        except BaseException:
            abort()
            raise

        return self.merge_lines(results)

    def merge_lines(self, results: list[Outcome | None]) -> _GroupOutcome:
        """Combine each() line results in input order, up to the first failure.

        ``None`` marks a line skipped because another line had already failed; with
        workers that can come *before* the failing line, so it is skipped, not a stop.
        """
        parts: list[str] = []
        spawns = 0
        failure = next((r for r in results if r is not None and r.returncode), None)
        for outcome in results:
            if outcome is None:
                continue
            spawns += outcome.spawns
            self.stderr.extend(outcome.stderr)
            self.diagnostics.extend(outcome.diagnostics)
            if outcome.stdout:
                parts.append(outcome.stdout.rstrip("\n"))
            if outcome.returncode:
                break
        output = "\n".join(parts) + "\n" if parts else ""
        return _GroupOutcome(output, failure.returncode if failure else 0, spawns=spawns)


# -- Streaming groups --------------------------------------------------------------------


class _StreamGroup:
    """Processes and bridge threads connected by pipes, run concurrently."""

    def __init__(
        self, run: _Run, steps: tuple[Step, ...], text: str, tee: Sink | None = None
    ) -> None:
        self.run_ = run
        self.tee = tee
        self.steps = steps
        self.text = text
        self.owned: list[IO[bytes]] = []  # main-thread fds not yet handed off
        self.procs: list[tuple[int, subprocess.Popen[bytes]]] = []
        self.workers: list[_Worker] = []
        # each process's stderr: captured tail (for the Result) and live display
        self.stderrs: list[tuple[int, subprocess.Popen[bytes], _Tail, Sink]] = []
        self.final: IO[bytes] | None = None
        self.bytes_in = 0
        self.bytes_out = 0
        # redirected stdout: our own descriptor of the file, and its size before the run
        self.sink: tuple[IO[bytes], int] | None = None
        self.spawned = False  # every process this group will have is running (or failed)
        self.aborting = False

    def _give_up(self) -> bool:
        """Stop waiting on pipes: the group is being torn down and none of its processes
        is alive. Their descendants may hold the pipes open indefinitely."""
        if not (self.aborting or self.run_.group.stopped is not None) or not self.spawned:
            return False
        return all(proc.poll() is not None for _, proc in self.procs)

    # -- ownership helpers

    def _own(self, f: IO[bytes]) -> IO[bytes]:
        self.owned.append(f)
        return f

    def _close(self, f: IO[bytes]) -> None:
        self.owned.remove(f)
        f.close()

    def _start(self, fn: Callable[[], None], *owns: IO[bytes]) -> None:
        worker = _Worker(fn, owns)
        worker.start()
        for f in owns:  # handed off only once the thread is really running
            if f in self.owned:
                self.owned.remove(f)
        self.workers.append(worker)

    def _pipe(self) -> tuple[IO[bytes], IO[bytes]]:
        read_fd, write_fd = os.pipe()
        try:
            read_end = self._own(os.fdopen(read_fd, "rb", buffering=0))
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            raise
        return read_end, self._own(os.fdopen(write_fd, "wb", buffering=0))

    # -- lifecycle

    def run(self) -> _GroupOutcome:
        try:
            failure = self._build()
            self.spawned = True
            output = self._finish() if failure is None else ""
        except BaseException:
            self._abort()
            raise
        finally:
            self._release()
        if failure is not None:
            self._abort()
            return failure
        for worker in self.workers:
            if worker.error is not None:
                self._stderr_done()
                raise worker.error
        return self._status(output)

    def _build(self) -> _GroupOutcome | None:
        body = list(enumerate(self.steps))
        sink = body.pop()[1] if isinstance(self.steps[-1], RedirectStep) else None
        upstream: IO[bytes] | None = None

        first = body[0][1]
        if isinstance(first, FeedStep):
            data = first.data
            upstream = self._feed(data if isinstance(data, bytes) else self.run_.encode(data))
            body.pop(0)
        elif isinstance(first, FromFileStep):
            try:
                upstream = self._source(first)
            except OSError as exc:
                return self.run_.fail(f"{first.path}: {exc.strerror}", failed=0)
            body.pop(0)
        elif self.text:
            upstream = self._feed(self.run_.encode(self.text))

        out_sink = err_sink = None
        if sink is not None:
            assert isinstance(sink, RedirectStep)
            try:
                out_sink, err_sink = self.run_.open_sinks(sink)
            except OSError as exc:
                return self.run_.fail(f"{exc.filename}: {exc.strerror}", failed=len(self.steps) - 1)
            for f in {id(f): f for f in (out_sink, err_sink) if f is not None}.values():
                self._own(f)
            if out_sink is not None and sink.stdout is not None:
                held = self._own(os.fdopen(os.dup(out_sink.fileno()), "rb", buffering=0))
                self.sink = (held, os.fstat(held.fileno()).st_size)

        for position, (index, step) in enumerate(body):
            to_file = out_sink if position == len(body) - 1 else None
            if isinstance(step, CmdStep):
                result = self._spawn(index, step, upstream, to_file, err_sink)
                if isinstance(result, _GroupOutcome):
                    return result
                upstream = result
            else:
                assert isinstance(step, (FilterStep, PeekStep, MeterStep))
                upstream = self._bridge(step, upstream, to_file)
        self.final = upstream
        return None

    def _feed(self, data: bytes) -> IO[bytes]:
        self.bytes_in = len(data)
        read_end, write_end = self._pipe()

        view = memoryview(data)

        def feed() -> None:
            for i in range(0, len(view), CHUNK):
                if not _send(write_end, view[i : i + CHUNK], self._give_up):
                    return

        self._start(feed, write_end)
        return read_end

    def _source(self, step: FromFileStep) -> IO[bytes]:
        src = self._own(open(step.path, "rb"))  # noqa: SIM115
        self.bytes_in = os.fstat(src.fileno()).st_size
        if step.block_size <= 0:
            return src  # the fd goes straight to the process: kernel-speed `< file`
        read_end, write_end = self._pipe()

        def copy() -> None:
            while block := src.read(step.block_size):
                if not _send(write_end, block, self._give_up):
                    return

        self._start(copy, write_end, src)
        return read_end

    def _spawn(
        self,
        index: int,
        step: CmdStep,
        upstream: IO[bytes] | None,
        to_file: IO[bytes] | None,
        err_sink: IO[bytes] | None,
    ) -> IO[bytes] | _GroupOutcome | None:
        stderr, terminal, slave = self._stderr_target(step, err_sink)
        argv = step.argv(self.run_.ctx)
        env = _child_env(step)
        # An environment without PATH would make the lookup fall back to /bin:/usr/bin;
        # resolve on our PATH instead, as the preflight check did.
        needs_lookup = env is not None and "PATH" not in env and os.sep not in step.binary
        try:
            proc = self.run_.group.spawn(
                argv,
                executable=shutil.which(step.binary) if needs_lookup else None,
                stdin=upstream if upstream is not None else subprocess.DEVNULL,
                stdout=to_file if to_file is not None else subprocess.PIPE,
                stderr=stderr,
                env=env,
                cwd=step.cwd,
            )
        except Stopped:
            stopped = self.run_.group.stopped
            return _GroupOutcome(returncode=stopped.returncode if stopped else 1, failed=index)
        except OSError as exc:
            if step.cwd is not None and exc.filename == step.cwd:
                return self.run_.fail(f"{step.cwd}: {exc.strerror}", 1, index)
            if isinstance(exc, FileNotFoundError):
                return self.run_.fail(f"{step.binary}: command not found", 127, index)
            return self.run_.fail(f"{step.binary}: {exc.strerror}", 126, index)
        finally:
            if upstream is not None:
                self._close(upstream)  # the child holds its own copy now
            if slave is not None:
                os.close(slave)  # likewise for the pseudo-terminal

        self.procs.append((index, proc))
        stream = terminal if terminal is not None else proc.stderr
        if stream is not None:
            tail, live = _Tail(), self.run_.job.source(shlex.join(argv))
            self.stderrs.append((index, proc, tail, live))
            self._own(stream)
            self._start(lambda: tail.drain(stream, self._give_up, live.write), stream)
        return self._own(proc.stdout) if proc.stdout is not None else None

    def _stderr_target(
        self, step: CmdStep, err_sink: IO[bytes] | None
    ) -> tuple[Any, IO[bytes] | None, int | None]:
        """Where the child's stderr goes, our end to read it (pty only), and the pty
        end to close once the child holds it."""
        if step.merge_stderr:
            return subprocess.STDOUT, None, None
        if err_sink is not None:
            return err_sink, None, None
        if step.tty:
            terminal, slave = self._pty()
            return slave, terminal, slave
        return subprocess.PIPE, None, None

    def _pty(self) -> tuple[IO[bytes], int]:
        """A raw pseudo-terminal for a child's stderr, sized to the live display."""
        master, slave = os.openpty()
        terminal = self._own(os.fdopen(master, "rb", buffering=0))
        try:
            tty.setraw(slave)  # no echo, no newline translation: bytes arrive unchanged
            width = max(20, self.run_.opts.console.columns() - 2)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
        except BaseException:
            os.close(slave)
            raise
        return terminal, slave

    def _bridge(
        self,
        step: FilterStep | PeekStep | MeterStep,
        upstream: IO[bytes] | None,
        to_file: IO[bytes] | None,
    ) -> IO[bytes] | None:
        if upstream is None:  # nothing flows in: an already-closed pipe reads as EOF
            src, empty = self._pipe()
            self._close(empty)
        else:
            src = upstream
        if to_file is not None:
            dst, downstream = to_file, None
        else:
            downstream, dst = self._pipe()

        if isinstance(step, FilterStep):
            fn = self._filter(step, src, dst)
        elif isinstance(step, PeekStep):
            fn = self._peek(step, src, dst)
        else:
            fn = self._meter(step, src, dst)
        self._start(fn, dst, src)
        return downstream

    def _filter(self, step: FilterStep, src: IO[bytes], dst: IO[bytes]) -> Callable[[], None]:
        keep, decode = step.fn, self.run_.decode

        def loop() -> None:
            for data in _blocks(src, self._give_up):
                raw, texts = data.split(b"\n"), decode(data).split("\n")
                if len(raw) != len(texts):
                    raise ValueError("filter() needs an ASCII-compatible encoding")
                complete = raw[-1] == b""  # False only for a final line without newline
                if complete:
                    raw.pop()
                    texts.pop()
                kept = [i for i, text in enumerate(texts) if keep(text.removesuffix("\r"))]
                if not kept:
                    continue
                out = b"\n".join(raw[i] for i in kept)
                if complete or kept[-1] != len(raw) - 1:
                    out += b"\n"
                if not _send(dst, out, self._give_up):  # kept lines pass through as bytes
                    return

        return loop

    def _peek(self, step: PeekStep, src: IO[bytes], dst: IO[bytes]) -> Callable[[], None]:
        decode = self.run_.decode
        observe = step.fn or render.peek_observer(step.label, self.run_.opts.emit)

        def loop() -> None:
            for data in _blocks(src, self._give_up):
                *lines, last = decode(data).split("\n")
                for line in lines:
                    observe(line + "\n")
                if last:
                    observe(last)
                if not _send(dst, data, self._give_up):
                    return

        return loop

    def _meter(self, step: MeterStep, src: IO[bytes], dst: IO[bytes]) -> Callable[[], None]:
        stats = MeterStats(label=step.label or "")
        opts = self.run_.opts
        report = render.meter_reporter(step.to, opts.emit, opts.console.emitter)
        start = time.monotonic()

        def rates(now: float) -> None:
            stats.elapsed = now - start
            if stats.elapsed > 0:
                stats.bytes_per_sec = stats.bytes / stats.elapsed
                stats.lines_per_sec = stats.lines / stats.elapsed

        def per_line() -> None:  # interval=0: one report for every line
            for data in _blocks(src, self._give_up):
                *lines, last = data.split(b"\n")
                sizes = [len(line) + 1 for line in lines] + ([len(last)] if last else [])
                for size in sizes:
                    stats.bytes += size
                    stats.lines += 1
                    rates(time.monotonic())
                    report(stats)
                if not _send(dst, data, self._give_up):
                    return

        def per_chunk() -> None:  # counts newlines; never splits the data
            last, ends_with_newline = start, True
            while chunk := _read(src, self._give_up):
                stats.bytes += len(chunk)
                stats.lines += chunk.count(b"\n")
                ends_with_newline = chunk.endswith(b"\n")
                now = time.monotonic()
                if now - last >= step.interval:
                    rates(now)
                    report(stats)
                    last = now
                if not _send(dst, chunk, self._give_up):
                    return
            stats.lines += not ends_with_newline  # a final line without newline

        def loop() -> None:
            try:
                (per_line if step.interval == 0 else per_chunk)()
            finally:
                rates(time.monotonic())

        return loop

    def _finish(self) -> str:
        parts: list[bytes] = []
        if self.final is not None:
            while chunk := _read(self.final, self._give_up):
                parts.append(chunk)
                if self.tee is not None:
                    self.tee.write(chunk)
            self._close(self.final)
        for _, proc in self.procs:
            self.run_.group.wait(proc)
        for worker in self.workers:
            worker.join()
        data = b"".join(parts)
        if self.sink is not None:  # stdout went to the file, not to us
            held, before = self.sink
            self.bytes_out = max(0, os.fstat(held.fileno()).st_size - before)
        else:
            self.bytes_out = len(data)
        return self.run_.decode(data)

    def _release(self) -> None:
        """Close what the main thread still owns (never a worker's fds)."""
        for f in list(self.owned):
            self._close(f)

    def _abort(self) -> None:
        """Error path: stop this group's children, then let the workers drain out."""
        self.aborting = self.spawned = True  # workers give up once the children are gone
        self._release()  # first: a worker blocked on one of these pipes gets EPIPE/EOF now
        self.run_.group.reap([proc for _, proc in self.procs])
        deadline = time.monotonic() + GRACE
        for worker in self.workers:
            worker.join(max(0.0, deadline - time.monotonic()))
            # A worker still alive is stuck in user code; it keeps its own fds.
        self._stderr_done()

    def _stderr_done(self) -> None:
        """Every process has exited: close its live display with its status."""
        for index, proc, _, live in self.stderrs:
            rc = proc.returncode if proc.returncode is not None else 1
            live.done(0 if self._accepted(index, rc) else rc)

    def _accepted(self, index: int, rc: int) -> bool:
        """An exit that is not a failure: 0, one of the tool's ok_codes, or SIGPIPE from
        a writer whose reader stopped early (unless ignore_sigpipe is off)."""
        step = self.steps[index]
        assert isinstance(step, CmdStep)
        return rc in step.ok_codes or (self.run_.opts.ignore_sigpipe and rc in _SIGPIPE_CODES)

    def _status(self, output: str) -> _GroupOutcome:
        self._stderr_done()
        for _, _, tail, _ in self.stderrs:
            text = tail.text(self.run_.opts.encoding, self.run_.opts.errors)
            if text:
                self.run_.stderr.append(text)
        failed, code = None, 0
        for index, proc in self.procs:
            rc = proc.returncode
            if self._accepted(index, rc):
                continue
            failed, code = index, rc
            self.run_.diagnostics.append(f"{shlex.join(map(str, proc.args))}: {explain_exit(rc)}")  # type: ignore[arg-type]
            break
        return _GroupOutcome(
            output,
            code,
            failed,
            spawns=len(self.procs),
            bytes_in=self.bytes_in,
            bytes_out=self.bytes_out,
        )
