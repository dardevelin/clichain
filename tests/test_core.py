"""Tests for clichain core."""

import os
import sys

import clichain
from clichain import env, file_exists, tool

# Silence check output during tests
clichain.set_output(None)


# -- Basic execution ----------------------------------------------------------


def test_simple_run():
    echo = tool("echo")
    result = echo("hello").run()
    assert result.ok
    assert result.stdout.strip() == "hello"


def test_result_lines():
    echo = tool("echo")
    result = echo("a\nb\nc").run()
    assert result.lines == ["a", "b", "c"]


def test_result_lines_empty():
    echo = tool("echo")
    result = echo("").run()
    assert result.lines == []


# -- Pipe (streaming) --------------------------------------------------------


def test_pipe_two_commands():
    echo = tool("echo")
    wc = tool("wc")
    result = echo("one\ntwo\nthree").pipe(wc("-l")).run()
    assert result.ok
    assert result.stdout.strip() == "3"


def test_pipe_three_commands():
    echo = tool("echo")
    grep = tool("grep")
    wc = tool("wc")
    result = echo("foo\nbar\nbaz").pipe(grep("ba")).pipe(wc("-l")).run()
    assert result.ok
    assert result.stdout.strip() == "2"


def test_pipe_exit_early_on_failure():
    false_cmd = tool("false")
    echo = tool("echo")
    result = false_cmd().pipe(echo("should not reach")).run()
    assert not result.ok


# -- Streaming filter between commands ----------------------------------------


def test_filter_streams_between_cmds():
    echo = tool("echo")
    wc = tool("wc")
    result = (
        echo("a.py\nb.txt\nc.py").filter(lambda line: line.endswith(".py")).pipe(wc("-l")).run()
    )
    assert result.ok
    assert result.stdout.strip() == "2"


def test_filter_chain():
    echo = tool("echo")
    result = (
        echo("1\n2\n3\n4\n5")
        .filter(lambda line: int(line) > 2)
        .filter(lambda line: int(line) < 5)
        .run()
    )
    assert result.ok
    assert result.lines == ["3", "4"]


# -- Streaming peek between commands -----------------------------------------


def test_peek_streams_between_cmds():
    echo = tool("echo")
    wc = tool("wc")
    seen: list[str] = []
    result = echo("hello\nworld").peek(fn=lambda line: seen.append(line)).pipe(wc("-l")).run()
    assert result.ok
    assert result.stdout.strip() == "2"
    assert len(seen) == 2


def test_peek_with_filter_streaming():
    echo = tool("echo")
    wc = tool("wc")
    seen: list[str] = []
    result = (
        echo("a.py\nb.txt\nc.py")
        .filter(lambda line: line.endswith(".py"))
        .peek(fn=lambda line: seen.append(line.strip()))
        .pipe(wc("-l"))
        .run()
    )
    assert result.ok
    assert result.stdout.strip() == "2"
    assert seen == ["a.py", "c.py"]


# -- Capture + interpolation --------------------------------------------------


def test_capture_and_interpolate():
    echo = tool("echo")
    result = echo("world").capture("name").pipe(echo("hello {name}")).run()
    assert result.ok
    assert result.stdout.strip() == "hello world"


# -- Filter (standalone) -----------------------------------------------------


def test_filter():
    echo = tool("echo")
    result = echo("foo.py\nbar.txt\nbaz.py").filter(lambda line: line.endswith(".py")).run()
    assert result.ok
    assert result.lines == ["foo.py", "baz.py"]


# -- Each ---------------------------------------------------------------------


def test_each_sequential():
    echo = tool("echo")
    result = echo("a\nb\nc").each(lambda line: echo(f"item: {line}")).run()
    assert result.ok
    assert "item: a" in result.stdout
    assert "item: b" in result.stdout
    assert "item: c" in result.stdout


def test_each_parallel():
    echo = tool("echo")
    result = echo("a\nb\nc").each(lambda line: echo(f"item: {line}"), workers=3).run()
    assert result.ok
    assert "item: a" in result.stdout
    assert "item: b" in result.stdout
    assert "item: c" in result.stdout


def test_each_early_exit_on_failure():
    echo = tool("echo")
    false_cmd = tool("false")
    result = echo("a\nb\nc").each(lambda line: false_cmd()).run()
    assert not result.ok


