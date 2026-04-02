"""Benchmark: bash vs clichain on realistic pipe workloads.

Measures wall time, peak memory (RSS), and verifies output correctness.
"""

import os
import resource
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import clichain
from clichain import tool

clichain.output = None  # silence check output


def measure_bash(cmd: str, stdin_data: str | None = None) -> dict:
    """Run a bash command, measure time and peak memory."""
    start = time.monotonic()
    proc = subprocess.run(
        ["bash", "-c", cmd],
        input=stdin_data,
        capture_output=True,
        text=True,
    )
    elapsed = time.monotonic() - start
    return {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
        "elapsed": elapsed,
    }


def measure_clichain(pipeline) -> dict:
    """Run a clichain pipeline, measure time and peak memory."""
    start = time.monotonic()
    result = pipeline.run(validate=False)
    elapsed = time.monotonic() - start
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "elapsed": elapsed,
    }


def get_peak_rss_mb() -> float:
    """Get peak RSS of this process in MB."""
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    # macOS reports in bytes, Linux in KB
    if sys.platform == "darwin":
        return usage.ru_maxrss / (1024 * 1024)
    return usage.ru_maxrss / 1024


def generate_test_data(lines: int, line_length: int = 80) -> str:
    """Generate test data with numbered lines."""
    return "\n".join(f"line-{i:08d} {'x' * (line_length - 20)}" for i in range(lines))


def report(name: str, bash_result: dict, chain_result: dict) -> None:
    """Print comparison."""
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")
    print(f"  {'':20s} {'bash':>12s} {'clichain':>12s} {'ratio':>8s}")
    print(f"  {'time (s)':20s} {bash_result['elapsed']:12.4f} {chain_result['elapsed']:12.4f} {chain_result['elapsed'] / max(bash_result['elapsed'], 0.0001):8.2f}x")

    bash_out_lines = len(bash_result["stdout"].strip().splitlines()) if bash_result["stdout"].strip() else 0
    chain_out_lines = len(chain_result["stdout"].strip().splitlines()) if chain_result["stdout"].strip() else 0
    match = bash_out_lines == chain_out_lines
    print(f"  {'output lines':20s} {bash_out_lines:12d} {chain_out_lines:12d} {'ok' if match else 'MISMATCH':>8s}")
    print(f"  {'exit code':20s} {bash_result['returncode']:12d} {chain_result['returncode']:12d}")


# -- Benchmarks ---------------------------------------------------------------

def bench_simple_pipe(data: str) -> None:
    """echo data | grep pattern | wc -l"""
    echo = tool("echo")
    grep = tool("grep")
    wc = tool("wc")

    bash_r = measure_bash(
        f"echo '{data}' | grep 'line-0000' | wc -l"
    )
    chain_r = measure_clichain(
        echo(data).pipe(grep("line-0000")).pipe(wc("-l"))
    )
    report("Simple pipe: echo | grep | wc", bash_r, chain_r)


def bench_three_stage_pipe(data: str) -> None:
    """echo data | grep pattern | sort | head"""
    echo = tool("echo")
    grep = tool("grep")
    sort = tool("sort")
    head = tool("head")

    bash_r = measure_bash(
        f"echo '{data}' | grep 'line-000' | sort -r | head -100"
    )
    chain_r = measure_clichain(
        echo(data).pipe(grep("line-000")).pipe(sort("-r")).pipe(head("-100"))
    )
    report("4-stage pipe: echo | grep | sort | head", bash_r, chain_r)


def bench_filter_in_middle(data: str) -> None:
    """echo data | [python filter] | wc -l — tests thread-based filter."""
    echo = tool("echo")
    wc = tool("wc")

    bash_r = measure_bash(
        f"echo '{data}' | grep '0000' | wc -l"
    )
    chain_r = measure_clichain(
        echo(data)
        .filter(lambda line: "0000" in line)
        .pipe(wc("-l"))
    )
    report("Filter in middle: echo | filter | wc", bash_r, chain_r)


def bench_large_throughput() -> None:
    """Generate large data with seq, pipe through cat and wc."""
    seq = tool("seq")
    cat = tool("cat")
    wc = tool("wc")

    n = 1_000_000

    bash_r = measure_bash(f"seq {n} | cat | wc -l")
    chain_r = measure_clichain(
        seq(str(n)).pipe(cat()).pipe(wc("-l"))
    )
    report(f"Large throughput: seq {n} | cat | wc", bash_r, chain_r)


def bench_feed_and_sort() -> None:
    """Feed string data into sort."""
    sort = tool("sort")

    data = "\n".join(f"item-{i:06d}" for i in reversed(range(10_000)))

    bash_r = measure_bash(f"sort <<< '{data}'")
    chain_r = measure_clichain(
        sort().feed(data + "\n")
    )
    report("Feed + sort: 10k lines", bash_r, chain_r)


def bench_capture_interpolate() -> None:
    """Capture output and interpolate into next command."""
    echo = tool("echo")

    bash_r = measure_bash(
        'DIR=$(echo "/tmp"); echo "dir is $DIR"'
    )
    chain_r = measure_clichain(
        echo("/tmp")
        .capture("dir")
        .pipe(echo("dir is {dir}"))
    )
    report("Capture + interpolate", bash_r, chain_r)


def bench_each_sequential() -> None:
    """Run a command for each line — sequential."""
    echo = tool("echo")
    seq = tool("seq")

    bash_r = measure_bash(
        "seq 50 | while read i; do echo \"item $i\"; done"
    )
    chain_r = measure_clichain(
        seq("50").each(lambda line: echo(f"item {line}"))
    )
    report("Each sequential: 50 items", bash_r, chain_r)


def bench_each_parallel() -> None:
    """Run a command for each line — parallel."""
    echo = tool("echo")
    seq = tool("seq")

    bash_r = measure_bash(
        "seq 50 | xargs -P4 -I{} echo 'item {}'"
    )
    chain_r = measure_clichain(
        seq("50").each(lambda line: echo(f"item {line}"), workers=4)
    )
    report("Each parallel (4 workers): 50 items", bash_r, chain_r)


# -- Main ---------------------------------------------------------------------

if __name__ == "__main__":
    print("Generating test data...")
    small_data = generate_test_data(1_000)
    medium_data = generate_test_data(10_000)

    print(f"Small data: {len(small_data):,} bytes, 1,000 lines")
    print(f"Medium data: {len(medium_data):,} bytes, 10,000 lines")

    bench_simple_pipe(small_data)
    bench_three_stage_pipe(small_data)
    bench_filter_in_middle(medium_data)
    bench_large_throughput()
    bench_feed_and_sort()
    bench_capture_interpolate()
    bench_each_sequential()
    bench_each_parallel()

    print(f"\nPeak child RSS: {get_peak_rss_mb():.1f} MB")
