"""clichain — Fluent CLI tool chaining for Python."""

from clichain.checks import CheckResult, env, file_exists
from clichain.core import (
    Cmd,
    MeterStats,
    Pipeline,
    Result,
    StepProfile,
    _version,
    set_output,
    tool,
)

__version__ = _version

__all__ = [
    "CheckResult",
    "Cmd",
    "MeterStats",
    "Pipeline",
    "Result",
    "StepProfile",
    "__version__",
    "env",
    "file_exists",
    "set_output",
    "tool",
]
