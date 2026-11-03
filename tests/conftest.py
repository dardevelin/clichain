"""Shared fixtures and helpers for the clichain test suite."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
from dataclasses import dataclass

import pytest

import clichain


@dataclass
class Snippet:
    stdout: str
    stderr: str
    returncode: int | None  # None when the snippet hung and was killed

    @property
    def hung(self) -> bool:
        return self.returncode is None


def run_snippet(
    code: str,
    *,
    timeout: float = 15,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> Snippet:
    """Run Python code in a fresh interpreter, isolated in its own session.

    A hang kills the whole session (the interpreter and anything it spawned), so a
    regression can never wedge the test run or leave orphans behind.
    """
    full_env = {**os.environ, **(env or {})}
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=cwd,
        env=full_env,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
        return Snippet(out, err, proc.returncode)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return Snippet(out, err, None)


def open_fds() -> set[int]:
    """File descriptors open in this process."""
    fd_dir = "/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"
    fds = set()
    for name in os.listdir(fd_dir):
        fd = int(name)
        try:
            os.fstat(fd)
        except OSError:
            continue  # the fd used by listdir itself
        fds.add(fd)
    return fds


# Every environment variable clichain itself reads, pinned for every test (None: unset).
# Without this the suite depends on where it runs: GitHub Actions sets GITHUB_ACTIONS,
# which switches grouped live output to ::group:: sections. A test that needs another
# value sets it with monkeypatch. test_environment.py fails if the library starts
# reading a variable that is not listed here.
HERMETIC_ENV: dict[str, str | None] = {
    "GITHUB_ACTIONS": None,
    "CLICHAIN_DESCRIBE": None,
    "TERM": "xterm-256color",
}


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch):
    for name, value in HERMETIC_ENV.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def captured_output(fn) -> list[str]:
    """Lines clichain emits while ``fn`` runs (the autouse fixture resets the output)."""
    lines: list[str] = []
    clichain.set_output(lines.append)
    fn()
    return lines


@pytest.fixture(autouse=True)
def _quiet_and_strict_threads():
    """Silence library output and fail on any unhandled exception in a thread."""
    clichain.set_output(None)
    errors: list[threading.ExceptHookArgs] = []
    previous = threading.excepthook
    threading.excepthook = errors.append
    try:
        yield
    finally:
        threading.excepthook = previous
        clichain.set_output(None)
    assert not errors, [f"{e.exc_type.__name__}: {e.exc_value}" for e in errors]
