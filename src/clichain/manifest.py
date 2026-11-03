"""Run records: the tool/file inventory, the run manifest, and the CycloneDX SBOM."""

from __future__ import annotations

import functools
import os
import platform
import shlex
import shutil
import subprocess
import uuid
from typing import TYPE_CHECKING, Any

from clichain.checks import detect_version
from clichain.steps import CmdStep, FromFileStep, RedirectStep, Step

if TYPE_CHECKING:
    from clichain.result import Result


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("cmdchain")
    except PackageNotFoundError:
        return "0.0.0"


VERSION = _package_version()
_PATH_KEYS = {"path", "stdout", "stderr", "cwd"}  # call-tree fields that hold file paths


def _redact_arg(arg: str) -> str:
    """Drop the directories of an absolute path argument (``/home/a/x.txt``, ``--f=/a/x``)."""
    key, sep, value = arg.partition("=")
    if sep and value.startswith("/"):
        return f"{key}={_redact_arg(value)}"
    return (os.path.basename(arg) or arg) if arg.startswith("/") else arg


def unique_tools(steps: tuple[Step, ...]) -> list[CmdStep]:
    seen: dict[str, CmdStep] = {}
    for step in steps:
        if isinstance(step, CmdStep):
            seen.setdefault(step.binary, step)
    return list(seen.values())


def files(steps: tuple[Step, ...], redact: bool = False) -> dict[str, list[str]]:
    """``files_read`` / ``files_written`` entries for a record (omitted when empty)."""
    read = [s.path for s in steps if isinstance(s, FromFileStep)]
    written = [p for s in steps if isinstance(s, RedirectStep) for p in (s.stdout, s.stderr) if p]
    found = {"files_read": read, "files_written": written}
    if redact:
        found = {k: [os.path.basename(p) for p in v] for k, v in found.items()}
    return {k: v for k, v in found.items() if v}


def tool_info(step: CmdStep) -> dict[str, Any]:
    """Name, resolved path and detected version (probed once per binary, then cached)."""
    entry: dict[str, Any] = {"name": step.binary}
    path = shutil.which(step.binary)
    if path:
        entry["path"] = path
        version = detect_version(step.binary, step.version_args, path=path)
        if version:
            entry["version"] = version
    return entry


def _generator() -> dict[str, str]:
    return {"name": "clichain", "version": VERSION, "python": platform.python_version()}


def source_commit(source_location: str) -> str | None:
    """Short HEAD of the git repository holding the script that called run()."""
    script = source_location.rpartition(":")[0]
    directory = os.path.dirname(script) if script and os.path.exists(script) else os.getcwd()
    return _git_head(os.path.realpath(directory))


@functools.lru_cache(maxsize=32)
def _git_head(directory: str) -> str | None:
    """Looked up once per directory per process: reports call this repeatedly."""
    try:
        proc = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--short", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def describe_data(steps: tuple[Step, ...]) -> dict[str, Any]:
    """What a pipeline needs, without running it (``CLICHAIN_DESCRIBE=json``)."""
    tools = []
    for step in unique_tools(steps):
        entry = tool_info(step)
        if step.version:
            entry["requires"] = step.version
        entry["found"] = "path" in entry
        entry["ok"] = step.as_check()().ok
        tools.append(entry)
    return {
        "generator": _generator(),
        "environment": {"os": platform.system(), "arch": platform.machine()},
        "steps": [
            {"step": i, "type": type(s).__name__, "name": s.title()} for i, s in enumerate(steps)
        ],
        "tools": tools,
        **files(steps),
    }


def run_manifest(result: Result, redact: bool = False) -> dict[str, Any]:
    tools = [tool_info(s) for s in unique_tools(result._steps)]
    environment = {
        "os": platform.system(),
        "os_version": platform.release(),
        "arch": platform.machine(),
    }
    if redact:
        for t in tools:
            t.pop("path", None)
    else:
        environment |= {"hostname": platform.node(), "cwd": os.getcwd()}

    data: dict[str, Any] = {"generator": _generator(), "environment": environment}
    commit = source_commit(result._source_location)
    if commit:
        data["source_commit"] = commit
    call_tree = []
    for i, step in enumerate(result._steps):
        node = {"step": i, "type": type(step).__name__, **step.node()}
        if redact:
            for key in _PATH_KEYS & node.keys():
                node[key] = os.path.basename(node[key])
            if isinstance(step, CmdStep):
                node["cmd"] = shlex.join(map(_redact_arg, (step.binary, *step.args)))
        call_tree.append(node)
    data |= {
        "timestamp": result._timestamp,
        "elapsed": round(result.elapsed, 4),
        "exit_code": result.returncode,
        "tools": tools,
        "call_tree": call_tree,
        **files(result._steps, redact),
    }
    return data


def cyclonedx(result: Result) -> dict[str, Any]:
    """CycloneDX 1.6 BOM: one ``application`` component per tool the run used."""
    components = []
    for step in unique_tools(result._steps):
        info = tool_info(step)
        component: dict[str, Any] = {
            "type": "application",
            "bom-ref": f"tool:{step.binary}",
            "name": step.binary,
        }
        if "version" in info:
            component["version"] = info["version"]
        if "path" in info:
            component["properties"] = [{"name": "clichain:path", "value": info["path"]}]
        components.append(component)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": result._timestamp,
            "tools": {
                "components": [{"type": "application", "name": "clichain", "version": VERSION}]
            },
            "properties": [
                {"name": "clichain:python", "value": platform.python_version()},
                {"name": "clichain:exit_code", "value": str(result.returncode)},
            ],
        },
        "components": components,
    }