# -- Peek (standalone) -------------------------------------------------------


def test_peek_passthrough():
    echo = tool("echo")
    seen: list[str] = []
    result = echo("hello\nworld").peek(fn=lambda line: seen.append(line)).run()
    assert result.ok
    assert result.stdout.strip() == "hello\nworld"
    assert len(seen) == 2


# -- Collect ------------------------------------------------------------------


def test_collect():
    echo = tool("echo")
    result = echo("hello").collect().run()
    assert result.ok
    assert result.stdout.strip() == "hello"


# -- Feed ---------------------------------------------------------------------


def test_feed():
    sort = tool("sort")
    result = sort().feed("cherry\napple\nbanana\n").run()
    assert result.ok
    assert result.lines == ["apple", "banana", "cherry"]


# -- Redirect ----------------------------------------------------------------


def test_redirect_stdout(tmp_path):
    echo = tool("echo")
    out_file = str(tmp_path / "out.txt")
    echo("saved to file").redirect(stdout=out_file).run()
    with open(out_file) as f:
        assert "saved to file" in f.read()


def test_redirect_append(tmp_path):
    echo = tool("echo")
    out_file = str(tmp_path / "out.txt")
    echo("line1").redirect(stdout=out_file).run()
    echo("line2").redirect(stdout=out_file, append=True).run()
    with open(out_file) as f:
        content = f.read()
    assert "line1" in content
    assert "line2" in content


# -- Merge stderr -------------------------------------------------------------


def test_merge_stderr():
    bash = tool("bash")
    result = bash("-c", "echo out; echo err >&2").merge_stderr().run()
    assert result.ok
    assert "out" in result.stdout
    assert "err" in result.stdout


# -- Checks: binary ----------------------------------------------------------


def test_check_binary_exists():
    echo = tool("echo")
    results = echo("hello").check()
    assert all(r.ok for r in results)


def test_check_binary_missing():
    missing = tool("nonexistent_tool_xyz")
    results = missing().check()
    assert any(not r.ok for r in results)


def test_run_validates_by_default():
    missing = tool("nonexistent_tool_xyz")
    result = missing().run()
    assert not result.ok
    assert len(result.checks) > 0
    assert any(not c.ok for c in result.checks)


def test_run_skip_validation():
    # Even with a missing tool, validate=False skips checks
    # (the subprocess will still fail, but checks won't block)
    echo = tool("echo")
    result = echo("hello").run(validate=False)
    assert result.ok


def test_check_version():
    # bash should exist and have a version
    bash = tool("bash", version=">=1.0")
    results = bash().check()
    assert all(r.ok for r in results)


def test_check_on_fail_warn():
    missing = tool("nonexistent_tool_xyz", on_fail="warn")
    result = missing().run()
    # warn doesn't block execution — but subprocess will fail
    assert len(result.checks) > 0


def test_check_on_fail_pass():
    missing = tool("nonexistent_tool_xyz", on_fail="pass")
    result = missing().run()
    # pass doesn't block execution
    assert len(result.checks) > 0


# -- Checks: env -------------------------------------------------------------


def test_check_env_exists():
    os.environ["CLICHAIN_TEST_VAR"] = "hello"
    echo = tool("echo")
    result = echo("hello").run(pre=[env("CLICHAIN_TEST_VAR")])
    assert result.ok
    del os.environ["CLICHAIN_TEST_VAR"]


def test_check_env_missing():
    echo = tool("echo")
    result = echo("hello").run(pre=[env("CLICHAIN_MISSING_VAR_XYZ")])
    assert not result.ok
    assert any(not c.ok for c in result.checks)


def test_check_env_warn():
    echo = tool("echo")
    result = echo("hello").run(pre=[env("CLICHAIN_MISSING_VAR_XYZ", on_fail="warn")])
    assert result.ok  # warn doesn't block


# -- Checks: file_exists -----------------------------------------------------


