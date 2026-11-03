"""CLI: explain validates codes (R11); check gates CI and never executes pipelines (S6)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

CLI = str(Path(sys.executable).with_name("clichain"))


def clichain_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [CLI, *args], capture_output=True, text=True, timeout=30, cwd=cwd, stdin=subprocess.DEVNULL
    )


def test_explain_known_code():
    r = clichain_cli("explain", "X127")
    assert r.returncode == 0
    assert "command not found" in r.stdout


def test_explain_timeout_code():
    r = clichain_cli("explain", "X124")
    assert r.returncode == 0
    assert "timed out" in r.stdout


@pytest.mark.parametrize("code", ["SX", "X", "Z1", "", "S-1"])
def test_explain_invalid_code(code):
    r = clichain_cli("explain", code)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "unknown error code" in r.stdout + r.stderr


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "script.py"
    path.write_text("from clichain import tool\n" + body)
    return str(path)


def test_check_passes_when_tools_exist(tmp_path):
    r = clichain_cli(
        "check", _script(tmp_path, "pipeline = tool('echo')('x').pipe(tool('cat')())\n")
    )
    assert r.returncode == 0, r.stderr


def test_check_fails_when_a_tool_is_missing(tmp_path):
    r = clichain_cli("check", _script(tmp_path, "x = tool('definitely-not-a-binary-zz')\n"))
    assert r.returncode == 1
    assert "not found" in r.stderr


def test_check_does_not_execute_inline_runs(tmp_path):
    marker = tmp_path / "marker"
    script = _script(
        tmp_path,
        f"tool('touch')({str(marker)!r}).run()\ntool('definitely-not-a-binary-zz')().run()\n",
    )
    r = clichain_cli("check", script)
    assert not marker.exists()
    assert r.returncode == 1  # the inline run's missing tool is still found


def test_check_survives_script_errors(tmp_path):
    script = _script(tmp_path, "p = tool('echo')('x')\nraise RuntimeError('broken script')\n")
    r = clichain_cli("check", script)
    assert r.returncode == 0
    assert "broken script" in r.stderr


def test_check_does_not_execute_runs_in_threads(tmp_path):
    marker = tmp_path / "marker"
    script = _script(
        tmp_path,
        "import threading\n"
        f"t = threading.Thread(target=lambda: tool('touch')({str(marker)!r}).run())\n"
        "t.start(); t.join()\n",
    )
    r = clichain_cli("check", script)
    assert not marker.exists()
    assert r.returncode == 0, r.stderr


def test_explain_exit_status_above_128_that_is_not_a_signal():
    from clichain.errors import error_code, explain_exit

    assert error_code(255) == "X255"
    assert "out of range" in explain_exit(255)
    assert error_code(130) == "S2"
    assert error_code(-13) == "S13"
