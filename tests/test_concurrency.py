"""Thread safety and resource safety: no shared mutable state, no leaked fds/threads/children,
bounded memory for redirected output and stderr."""

from __future__ import annotations

import io
import os
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor

import pytest

import clichain
from clichain import Live, tool

from .conftest import open_fds

echo, cat, seq, sh = tool("echo"), tool("cat"), tool("seq"), tool("sh")


# -- Thread safety -------------------------------------------------------------------


def test_one_pipeline_run_concurrently_from_many_threads():
    pipeline = (
        seq("1", "500")
        .filter(lambda line: int(line) % 2 == 0)
        .meter(to=lambda s: None, interval=0)
        .peek(fn=lambda line: None)
        .pipe(tool("wc")("-l"))
    )

    def run_once(_: int) -> str:
        return pipeline.run().stdout.strip()

    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(pool.map(run_once, range(64)))
    assert outputs == ["250"] * 64


def test_concurrent_meters_report_their_own_run():
    pipeline_stats: dict[int, list] = {}

    def run_once(n: int) -> None:
        mine: list = []
        seq("1", str(n)).meter(to=mine.append, interval=0).run()
        pipeline_stats[n] = mine

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run_once, range(10, 50)))
    for n, stats in pipeline_stats.items():
        assert stats[-1].lines == n


def test_output_lines_are_never_interleaved_or_lost():
    lines: list[str] = []
    lock = threading.Lock()

    def sink(msg: str) -> None:
        with lock:
            lines.append(msg)

    clichain.set_output(sink)
    pipeline = seq("1", "200").peek(label="p").pipe(cat())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: pipeline.run(validate=False), range(8)))
    assert len(lines) == 8 * 200
    assert all(line.startswith("[p] ") for line in lines)


def test_parallel_each_under_load():
    data = "\n".join(str(i) for i in range(300))
    result = echo(data).each(lambda line: echo(line).filter(lambda x: True), workers=16).run()
    assert result.lines == [str(i) for i in range(300)]


# -- Resource safety --------------------------------------------------------------


def _battery(tmp_path) -> None:
    """Every success and failure path, including the ones that used to hang."""

    def boom(_):
        raise RuntimeError("boom")

    out = str(tmp_path / "out.txt")
    echo("a\nb").pipe(cat()).filter(lambda line: True).meter().peek(fn=lambda line: None).run()
    tool("yes")().filter(lambda line: True).pipe(tool("head")("-1")).run()
    echo("x").pipe(tool("nonexistent_tool_xyz")()).pipe(cat()).run(validate=False)
    cat().from_file("/nonexistent").pipe(cat()).run()
    seq("1", "50").from_file(__file__, block_size=64).pipe(cat()).run()
    sh("-c", "echo out; echo err >&2").redirect(stdout=out, stderr=out + ".err").run()
    tool("sleep")("5").filter(lambda line: True).run(timeout=0.2)
    echo("a\nb").each(lambda line: tool("false")(), workers=2).run()
    with pytest.raises(RuntimeError):
        seq("1", "100000").filter(boom).pipe(cat()).run()
    tool("false")().feed("data").run()
    # live output and pseudo-terminals: renderer threads and pty fds must be released too
    shown = io.StringIO()
    tool("sh", tty=True)("-c", "echo progress >&2").run(live=Live("blocks", stream=shown))
    echo("a\nb").each(lambda line: sh("-c", "echo $0 >&2", line), workers=2).run(
        live=Live("lines", stream=shown)
    )
    tool("sh", tty=True)("-c", "sleep 5").run(timeout=0.2, live=Live("grouped", stream=shown))


def test_no_leaked_fds_threads_or_children(tmp_path):
    _battery(tmp_path)  # warm-up: lazy imports and caches open nothing later
    fds, threads = open_fds(), threading.active_count()
    for _ in range(3):
        _battery(tmp_path)
    assert open_fds() == fds
    assert threading.active_count() == threads
    with pytest.raises(ChildProcessError):
        os.waitpid(-1, os.WNOHANG)  # nothing left unreaped


def test_redirected_output_is_not_buffered_in_python(tmp_path):
    """Peak Python memory stays flat while the redirected output doubles."""
    out = tmp_path / "big.txt"

    def peak_for(lines: int) -> int:
        tracemalloc.start()
        try:
            pipeline = seq("1", str(lines)).filter(lambda line: True)
            assert pipeline.redirect(stdout=str(out)).run().ok
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    small, large = peak_for(1_500_000), peak_for(3_000_000)
    assert out.stat().st_size > 20_000_000
    assert large < small * 1.25  # bounded by the block size, not the output size
    assert large < out.stat().st_size / 4


def test_captured_stderr_is_bounded():
    result = sh("-c", "head -c 8000000 /dev/zero | tr '\\0' e >&2").run()
    assert result.ok
    assert len(result.stderr) < 1_200_000
    assert "truncated" in result.stderr


def test_stop_does_not_close_fds_under_running_threads():
    # Timeouts while bridges are mid-stream must never surface EBADF in threads
    # (the autouse fixture fails the test on any unhandled thread exception).
    for _ in range(20):
        seq("1", "10000000").filter(lambda line: True).peek(fn=lambda line: None).pipe(cat()).run(
            timeout=0.05
        )
    time.sleep(0.1)
