"""End-to-end test for clichain compile.

Requires pyinstaller: pip install clichain[compile]
Skipped if pyinstaller is not installed.
"""

import importlib.util
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("PyInstaller") is None,
    reason="pyinstaller not installed (pip install clichain[compile])",
)


SCRIPT = """\
from clichain import tool, set_output
set_output(None)

echo = tool("echo")
sort = tool("sort")
wc = tool("wc")

result = (
    echo("cherry\\napple\\nbanana")
    .pipe(sort())
    .pipe(wc("-l"))
    .run(validate=False)
)
print(result.stdout.strip())
"""


def test_compile_and_run(tmp_path):
    """Compile a clichain script to a binary, run it, verify output."""
    script_path = tmp_path / "test_script.py"
    script_path.write_text(SCRIPT)
    binary_name = "test_binary"
    binary_path = tmp_path / "dist" / binary_name

    # Compile
    result = subprocess.run(
        [
            "clichain",
            "compile",
            str(script_path),
            "-o",
            binary_name,
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )
    assert result.returncode == 0, f"compile failed:\n{result.stderr}"
    assert binary_path.exists(), f"binary not found at {binary_path}"

    # Run the compiled binary
    run_result = subprocess.run(
        [str(binary_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run_result.returncode == 0, f"binary failed:\n{run_result.stderr}"
    assert run_result.stdout.strip() == "3"


SCRIPT_REPORT = """\
import sys
from clichain import tool, set_output
set_output(None)

echo = tool("echo")
result = echo("hello").run(validate=False)

sbom = result.sbom()
assert "generator" in sbom
assert sbom["generator"]["name"] == "clichain"
assert len(sbom["tools"]) > 0
print("sbom:ok")

if result.ok:
    print("run:ok")
"""


def test_compile_sbom_works(tmp_path):
    """Compiled binary can produce SBOM."""
    script_path = tmp_path / "test_sbom.py"
    script_path.write_text(SCRIPT_REPORT)
    binary_name = "test_sbom"
    binary_path = tmp_path / "dist" / binary_name

    # Compile
    result = subprocess.run(
        [
            "clichain",
            "compile",
            str(script_path),
            "-o",
            binary_name,
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )
    assert result.returncode == 0, f"compile failed:\n{result.stderr}"

    # Run
    run_result = subprocess.run(
        [str(binary_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run_result.returncode == 0, f"binary failed:\n{run_result.stderr}"
    assert "sbom:ok" in run_result.stdout
    assert "run:ok" in run_result.stdout


SCRIPT_ERROR = """\
from clichain import tool, set_output
set_output(None)

missing = tool("nonexistent_xyz_tool", on_fail="warn")
result = missing().run()
print(f"ok:{result.ok}")
print(f"rc:{result.returncode}")
"""


def test_compile_error_handling(tmp_path):
    """Compiled binary handles errors correctly."""
    script_path = tmp_path / "test_error.py"
    script_path.write_text(SCRIPT_ERROR)
    binary_name = "test_error"
    binary_path = tmp_path / "dist" / binary_name

    # Compile
    result = subprocess.run(
        [
            "clichain",
            "compile",
            str(script_path),
            "-o",
            binary_name,
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )
    assert result.returncode == 0, f"compile failed:\n{result.stderr}"

    # Run
    run_result = subprocess.run(
        [str(binary_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "ok:False" in run_result.stdout
    assert "rc:127" in run_result.stdout
