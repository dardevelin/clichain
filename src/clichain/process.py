"""Child process supervision for one run: spawn, stop, and signal handling.

Stopping only ever *signals* processes. File descriptors are closed by their single
owner (the main thread or the worker thread they were handed to), never from here,
so a thread can never have an fd closed underneath it.
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType
from typing import IO, Any

GRACE = 5.0  # seconds between SIGTERM and SIGKILL


class Stopped(Exception):
    """A spawn was refused because the group is stopping."""


def _send(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Signal ``proc`` unless it has already exited."""
    with contextlib.suppress(OSError):
        if proc.poll() is None:
            proc.send_signal(sig)


@dataclass(frozen=True)
class Stop:
    """Why a run was stopped, and the Result it should report."""

    reason: str  # "timeout", "signal" or "error"
    returncode: int
    message: str | None = None


class ProcessGroup:
    """Every live child of one run (including nested each() runs).

    Thread-safe. ``stop()`` may be called from a signal handler, a timer thread or a
    worker thread; children spawned while or after it runs are terminated too.
    """

    def __init__(self) -> None:
        # Re-entrant: a signal handler runs on the main thread, possibly while the main
        # thread is inside one of these critical sections.
        self._lock = threading.RLock()
        self._procs: set[subprocess.Popen[bytes]] = set()
        self._stop: Stop | None = None
        self._finished = False
        self._killer: threading.Timer | None = None

    @property
    def stopped(self) -> Stop | None:
        """The first ``stop()`` request, or None while the run is going."""
        return self._stop

    def spawn(
        self,
        args: list[str],
        *,
        executable: str | None,
        stdin: IO[bytes] | int,
        stdout: IO[bytes] | int,
        stderr: IO[bytes] | int,
        env: dict[str, str] | None,
        cwd: str | None,
    ) -> subprocess.Popen[bytes]:
        if self._stop is not None:
            raise Stopped
        proc = subprocess.Popen(
            args,
            executable=executable,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            cwd=cwd,
            bufsize=0,  # raw pipes: reads return what is available, writes report progress
        )
        with self._lock:
            self._procs.add(proc)
            stopping = self._stop is not None
        if stopping:  # stop() ran between the check above and the add
            _send(proc, signal.SIGTERM)
        return proc

    def wait(self, proc: subprocess.Popen[bytes]) -> int:
        code = proc.wait()
        with self._lock:
            self._procs.discard(proc)
        return code

    def reap(self, procs: list[subprocess.Popen[bytes]]) -> None:
        """Terminate ``procs``, give them the grace period, then kill and wait."""
        for proc in procs:
            _send(proc, signal.SIGTERM)
        deadline = time.monotonic() + GRACE
        for proc in procs:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _send(proc, signal.SIGKILL)
            self.wait(proc)

    def stop(self, reason: str, returncode: int, message: str | None = None) -> None:
        """Terminate every child now and kill survivors after the grace period.

        The first call decides what the run reports (``returncode`` and ``message``).
        """
        with self._lock:
            if self._finished:
                return
            if self._stop is None:
                self._stop = Stop(reason, returncode, message)
            if self._killer is None:
                self._killer = threading.Timer(GRACE, self.kill)
                self._killer.daemon = True
                self._killer.start()
            procs = list(self._procs)
        for proc in procs:
            _send(proc, signal.SIGTERM)

    def finish(self) -> None:
        """Mark the run complete: later stop() calls (a late timer) are ignored."""
        with self._lock:
            self._finished = True

    def kill(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _send(proc, signal.SIGKILL)

    def close(self) -> None:
        """Release the kill timer. Call once every child has been waited for."""
        with self._lock:
            killer = self._killer
        if killer is not None:
            killer.cancel()
            killer.join()


Handler = Callable[[int, FrameType | None], Any] | int | None


class SignalGuard:
    """While active, SIGINT/SIGTERM stop the run's children instead of the process.

    Installed only on the main thread, and never over a signal that is ignored. After
    the run has cleaned up, ``deliver()`` hands the signal to whatever handled it
    before: the default SIGINT handler raises KeyboardInterrupt, a default SIGTERM
    terminates the process, and a custom handler is called as usual.
    """

    SIGNALS = (signal.SIGINT, signal.SIGTERM)

    def __init__(self, group: ProcessGroup) -> None:
        self._group = group
        self._previous: dict[int, Handler] = {}
        self.received: int | None = None

    def __enter__(self) -> SignalGuard:
        if threading.current_thread() is not threading.main_thread():
            return self
        for sig in self.SIGNALS:
            previous = signal.getsignal(sig)
            if previous is signal.SIG_IGN or previous is None:
                continue  # ignored, or owned by non-Python code: leave it alone
            try:
                signal.signal(sig, self._handle)
            except ValueError:  # not the main interpreter
                continue
            self._previous[sig] = previous
        return self

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        if self.received is not None:
            self._group.kill()  # second signal: don't wait out the grace period
            return
        self.received = signum
        self._group.stop("signal", 128 + signum)

    def __exit__(self, *_: object) -> None:
        for sig, previous in self._previous.items():
            signal.signal(sig, previous)  # type: ignore[arg-type]

    def deliver(self) -> None:
        """Re-deliver a received signal to its previous handler (after ``__exit__``)."""
        signum = self.received
        if signum is None:
            return
        previous = self._previous[signum]
        if callable(previous):
            previous(signum, sys._getframe(1))
        elif previous == signal.SIG_DFL:
            signal.raise_signal(signum)
