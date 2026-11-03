"""What a run returns: output, status, diagnostics, and run records."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from clichain import manifest as _manifest
from clichain import render
from clichain.checks import CheckResult
from clichain.steps import DEFAULT_ENCODING, DEFAULT_ERRORS, Step


@dataclass
class MeterStats:
    label: str = ""
    bytes: int = 0
    lines: int = 0
    elapsed: float = 0.0
    bytes_per_sec: float = 0.0
    lines_per_sec: float = 0.0


@dataclass(frozen=True)
class StepProfile:
    name: str
    elapsed: float = 0.0
    lines_in: int = 0
    lines_out: int = 0
    spawns: int = 0
    bytes_in: int = 0  # bytes entering the group
    bytes_out: int = 0  # bytes it produced (written to the file, for a redirect)


@dataclass
class Result:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    checks: list[CheckResult] = field(default_factory=list)
    profile: list[StepProfile] = field(default_factory=list)
    elapsed: float = 0.0
    _steps: tuple[Step, ...] = field(default=(), repr=False)
    _timestamp: str = ""
    _failed_step: int | None = field(default=None, repr=False)
    _source_location: str = field(default="", repr=False)
    _encoding: str = field(default=DEFAULT_ENCODING, repr=False)
    _errors: str = field(default=DEFAULT_ERRORS, repr=False)
    _output: render.Output = field(default=render.DEFAULT, repr=False)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def lines(self) -> list[str]:
        return self.stdout.strip().splitlines()

    @property
    def stdout_bytes(self) -> bytes:
        """stdout as the exact bytes the pipeline produced (lossless round-trip)."""
        return self.stdout.encode(self._encoding, self._errors)

    def explain(self) -> None:
        """Print an error diagnosis with the execution trace."""
        render.emit_lines(render.explain_lines(self), self._output)

    def report(self) -> None:
        """Print the full execution report."""
        render.emit_lines(render.report_lines(self, self.manifest()), self._output)

    def manifest(self, redact: bool = False) -> dict[str, Any]:
        """Run record: environment, tools, files, call tree, timing, exit code.

        ``redact=True`` drops hostname, cwd and absolute paths before sharing it.
        """
        return _manifest.run_manifest(self, redact=redact)

    def manifest_json(self, indent: int = 2, redact: bool = False) -> str:
        return json.dumps(self.manifest(redact=redact), indent=indent)

    def sbom(self) -> dict[str, Any]:
        """CycloneDX 1.6 software bill of materials for the tools this run used."""
        return _manifest.cyclonedx(self)

    def sbom_json(self, indent: int = 2) -> str:
        return json.dumps(self.sbom(), indent=indent)
