"""Preflight checks: binary, env, file_exists, and custom checks."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

OnFail = Literal["error", "warn", "pass"]


@dataclass
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
        if self.ok:
            return True
        return self.on_fail != "pass"

    def format(self) -> str:
        if self.ok:
            return f"  ok: {self.name} ({self.found})"
        label = "error" if self.on_fail == "error" else "warn"
        if self.msg:
            return f"  {label}: {self.name} — {self.msg}"
        return f"  {label}: {self.name} — expected {self.expected}, found {self.found}"


def _detect_version(binary: str) -> str | None:
    path = shutil.which(binary)
    if not path:
        return None
    for flag in ["--version", "-version", "version", "-v"]:
        try:
            proc = subprocess.run(
                [path, flag],
                capture_output=True,
                text=True,
                timeout=5,
            )
            text = proc.stdout + proc.stderr
            match = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
            if match:
                return match.group(1)
        except (subprocess.TimeoutExpired, OSError):
            continue
    return None


def _version_satisfies(found: str, constraint: str) -> bool:
    """Check simple version constraints like >=1.7, <3.0, >=6.0,<8.0."""
    found_parts = [int(x) for x in found.split(".")]

    for part in constraint.split(","):
        part = part.strip()
        if part.startswith(">="):
            req = [int(x) for x in part[2:].split(".")]
            if found_parts < req:
                return False
        elif part.startswith("<="):
            req = [int(x) for x in part[2:].split(".")]
            if found_parts > req:
                return False
        elif part.startswith(">"):
            req = [int(x) for x in part[1:].split(".")]
            if found_parts <= req:
                return False
        elif part.startswith("<"):
            req = [int(x) for x in part[1:].split(".")]
            if found_parts >= req:
                return False
        elif part.startswith("=="):
            req = [int(x) for x in part[2:].split(".")]
            if found_parts != req:
                return False
    return True


def binary_check(
    name: str,
    version: str | None = None,
    on_fail: OnFail = "error",
    msg: str = "",
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
                ok=True,
                name=name,
                expected="on $PATH",
                found=f"{path}",
                msg=msg,
                on_fail=on_fail,
            )

        detected = _detect_version(name)
        if not detected:
            return CheckResult(
                ok=True,
                name=name,
                expected=version,
                found=f"{path} (version unknown)",
                msg=msg,
                on_fail=on_fail,
            )

        satisfied = _version_satisfies(detected, version)
        return CheckResult(
            ok=satisfied,
            name=name,
            expected=version,
            found=f"{detected} at {path}",
            msg=msg or (f"{name} {detected} does not satisfy {version}" if not satisfied else ""),
            on_fail=on_fail,
        )

    return _check


def env(name: str, on_fail: OnFail = "error", msg: str = "") -> Callable[[], CheckResult]:
    def _check() -> CheckResult:
        value = os.environ.get(name)
        return CheckResult(
            ok=value is not None,
            name=f"env:{name}",
            expected="set",
            found="set" if value is not None else "not set",
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


def run_checks(checks: list[Callable[[], CheckResult]]) -> list[CheckResult]:
    return [check() for check in checks]
