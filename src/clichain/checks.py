"""Preflight checks: binaries (with version constraints), environment variables, files."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

OnFail = Literal["error", "warn", "pass"]

DEFAULT_VERSION_ARGS: tuple[str, ...] = ("--version",)
_PROBE_TIMEOUT = 5


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    name: str
    expected: str
    found: str
    msg: str
    on_fail: OnFail

    @property
    def should_stop(self) -> bool:
        return not self.ok and self.on_fail == "error"

    @property
    def should_report(self) -> bool:
        return self.ok or self.on_fail != "pass"

    def format(self) -> str:
        if self.ok:
            return f"  ok: {self.name} ({self.found})"
        label = "error" if self.on_fail == "error" else "warn"
        if self.msg:
            return f"  {label}: {self.name} — {self.msg}"
        return f"  {label}: {self.name} — expected {self.expected}, found {self.found}"


# -- Version detection --------------------------------------------------------

_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")
_probe_cache: dict[tuple[str, tuple[str, ...]], str | None] = {}
_probe_lock = threading.Lock()


def detect_version(
    binary: str, args: tuple[str, ...] = DEFAULT_VERSION_ARGS, path: str | None = None
) -> str | None:
    """Run ``binary *args`` once per resolved path and return the first ``N.N[.N]`` it prints.

    Only the given flags are passed (``--version`` by default): never positional words,
    which tools without a version flag would treat as file operands. stdin is closed so
    the probe can never consume the caller's input.
    """
    path = path or shutil.which(binary)
    if path is None:
        return None
    key = (path, args)
    with _probe_lock:
        if key in _probe_cache:
            return _probe_cache[key]
    try:
        proc = subprocess.run(
            [path, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
        )
        text = (proc.stdout + proc.stderr).decode(errors="replace")
        match = _VERSION.search(text)
        version = match[0] if match else None
    except (subprocess.TimeoutExpired, OSError):
        version = None
    with _probe_lock:
        _probe_cache[key] = version
    return version


# -- Version constraints -------------------------------------------------------

Clause = tuple[str, tuple[int, ...]]
_CLAUSE = re.compile(r"\s*(>=|<=|==|!=|~=|>|<)\s*(\d+(?:\.\d+)*)\s*")


def parse_constraint(spec: str) -> tuple[Clause, ...]:
    """Parse ``">=6.0,<8"`` into clauses; raise ValueError on anything unrecognised."""
    clauses: list[Clause] = []
    for part in spec.split(","):
        match = _CLAUSE.fullmatch(part)
        if not match:
            raise ValueError(f"invalid version constraint {spec!r}: cannot parse {part!r}")
        op, number = match[1], tuple(int(x) for x in match[2].split("."))
        if op == "~=" and len(number) < 2:
            raise ValueError(f"invalid version constraint {spec!r}: ~= needs at least X.Y")
        clauses.append((op, number))
    return tuple(clauses)


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def _clause_ok(found: tuple[int, ...], op: str, req: tuple[int, ...]) -> bool:
    if op == "~=":  # compatible release: >= req, and same prefix up to the last component
        upper = (*req[:-2], req[-2] + 1)
        return _clause_ok(found, ">=", req) and _clause_ok(found, "<", upper)
    f, r = _pad(found, req)
    return {
        ">=": f >= r,
        "<=": f <= r,
        ">": f > r,
        "<": f < r,
        "==": f == r,
        "!=": f != r,
    }[op]


def version_satisfies(found: str, constraint: str) -> bool:
    """True when version ``found`` meets every clause of ``constraint``."""
    version = tuple(int(x) for x in found.split("."))
    return all(_clause_ok(version, op, req) for op, req in parse_constraint(constraint))


# -- Checks ----------------------------------------------------------------------


def binary_check(
    name: str,
    version: str | None = None,
    on_fail: OnFail = "error",
    msg: str = "",
    version_args: tuple[str, ...] = DEFAULT_VERSION_ARGS,
) -> Callable[[], CheckResult]:
    def _check() -> CheckResult:
        path = shutil.which(name)
        if not path:
            return CheckResult(
                ok=False,
                name=name,
                expected="on $PATH" + (f" {version}" if version else ""),
                found="not found",
                msg=msg or f"{name} not found on $PATH",
                on_fail=on_fail,
            )
        if not version:
            return CheckResult(
                ok=True, name=name, expected="on $PATH", found=path, msg=msg, on_fail=on_fail
            )

        detected = detect_version(name, version_args, path=path)
        if detected is None:  # fail closed: a constraint we cannot verify is not met
            return CheckResult(
                ok=False,
                name=name,
                expected=version,
                found=f"{path} (version unknown)",
                msg=msg or f"{name}: could not determine version to check {version}",
                on_fail=on_fail,
            )
        satisfied = version_satisfies(detected, version)
        return CheckResult(
            ok=satisfied,
            name=name,
            expected=version,
            found=f"{detected} at {path}",
            msg=msg or ("" if satisfied else f"{name} {detected} does not satisfy {version}"),
            on_fail=on_fail,
        )

    return _check


def env(name: str, on_fail: OnFail = "error", msg: str = "") -> Callable[[], CheckResult]:
    def _check() -> CheckResult:
        present = name in os.environ
        return CheckResult(
            ok=present,
            name=f"env:{name}",
            expected="set",
            found="set" if present else "not set",
            msg=msg or f"environment variable {name} is not set",
            on_fail=on_fail,
        )

    return _check


def file_exists(path: str, on_fail: OnFail = "error", msg: str = "") -> Callable[[], CheckResult]:
    def _check() -> CheckResult:
        exists = os.path.exists(path)
        return CheckResult(
            ok=exists,
            name=f"file:{path}",
            expected="exists",
            found="exists" if exists else f"not found (cwd: {os.getcwd()})",
            msg=msg or f"file {path} not found",
            on_fail=on_fail,
        )

    return _check