def test_check_file_exists(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello")
    echo = tool("echo")
    result = echo("hello").run(pre=[file_exists(str(f))])
    assert result.ok


def test_check_file_missing():
    echo = tool("echo")
    result = echo("hello").run(pre=[file_exists("/nonexistent/path/xyz.txt")])
    assert not result.ok


# -- Checks: custom message --------------------------------------------------


def test_check_custom_msg():
    missing = tool("nonexistent_tool_xyz", msg="install with: brew install xyz")
    results = missing().check()
    assert any("brew install xyz" in r.msg for r in results)


# -- Profile ------------------------------------------------------------------


def test_profile_attached():
    echo = tool("echo")
    result = echo("hello").run()
    assert len(result.profile) > 0
    assert result.elapsed > 0


def test_profile_tracks_spawns():
    echo = tool("echo")
    grep = tool("grep")
    wc = tool("wc")
    result = echo("a\nb\nc").pipe(grep("a")).pipe(wc("-l")).run()
    assert result.ok
    # Streaming group has 3 spawns
    assert any(p.spawns == 3 for p in result.profile)


def test_profile_each_spawns():
    echo = tool("echo")
    result = echo("a\nb\nc").each(lambda line: echo(f"item: {line}")).run()
    assert result.ok
    each_profile = [p for p in result.profile if "each" in p.name]
    assert len(each_profile) == 1
    assert each_profile[0].spawns == 3


def test_profile_lines():
    echo = tool("echo")
    result = echo("a\nb\nc\nd\ne").filter(lambda line: line in ("a", "c", "e")).run()
    assert result.ok
    assert result.profile[-1].lines_out == 3


# -- SBOM ---------------------------------------------------------------------


def test_sbom_contains_tools():
    echo = tool("echo")
    grep = tool("grep")
    result = echo("hello").pipe(grep("hello")).run()
    sbom = result.sbom()
    assert "tools" in sbom
    names = [t["name"] for t in sbom["tools"]]
    assert "echo" in names
    assert "grep" in names


def test_sbom_contains_paths():
    echo = tool("echo")
    result = echo("hello").run()
    sbom = result.sbom()
    assert all("path" in t for t in sbom["tools"])


def test_sbom_contains_timestamp():
    echo = tool("echo")
    result = echo("hello").run()
    sbom = result.sbom()
    assert "timestamp" in sbom
    assert sbom["timestamp"] != ""


def test_sbom_tracks_files_written(tmp_path):
    echo = tool("echo")
    out_file = str(tmp_path / "out.txt")
    result = echo("hello").redirect(stdout=out_file).run()
    sbom = result.sbom()
    assert "files_written" in sbom
    assert out_file in sbom["files_written"]


def test_sbom_no_files_written():
    echo = tool("echo")
    result = echo("hello").run()
    sbom = result.sbom()
    assert "files_written" not in sbom


def test_sbom_json():
    echo = tool("echo")
    result = echo("hello").run()
    j = result.sbom_json()
    import json

    parsed = json.loads(j)
    assert "tools" in parsed


def test_sbom_generator():
    echo = tool("echo")
    result = echo("hello").run()
    sbom = result.sbom()
    assert "generator" in sbom
    assert sbom["generator"]["name"] == "clichain"
    assert sbom["generator"]["version"] == "0.1.0"
    assert "python" in sbom["generator"]


def test_sbom_call_tree():
    echo = tool("echo")
    grep = tool("grep")
    result = echo("hello\nworld").filter(lambda line: "hello" in line).pipe(grep("hello")).run()
    sbom = result.sbom()
    assert "call_tree" in sbom
    tree = sbom["call_tree"]
    assert len(tree) == 3
    assert tree[0]["type"] == "CmdStep"
    assert tree[0]["cmd"] == "echo 'hello\nworld'"
    assert tree[1]["type"] == "FilterStep"
    assert tree[2]["type"] == "CmdStep"


def test_sbom_call_tree_capture():
    echo = tool("echo")
    result = echo("world").capture("name").pipe(echo("hello {name}")).run()
    sbom = result.sbom()
    tree = sbom["call_tree"]
    capture_steps = [s for s in tree if s["type"] == "CaptureStep"]
    assert len(capture_steps) == 1
    assert capture_steps[0]["name"] == "name"


def test_sbom_deduplicates_tools():
    echo = tool("echo")
    result = echo("a").pipe(echo("b")).pipe(echo("c")).run()
    sbom = result.sbom()
    names = [t["name"] for t in sbom["tools"]]
    assert names.count("echo") == 1


# -- From file ----------------------------------------------------------------


def test_from_file(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("cherry\napple\nbanana\n")
    sort = tool("sort")
    result = sort().from_file(str(f)).run()
    assert result.ok
    assert result.lines == ["apple", "banana", "cherry"]


def test_from_file_pipe(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("foo\nbar\nbaz\nfoo\n")
    sort = tool("sort")
    uniq = tool("uniq")
    result = sort().from_file(str(f)).pipe(uniq()).run()
    assert result.ok
    assert result.lines == ["bar", "baz", "foo"]


def test_from_file_filter(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("a.py\nb.txt\nc.py\n")
    wc = tool("wc")
    result = wc("-l").from_file(str(f))
    # Use it as a source then filter
    cat = tool("cat")
    result = cat().from_file(str(f)).filter(lambda line: line.endswith(".py")).run()
    assert result.ok
    assert result.lines == ["a.py", "c.py"]


def test_from_file_sbom(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("hello\n")
    cat = tool("cat")
    result = cat().from_file(str(f)).run()
    sbom = result.sbom()
    assert "files_read" in sbom
    assert str(f) in sbom["files_read"]


def test_from_file_redirect(tmp_path):
    """from_file as source, redirect as sink — like bash: sort < in.txt > out.txt"""
    in_file = tmp_path / "in.txt"
    in_file.write_text("cherry\napple\nbanana\n")
    out_file = str(tmp_path / "out.txt")
    sort = tool("sort")
    result = sort().from_file(str(in_file)).redirect(stdout=out_file).run()
    assert result.ok
    with open(out_file) as f:
        assert f.read().strip().splitlines() == ["apple", "banana", "cherry"]


def test_from_file_block_size(tmp_path):
    """block_size reads file in chunks via thread."""
    f = tmp_path / "data.txt"
    f.write_text("cherry\napple\nbanana\n")
    sort = tool("sort")
    result = sort().from_file(str(f), block_size=64).run()
    assert result.ok
    assert result.lines == ["apple", "banana", "cherry"]


def test_from_file_block_size_pipe(tmp_path):
    """block_size works with downstream pipes."""
    f = tmp_path / "data.txt"
    f.write_text("aaa\nbbb\nccc\naaa\n")
    sort = tool("sort")
    uniq = tool("uniq")
    result = sort().from_file(str(f), block_size=128).pipe(uniq()).run()
    assert result.ok
    assert result.lines == ["aaa", "bbb", "ccc"]


def test_from_file_block_size_large(tmp_path):
    """block_size handles data larger than one block."""
    f = tmp_path / "data.txt"
    lines = [f"line-{i:06d}" for i in range(1000)]
    f.write_text("\n".join(lines) + "\n")
    wc = tool("wc")
    result = wc("-l").from_file(str(f), block_size=256).run()
    assert result.ok
    assert result.stdout.strip() == "1000"


# -- Meter --------------------------------------------------------------------


def test_meter_passthrough():
    echo = tool("echo")
    wc = tool("wc")
    result = echo("hello\nworld").meter().pipe(wc("-l")).run()
    assert result.ok
    assert result.stdout.strip() == "2"


def test_meter_counts_bytes():
    echo = tool("echo")
    result = echo("hello\nworld").meter().run()
    assert result.ok
    has_bytes = any(p.bytes_in is not None for p in result.profile)
    assert has_bytes


def test_meter_custom_callback():
    echo = tool("echo")
    collected: list = []
    result = echo("a\nb\nc").meter(to=lambda stats: collected.append(stats), interval=0).run()
    assert result.ok
    assert len(collected) > 0
    assert collected[-1].lines == 3


def test_meter_with_label():
    echo = tool("echo")
    result = echo("hello").meter(label="test").run()
    assert result.ok
    assert any("meter(test)" in p.name for p in result.profile)


def test_meter_no_bytes_without_meter():
    echo = tool("echo")
    result = echo("hello").run()
    assert all(p.bytes_in is None for p in result.profile)


def test_report(capsys):
    import clichain

    clichain.set_output(sys.stderr)

    echo = tool("echo")
    grep = tool("grep")
    result = echo("hello\nworld").pipe(grep("hello")).run()
    result.report()

    clichain.set_output(None)

    captured = capsys.readouterr()
    assert "clichain" in captured.err
    assert "total" in captured.err
