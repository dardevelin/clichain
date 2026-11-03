"""Reliability findings R1-R12 and the API fixes A5 (immutability) and A8 (options)."""

from __future__ import annotations

import inspect
import os
import time

import pytest

import clichain
from clichain import PipelineError, tool

from .conftest import run_snippet

echo, cat, sh, seq = tool("echo"), tool("cat"), tool("sh"), tool("seq")


# -- R1-R3: no hangs, no silent truncation -------------------------------------


def test_large_stderr_in_multistage_group_does_not_hang():
    snippet = run_snippet("""
        from clichain import tool
        r = tool('sh')('-c', 'seq 1 100000 >&2; echo hi').pipe(tool('cat')()).run()
        print(r.ok, r.stdout.strip())
    """)
    assert not snippet.hung
    assert snippet.stdout.strip() == "True hi"


def test_filter_exception_with_large_upstream_raises_instead_of_hanging():
    snippet = run_snippet("""
        from clichain import tool
        def boom(line):
            if line == '10':
                raise ValueError('boom')
            return True
        try:
            tool('seq')('1', '200000').filter(boom).pipe(tool('wc')('-l')).run()
        except ValueError as e:
            print('raised', e)
    """)
    assert not snippet.hung
    assert snippet.stdout.strip() == "raised boom"


def test_bridge_before_early_exit_consumer_does_not_hang():
    snippet = run_snippet("""
        from clichain import tool
        r = tool('seq')('1', '1000000').filter(lambda line: True).pipe(tool('head')('-1')).run()
        print(r.ok, repr(r.stdout))
    """)
    assert not snippet.hung
    assert snippet.stdout.strip() == "True '1\\n'"


@pytest.mark.parametrize("kind", ["filter", "peek", "meter_to"])
def test_callback_exception_propagates_instead_of_truncating(kind):
    def boom(_):
        raise ValueError("boom")

    source = seq("1", "100")
    pipeline = {
        "filter": source.filter(boom),
        "peek": source.peek(fn=boom),
        "meter_to": source.meter(to=boom, interval=0),
    }[kind]
    with pytest.raises(ValueError, match="boom"):
        pipeline.pipe(tool("wc")("-l")).run()


# -- R4: SIGPIPE from an early-exit consumer is not a failure ------------------


def test_sigpipe_upstream_is_success_by_default():
    result = tool("yes")().pipe(tool("head")("-1")).run()
    assert result.ok
    assert result.stdout == "y\n"


def test_sigpipe_counts_when_not_ignored():
    result = tool("yes")().pipe(tool("head")("-1")).run(ignore_sigpipe=False)
    assert not result.ok
    assert result.returncode == -13


# -- R5: redirect stderr writes stderr; redirect consumes what it writes -------


def test_redirect_stderr_gets_command_stderr(tmp_path):
    out, err = tmp_path / "o.txt", tmp_path / "e.txt"
    result = sh("-c", "echo OUT; echo ERR >&2").redirect(stdout=str(out), stderr=str(err)).run()
    assert result.ok
    assert out.read_text() == "OUT\n"
    assert err.read_text() == "ERR\n"
    assert result.stdout == ""
    assert "ERR" not in result.stderr


def test_redirect_after_buffering_boundary(tmp_path):
    out, err = tmp_path / "o.txt", tmp_path / "e.txt"
    result = (
        sh("-c", "echo OUT; echo ERR >&2")
        .collect()
        .redirect(stdout=str(out), stderr=str(err))
        .run()
    )
    assert result.ok
    assert out.read_text() == "OUT\n"
    assert err.read_text() == "ERR\n"


def test_redirect_from_bridge_streams_to_file(tmp_path):
    out = tmp_path / "o.txt"
    result = seq("1", "5").filter(lambda line: line != "3").redirect(stdout=str(out)).run()
    assert result.ok
    assert out.read_text() == "1\n2\n4\n5\n"


# -- R6: literal braces survive interpolation ----------------------------------


def test_literal_braces_after_capture():
    awk = tool("awk")
    result = echo("x y").capture("v").pipe(echo("a b")).pipe(awk("{print $1}")).run()
    assert result.ok
    assert result.stdout.strip() == "a"


