"""The public builder: tool(), Cmd and Pipeline. Pipelines are immutable values."""

from __future__ import annotations

import functools
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import IO, get_args

from clichain import render
from clichain.checks import (
    DEFAULT_VERSION_ARGS,
    CheckResult,
    OnFail,
    parse_constraint,
)
from clichain.errors import DESCRIBE_ENV, TIMEOUT_EXIT, DescribeExit, PipelineError
from clichain.execute import Outcome, RunOptions, execute
from clichain.live import Live, console_for
from clichain.manifest import describe_data, unique_tools
from clichain.process import ProcessGroup, SignalGuard
from clichain.result import DEFAULT_ENCODING, DEFAULT_ERRORS, MeterStats, Result
from clichain.steps import (
    SOURCE_STEPS,
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

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep
_DESCRIBE_TEXT = {"1", "true", "yes", "describe", "text"}

# Set by `clichain check`: run() records the pipeline instead of executing it. A process
# global on purpose: threads the checked script starts must not execute anything either.
_dry_run: list[Pipeline] | None = None
_dry_run_lock = threading.Lock()


@contextmanager
def dry_run() -> Iterator[list[Pipeline]]:
    """Within this block, in every thread, run() records pipelines instead of executing."""
    global _dry_run
    recorded: list[Pipeline] = []
    _dry_run = recorded
    try:
        yield recorded
    finally:
        _dry_run = None


def _describe_mode() -> str | None:
    value = os.environ.get(DESCRIBE_ENV, "").strip().lower()
    if value == "json":
        return "json"
    return "text" if value in _DESCRIBE_TEXT else None


def _caller_location() -> str:
    """File and line of the first frame outside clichain (cheap: no source loading)."""
    frame = sys._getframe(1)
    while frame is not None:
        filename = frame.f_code.co_filename
        if not os.path.abspath(filename).startswith(_PACKAGE_DIR):
            return f"{filename}:{frame.f_lineno}"
        frame = frame.f_back
    return ""


class Pipeline:
    """An immutable sequence of steps. Every builder method returns a new Pipeline,
    so pipelines can be shared, branched and run from several threads at once."""

    __slots__ = ("_steps",)

    def __init__(self, steps: Iterable[Step]) -> None:
        self._steps = tuple(steps)

    @property
    def steps(self) -> tuple[Step, ...]:
        return self._steps

    def _then(self, step: Step) -> Pipeline:
        return Pipeline((*self._steps, step))

    def _source(self, step: FeedStep | FromFileStep) -> Pipeline:
        if isinstance(self._steps[0], SOURCE_STEPS):
            raise ValueError("pipeline already has an input source (feed or from_file)")
        return Pipeline((step, *self._steps))

    # -- Builder ------------------------------------------------------------------

    def pipe(self, other: Pipeline) -> Pipeline:
        """Connect this pipeline's stdout to ``other``'s stdin (a Cmd or a Pipeline)."""
        if any(isinstance(s, SOURCE_STEPS) for s in other.steps):
            raise ValueError("cannot pipe into a pipeline that has its own input source")
        return Pipeline((*self._steps, *other.steps))

    def filter(self, fn: Callable[[str], bool]) -> Pipeline:
        """Keep lines where ``fn(line)`` is true; ``line`` has no line terminator."""
        return self._then(FilterStep(fn))

    def each(self, fn: Callable[[str], Pipeline], workers: int = 1) -> Pipeline:
        """Run the pipeline ``fn(line)`` for every input line; stops at the first failure."""
        if workers < 1:
            raise ValueError("workers must be at least 1")
        return self._then(EachStep(fn, workers))

    def peek(self, label: str | None = None, fn: Callable[[str], None] | None = None) -> Pipeline:
        """Observe each line (with its terminator) without changing the data."""
        return self._then(PeekStep(label, fn))

    def meter(
        self,
        label: str | None = None,
        to: Callable[[MeterStats], None] | IO[str] | None = None,
        interval: float = 1.0,
    ) -> Pipeline:
        """Count bytes and lines flowing through; report every ``interval`` seconds."""
        return self._then(MeterStep(label, to, interval))

    def collect(self) -> Pipeline:
        return self._then(CollectStep())

    def capture(self, name: str, *, reject_options: bool = False) -> Pipeline:
        """Bind the output (stripped) to ``{name}`` in later arguments.

        ``reject_options=True`` fails the run if the value starts with ``-``, so it
        can never be parsed as an option by the command it is passed to.
        """
        if not name.isidentifier():
            raise ValueError(f"capture name must be an identifier, got {name!r}")
        return self._then(CaptureStep(name, reject_options))

    def merge_stderr(self) -> Pipeline:
        """Send the last command's stderr into its stdout (``2>&1``)."""
        for i in range(len(self._steps) - 1, -1, -1):
            step = self._steps[i]
            if isinstance(step, CmdStep):
                if step.tty:
                    raise ValueError("merge_stderr() cannot be combined with tool(..., tty=True)")
                merged = replace(step, merge_stderr=True)
                return Pipeline((*self._steps[:i], merged, *self._steps[i + 1 :]))
        raise ValueError("merge_stderr() needs a command")  # pragma: no cover

    def redirect(
        self, stdout: str | None = None, stderr: str | None = None, append: bool = False
    ) -> Pipeline:
        """Write stdout (``>``) and/or the commands' stderr (``2>``) to files.

        What is redirected is consumed: it no longer appears in the Result.
        """
        if stdout is None and stderr is None:
            raise ValueError("redirect() needs stdout and/or stderr")
        return self._then(RedirectStep(stdout, stderr, append))

    def feed(self, data: str | bytes) -> Pipeline:
        """Use ``data`` as the pipeline's stdin (text is encoded with the run's encoding)."""
        return self._source(FeedStep(data))

    def from_file(self, path: str | os.PathLike[str], block_size: int = 0) -> Pipeline:
        """Use a file as the pipeline's stdin (``< path``)."""
        return self._source(FromFileStep(os.fspath(path), block_size))

    # -- Inspection ----------------------------------------------------------------

    def check(self) -> list[CheckResult]:
        """Verify every binary exists (and meets its version constraint)."""
        return self._preflight(True, None, render.emitter(render.DEFAULT))

    def describe(self) -> None:
        """Print the steps and requirements without executing anything."""
        render.emit_lines(render.describe_lines(describe_data(self._steps)))

    # -- Execution --------------------------------------------------------------------

    def run(
        self,
        validate: bool = True,
        pre: list[Callable[[], CheckResult]] | None = None,
        *,
        timeout: float | None = None,
        check: bool = False,
        encoding: str = DEFAULT_ENCODING,
        errors: str = DEFAULT_ERRORS,
        ignore_sigpipe: bool = True,
        output: render.Output = render.DEFAULT,
        live: Live | str | None = None,
    ) -> Result:
        """Execute the pipeline and return its Result.

        Commands that fail produce a failed Result; exceptions raised by your own
        callbacks (filter, peek, meter, each) propagate after every child is cleaned up.

        timeout: seconds before every process is stopped (returncode 124).
        check: raise PipelineError instead of returning a failed Result.
        encoding/errors: how bytes become text; the default round-trips any bytes.
        ignore_sigpipe: treat SIGPIPE from a writer whose reader exited early as success.
        output: where checks/peek/meter output goes (default: set_output()).
        live: show output while running: "lines", "grouped", "blocks", "auto", or a
            Live(...) for unit, tail, stdout and stream (default: nothing live).
        """
        recorder = _dry_run
        if recorder is not None:
            with _dry_run_lock:
                recorder.append(self)
            return Result(_steps=self._steps)
        mode = _describe_mode()
        if mode is not None:
            self._describe_and_exit(mode, output)

        group = ProcessGroup()
        try:
            # The console closes (screen restored) before the guard re-delivers a signal.
            with SignalGuard(group) as guard, console_for(live, encoding) as console:
                emit = console.emitter(output)
                checks = self._preflight(validate, pre, emit)
                make_result = functools.partial(
                    Result,
                    checks=checks,
                    _steps=self._steps,
                    _source_location=_caller_location(),
                    _encoding=encoding,
                    _errors=errors,
                    _output=output,
                )
                stopping = [c for c in checks if c.should_stop]
                if stopping:
                    stderr = "\n".join(c.format() for c in stopping)
                    result = make_result(stderr=stderr, returncode=1)
                else:
                    timestamp = datetime.now(UTC).isoformat()
                    start = time.monotonic()
                    opts = RunOptions(encoding, errors, ignore_sigpipe, emit, console)
                    outcome = self._execute(opts, timeout, group)
                    result = make_result(
                        stdout=outcome.stdout,
                        stderr="\n".join(
                            p.rstrip("\n") for p in (*outcome.stderr, *outcome.diagnostics)
                        ),
                        returncode=outcome.returncode,
                        profile=outcome.profile,
                        elapsed=time.monotonic() - start,
                        _timestamp=timestamp,
                        _failed_step=outcome.failed_step if outcome.returncode else None,
                    )
        finally:
            group.close()
        guard.deliver()  # handlers restored: the signal now does what it would have done
        if check and not result.ok:
            raise PipelineError(result)
        return result

    def _preflight(
        self,
        validate: bool,
        pre: list[Callable[[], CheckResult]] | None,
        emit: Callable[[str], None],
    ) -> list[CheckResult]:
        tools = unique_tools(self._steps) if validate else []
        checks = [check() for check in (*(s.as_check() for s in tools), *(pre or []))]
        for c in checks:
            if c.should_report:
                emit(c.format())
        return checks

    def _execute(self, opts: RunOptions, timeout: float | None, group: ProcessGroup) -> Outcome:
        timer = None
        with opts.console.job(pipeline_title(self._steps)) as job:
            try:
                if timeout is not None:
                    message = f"clichain: timed out after {timeout}s"
                    timer = threading.Timer(timeout, group.stop, ("timeout", TIMEOUT_EXIT, message))
                    timer.daemon = True
                    timer.start()
                outcome = execute(self._steps, opts, group, job)
                group.finish()  # a stop arriving from here on changed nothing: ignore it
                stopped = group.stopped
                if stopped is not None:  # the stop's code wins over the killed processes'
                    outcome.returncode = stopped.returncode
                    if stopped.message:
                        outcome.diagnostics.append(stopped.message)
            finally:
                if timer is not None:
                    timer.cancel()
                    timer.join()
            job.finish(outcome.returncode)
        return outcome

    def _describe_and_exit(self, mode: str, output: render.Output) -> None:
        data = describe_data(self._steps)
        lines = [json.dumps(data, indent=2)] if mode == "json" else render.describe_lines(data)
        render.emit_lines(lines, output)
        raise DescribeExit

    def __repr__(self) -> str:
        return f"Pipeline({pipeline_title(self._steps)})"


class Cmd(Pipeline):
    """A single command. Call it with arguments to get a new Cmd with those appended."""

    __slots__ = ()

    def __init__(self, step: CmdStep) -> None:
        super().__init__((step,))

    @property
    def step(self) -> CmdStep:
        step = self._steps[0]
        assert isinstance(step, CmdStep)
        return step

    def __call__(self, *args: str | os.PathLike[str]) -> Cmd:
        return Cmd(replace(self.step, args=(*self.step.args, *(os.fspath(a) for a in args))))

    def __repr__(self) -> str:
        return f"Cmd({self.step.title()})"


def tool(
    name: str,
    version: str | None = None,
    on_fail: OnFail = "error",
    msg: str = "",
    *,
    version_args: Iterable[str] = DEFAULT_VERSION_ARGS,
    ok_codes: Iterable[int] = (0,),
    tty: bool = False,
    env: Mapping[str, str] | None = None,
    inherit_env: bool = True,
    cwd: str | os.PathLike[str] | None = None,
) -> Cmd:
    """A command wrapper for the binary ``name``.

    version: constraint such as ``">=6.0,<8"`` (>=, <=, >, <, ==, !=, ~=); checked
        before running by probing ``name --version`` (or ``version_args``).
    on_fail: "error" stops the run when a check fails, "warn" reports, "pass" is silent.
    ok_codes: exit statuses that count as success, e.g. ``{0, 1}`` for grep or diff.
    tty: give the command's stderr a pseudo-terminal, so tools that only show progress
        on a terminal (git, curl, rsync) show it. stdout stays a pipe: data is unchanged.
    env: variables set for this command; inherit_env=False starts from an empty env.
    cwd: working directory for this command.
    """
    if on_fail not in get_args(OnFail):
        raise ValueError(f"on_fail must be one of {get_args(OnFail)}, got {on_fail!r}")
    if version is not None:
        parse_constraint(version)  # fail at definition, not mid-run
    codes = tuple(sorted(set(ok_codes)))
    if not codes or not all(type(c) is int and 0 <= c <= 255 for c in codes):
        raise ValueError(f"ok_codes must be exit statuses between 0 and 255, got {ok_codes!r}")
    return Cmd(
        CmdStep(
            name,
            version=version,
            version_args=tuple(version_args),
            ok_codes=codes,
            tty=tty,
            on_fail=on_fail,
            msg=msg,
            env=tuple((env or {}).items()),
            inherit_env=inherit_env,
            cwd=os.fspath(cwd) if cwd is not None else None,
        )
    )
