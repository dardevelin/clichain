"""Security findings S1-S9: version probing, option injection, describe mode, provenance."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from clichain import tool
from clichain.checks import version_satisfies

from .conftest import run_snippet


def _fake_tool(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def probe_log(tmp_path, monkeypatch):
    """A tool with no version output that records every invocation's argv."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "argv.log"
    _fake_tool(bindir, "noversion", f'printf "%s\\n" "$*" >> "{log}"\n')
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def _invocations(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


# -- S1: the version probe runs --version only --------------------------------


def test_version_probe_never_passes_positional_or_short_flags(probe_log):
    tool("noversion", version=">=1.0", on_fail="warn")().check()
    assert _invocations(probe_log) == ["--version"]


def test_reporting_probes_each_binary_once(probe_log):
    pipeline = tool("noversion")("payload")
    result = pipeline.run()
    result.manifest()
    result.manifest()
    result.sbom()
    pipeline.describe()
    probes = [line for line in _invocations(probe_log) if line != "payload"]
    assert probes == ["--version"]


def test_version_args_override(probe_log):
    tool("noversion", version=">=1.0", on_fail="warn", version_args=("-V",))().check()
    assert _invocations(probe_log) == ["-V"]


def test_version_probe_does_not_touch_files_named_version(tmp_path, monkeypatch):
    # BSD rm / touch take "version" as a file operand; the probe must never pass it.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_tool(
        bindir, "fakerm", 'for a in "$@"; do case "$a" in -*) ;; *) rm -f "$a";; esac; done\n'
    )
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "version").write_text("keep")
    tool("fakerm")().run().manifest()
    assert (tmp_path / "version").exists()


# -- S2: the probe never reads the caller's stdin -----------------------------


def test_version_probe_leaves_stdin_alone(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_tool(bindir, "eatstdin", "cat > /dev/null\n")
    snippet = run_snippet(
        """
        import sys
        from clichain import tool
        tool("eatstdin", version=">=1", on_fail="warn")().check()
        print(repr(sys.stdin.read()))
        """,
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
        stdin="precious\n",
    )
    assert not snippet.hung
    assert snippet.stdout.strip() == repr("precious\n")


# -- S3: opt-in guard against option injection --------------------------------


def test_capture_reject_options_blocks_leading_dash():
    echo = tool("echo")
    result = echo("-rf").capture("f", reject_options=True).pipe(echo("{f}")).run()
    assert not result.ok
    assert result.returncode == 1
    assert "capture 'f'" in result.stderr


def test_capture_allows_leading_dash_by_default():
    echo = tool("echo")
    result = echo("--x").capture("f").pipe(echo("got {f}")).run()
    assert result.ok
    assert result.stdout.strip() == "got --x"


# -- S5: describe mode only for explicit values, never inherited --------------


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_describe_mode_off_values_run_normally(value):
    snippet = run_snippet(
        """
        from clichain import tool
        print(tool("echo")("ran").run().stdout.strip())
        """,
        env={"CLICHAIN_DESCRIBE": value},
    )
    assert snippet.returncode == 0
    assert snippet.stdout.strip() == "ran"


def test_describe_mode_text_does_not_execute(tmp_path):
    marker = tmp_path / "marker"
    snippet = run_snippet(
        f"""
        import clichain, sys
        clichain.set_output(sys.stdout)
        clichain.tool("touch")({str(marker)!r}).run()
        print("after run")
        """,
        env={"CLICHAIN_DESCRIBE": "describe"},
    )
    assert snippet.returncode == 0
    assert "pipeline: 1 steps" in snippet.stdout
    assert "after run" not in snippet.stdout
    assert not marker.exists()


def test_describe_mode_json():
    snippet = run_snippet(
        """
        import clichain, sys
        clichain.set_output(sys.stdout)
        clichain.tool("echo")("x").run()
        """,
        env={"CLICHAIN_DESCRIBE": "json"},
    )
    assert snippet.returncode == 0
    data = json.loads(snippet.stdout)
    assert data["tools"][0]["name"] == "echo"


def test_describe_exit_is_catchable_by_hosts(monkeypatch):
    import clichain

    monkeypatch.setenv("CLICHAIN_DESCRIBE", "1")
    with pytest.raises(clichain.DescribeExit) as info:
        tool("echo")("x").run()
    assert isinstance(info.value, SystemExit)
    assert info.value.code == 0


def test_describe_variable_not_inherited_by_children(monkeypatch):
    monkeypatch.setenv("CLICHAIN_DESCRIBE", "0")
    result = tool("sh")("-c", "echo ${CLICHAIN_DESCRIBE-unset}").run()
    assert result.stdout.strip() == "unset"


# -- S7: version constraints are validated and fail closed --------------------


@pytest.mark.parametrize("spec", ["9", ">=1.x", "~=9", "=>1.0", ">=1.0,", "latest"])
def test_invalid_constraints_rejected_at_definition(spec):
    with pytest.raises(ValueError, match="version constraint"):
        tool("echo", version=spec)


@pytest.mark.parametrize(
    ("found", "spec", "expected"),
    [
        ("1.7.0", "==1.7", True),
        ("1.7", "==1.7.0", True),
        ("1.0", "!=1.0", False),
        ("1.0.1", "!=1.0", True),
        ("1.4.9", "~=1.4.5", True),
        ("1.5.0", "~=1.4.5", False),
        ("1.9", "~=1.4", True),
        ("2.0", "~=1.4", False),
        ("2.0", ">=1.7,<3", True),
        ("3.0", ">=1.7,<3", False),
        ("1.10", ">1.9", True),
    ],
)
def test_version_satisfies(found, spec, expected):
    assert version_satisfies(found, spec) is expected


def test_unknown_version_fails_closed(probe_log):
    result = tool("noversion", version=">=1.0")().run()
    assert not result.ok
    check = result.checks[0]
    assert not check.ok
    assert "unknown" in check.found
    assert _invocations(probe_log) == ["--version"]  # blocked before running


def test_unknown_version_with_warn_still_runs(probe_log):
    result = tool("noversion", version=">=1.0", on_fail="warn")("ran").run()
    assert result.ok
    assert "ran" in _invocations(probe_log)


# -- S8: no subprocess at import; commit labelled as the source's -------------


def test_import_runs_no_subprocess(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "git.log"
    _fake_tool(bindir, "git", f'echo called >> "{log}"\n')
    snippet = run_snippet(
        "import clichain",
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert snippet.returncode == 0
    assert not log.exists()


def test_manifest_reports_source_commit_not_generator_commit():
    data = tool("echo")("x").run().manifest()
    assert "commit" not in data["generator"]
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert data["source_commit"] == head


# -- S9: redacted manifests carry no host details -----------------------------


def test_manifest_redact(tmp_path):
    out = tmp_path / "out.txt"
    result = tool("echo")("x").redirect(stdout=str(out)).run()
    data = result.manifest(redact=True)
    text = result.manifest_json(redact=True)
    assert "hostname" not in data["environment"]
    assert "cwd" not in data["environment"]
    assert all("path" not in t for t in data["tools"])
    assert data["files_written"] == ["out.txt"]
    assert socket.gethostname() not in text
    assert str(tmp_path) not in text


def test_manifest_redact_covers_command_arguments(tmp_path):
    target = tmp_path / "secret.txt"
    target.write_text("x\n")
    result = tool("cat")(str(target)).run()
    text = result.manifest_json(redact=True)
    assert str(tmp_path) not in text
    assert "secret.txt" in text  # the file name stays: only directories are removed
