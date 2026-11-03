"""clichain — Fluent CLI tool chaining for Python."""

from clichain.checks import CheckResult, env, file_exists
from clichain.errors import DescribeExit, PipelineError
from clichain.live import Live
from clichain.manifest import VERSION as __version__
from clichain.pipeline import Cmd, Pipeline, tool
from clichain.render import set_output
from clichain.result import MeterStats, Result, StepProfile

__all__ = [
    "CheckResult",
    "Cmd",
    "DescribeExit",
    "Live",
    "MeterStats",
    "Pipeline",
    "PipelineError",
    "Result",
    "StepProfile",
    "__version__",
    "env",
    "file_exists",
    "set_output",
    "tool",
]