def test_only_captured_names_are_interpolated():
    result = echo("x").capture("v").pipe(echo("{v} {other} {v}")).run()
    assert result.stdout.strip() == "x {other} x"


# -- R7: binary-safe text handling ---------------------------------------------


def test_non_utf8_output_is_lossless():
    data = b"\xff\xfe\x00raw\n"
    printf = tool("printf")
    result = printf(r"\377\376\000raw\n").pipe(cat()).run()
    assert result.ok
    assert result.stdout_bytes == data
    filtered = printf(r"\377\376\000raw\n").filter(lambda line: True).run()
    assert filtered.stdout_bytes == data


def test_explicit_encoding_and_strict_errors():
    printf = tool("printf")
    assert printf(r"\377").run(encoding="latin-1").stdout == "\xff"
    with pytest.raises(UnicodeDecodeError):
        printf(r"\377").run(errors="strict")


def test_crlf_preserved_and_filter_sees_bare_line():
    seen: list[str] = []
    result = tool("printf")(r"a\r\nb\r\n").filter(lambda line: seen.append(line) or True).run()
    assert seen == ["a", "b"]
    assert result.stdout_bytes == b"a\r\nb\r\n"


# -- R9: per-run meter statistics ----------------------------------------------


def test_meter_stats_reset_between_runs():
    collected: list = []
    pipeline = seq("1", "10").meter(to=collected.append, interval=0).pipe(cat())
    pipeline.run()
    first = collected[-1].lines
    collected.clear()
    pipeline.run()
    assert first == collected[-1].lines == 10


def test_meter_callback_gets_snapshots():
    collected: list = []
    seq("1", "5").meter(to=collected.append, interval=0).run()
    assert [s.lines for s in collected[:5]] == [1, 2, 3, 4, 5]


# -- R10: one error model --------------------------------------------------------


def test_missing_input_file_is_a_failed_result():
    result = cat().from_file("/nonexistent/input.txt").pipe(tool("wc")()).run()
    assert not result.ok
    assert result.returncode == 1
    assert "/nonexistent/input.txt" in result.stderr


def test_unwritable_redirect_is_a_failed_result():
    result = echo("x").redirect(stdout="/nonexistent/dir/out.txt").run()
    assert not result.ok
    assert result.returncode == 1
    assert "/nonexistent/dir/out.txt" in result.stderr


def test_missing_binary_mid_pipeline_is_127():
    result = echo("x").pipe(tool("nonexistent_tool_xyz")()).run(validate=False)
    assert result.returncode == 127
    assert "command not found" in result.stderr


def test_check_true_raises_pipeline_error():
    with pytest.raises(PipelineError) as info:
        tool("false")().run(check=True)
    assert info.value.result.returncode == 1


# -- R12: no full stack inspection per run --------------------------------------


def test_run_does_not_call_inspect_stack(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("inspect.stack called")

    monkeypatch.setattr(inspect, "stack", forbidden)
    result = tool("false")().run()
    assert result._source_location.startswith(__file__)


# -- A5: immutable builders and a single input source ---------------------------


def test_branching_from_shared_prefix_does_not_alias():
    base = echo("b\na").pipe(cat())
    sorted_ = base.pipe(tool("sort")())
    counted = base.pipe(tool("wc")("-l"))
    assert sorted_ is not counted
    assert len(base.steps) == 2
    assert sorted_.run().stdout == "a\nb\n"
    assert counted.run().stdout.strip() == "2"


def test_second_input_source_is_rejected(tmp_path):
    f = tmp_path / "in.txt"
    f.write_text("x\n")
    with pytest.raises(ValueError, match="input source"):
        cat().feed("a").feed("b")
    with pytest.raises(ValueError, match="input source"):
        cat().feed("a").from_file(str(f))


def test_source_applies_to_head_of_pipeline():
    result = cat().pipe(tool("sort")()).feed("b\na\n").run()
    assert result.stdout == "a\nb\n"


def test_pipe_accepts_pipelines():
    result = echo("b\na").pipe(cat().pipe(tool("sort")())).run()
    assert result.stdout == "a\nb\n"


# -- A8: env, cwd, timeout -------------------------------------------------------


def test_env_overrides_and_isolation(monkeypatch):
    monkeypatch.setenv("CLICHAIN_SECRET", "s3cret")
    shown = tool("sh", env={"FOO": "bar"})("-c", "echo $FOO ${CLICHAIN_SECRET-none}")
    assert shown.run().stdout.strip() == "bar s3cret"
    isolated = tool("sh", env={"FOO": "bar"}, inherit_env=False)
    assert isolated("-c", "echo $FOO ${CLICHAIN_SECRET-none}").run().stdout.strip() == "bar none"


def test_cwd(tmp_path):
    result = tool("pwd", cwd=str(tmp_path))().run()
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(tmp_path)


def test_missing_cwd_is_a_failed_result():
    result = tool("pwd", cwd="/nonexistent/dir")().run()
    assert result.returncode == 1
    assert "/nonexistent/dir" in result.stderr


@pytest.mark.parametrize(
    "pipeline",
    [
        tool("sleep")("5"),
        tool("sleep")("5").filter(lambda line: True).pipe(cat()),
        echo("a\nb").each(lambda _: tool("sleep")("5"), workers=2),
    ],
    ids=["single", "bridged", "each"],
)
def test_timeout(pipeline):
    start = time.monotonic()
    result = pipeline.run(timeout=0.5)
    assert time.monotonic() - start < 4
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_timeout_explained():
    from clichain.errors import explain_exit

    assert "timed out" in explain_exit(124)


# -- each() details ----------------------------------------------------------------


def test_each_keeps_output_before_failure():
    def step(line: str):
        return tool("false")() if line == "c" else echo(f"ok {line}")

    result = echo("a\nb\nc\nd").each(step).run()
    assert not result.ok
    assert result.lines == ["ok a", "ok b"]


def test_each_parallel_preserves_order():
    lines = "\n".join(str(i) for i in range(40))
    result = echo(lines).each(lambda line: echo(f"n{line}"), workers=8).run()
    assert result.lines == [f"n{i}" for i in range(40)]


def test_each_preserves_leading_whitespace():
    result = tool("printf")("  a\\n").each(lambda line: echo(f"[{line}]")).run()
    assert result.lines == ["[  a]"]


def test_each_requires_pipeline():
    with pytest.raises(TypeError, match="each"):
        echo("a").each(lambda _: "not a command").run()  # type: ignore[arg-type]


def test_public_api_exports():
    for name in ("PipelineError", "DescribeExit", "Pipeline", "Cmd", "tool", "Result"):
        assert hasattr(clichain, name)


# -- Code review follow-ups ---------------------------------------------------------


def test_parallel_each_callback_error_stops_remaining_lines():
    calls: list[str] = []

    def fn(line: str):
        calls.append(line)
        if line == "0":
            raise RuntimeError("boom")
        return tool("sleep")("0.1")

    start = time.monotonic()
    with pytest.raises(RuntimeError, match="boom"):
        seq("0", "19").each(fn, workers=2).run()
    assert len(calls) < 6
    assert time.monotonic() - start < 1.5


def test_check_true_raises_when_preflight_fails():
    with pytest.raises(PipelineError) as info:
        tool("nonexistent_tool_xyz")().run(check=True)
    assert info.value.result.returncode == 1


@pytest.mark.parametrize(
    "pipeline",
    [
        sh("-c", "sleep 4; echo x"),
        sh("-c", "sleep 4; echo x").filter(lambda line: True).pipe(cat()),
        sh("-c", "sleep 4 >&2; echo x").meter(to=lambda s: None),
    ],
    ids=["direct", "bridged", "stderr"],
)
def test_timeout_is_not_blocked_by_descendants_holding_pipes(pipeline):
    start = time.monotonic()
    result = pipeline.run(timeout=0.5)
    assert result.returncode == 124
    assert time.monotonic() - start < 2.5


def test_inherit_env_false_finds_binaries_on_parent_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "only-here"
    script.write_text("#!/bin/sh\necho found\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    result = tool("only-here", inherit_env=False)().run()
    assert result.ok, result.stderr
    assert result.stdout == "found\n"


def test_stop_after_finish_does_not_change_the_result():
    from clichain.process import ProcessGroup

    group = ProcessGroup()
    group.finish()
    group.stop("timeout", 124, "late")
    assert group.stopped is None
    group.close()
