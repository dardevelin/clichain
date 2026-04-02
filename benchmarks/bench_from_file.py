"""Benchmark: from_file with and without block_size.

Compares:
  1. bash: cmd < file
  2. clichain: from_file (kernel, block_size=0)
  3. clichain: from_file (thread, block_size=4096)
  4. clichain: from_file (thread, block_size=65536)
  5. clichain: from_file (thread, block_size=1048576)

Tests with different file sizes and both text and binary workloads.
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

clichain.set_output(None)


def measure_bash(cmd: str) -> dict:
    start = time.monotonic()
    proc = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
    )
    elapsed = time.monotonic() - start
    return {
        "stdout_len": len(proc.stdout),
        "returncode": proc.returncode,
        "elapsed": elapsed,
    }


def measure_clichain(pipeline) -> dict:
    start = time.monotonic()
    result = pipeline.run(validate=False)
    elapsed = time.monotonic() - start
    return {
        "stdout_len": len(result.stdout),
        "returncode": result.returncode,
        "elapsed": elapsed,
    }


def generate_text_file(path: str, lines: int, line_len: int = 80) -> int:
    with open(path, "w") as f:
        for i in range(lines):
            f.write(f"{i:08d} {'x' * (line_len - 10)}\n")
    return os.path.getsize(path)


def generate_binary_file(path: str, size_mb: int) -> int:
    with open(path, "wb") as f:
        chunk = os.urandom(65536)
        written = 0
        target = size_mb * 1024 * 1024
        while written < target:
            to_write = min(len(chunk), target - written)
            f.write(chunk[:to_write])
            written += to_write
    return os.path.getsize(path)


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def fmt_rate(size: int, elapsed: float) -> str:
    if elapsed == 0:
        return "-"
    rate = size / elapsed
    return f"{fmt_size(int(rate))}/s"


def report_row(label: str, size: int, result: dict) -> None:
    e = result["elapsed"]
    rate = fmt_rate(size, e)
    ok = "ok" if result["returncode"] == 0 else "FAIL"
    print(f"  {label:<30s}  {e:8.4f}s  {rate:>12s}  {ok}")


def bench_text(lines: int) -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        path = f.name
    size = generate_text_file(path, lines)

    wc = tool("wc")
    shasum = tool("shasum")

    print(f"\n  Text file: {fmt_size(size)}, {lines:,} lines")
    print(f"  {'method':<30s}  {'time':>8s}  {'throughput':>12s}  status")
    print("  " + "-" * 60)

    # bash baseline
    r = measure_bash(f"wc -l < {path}")
    report_row("bash: wc -l < file", size, r)

    # kernel (block_size=0)
    r = measure_clichain(wc("-l").from_file(path))
    report_row("clichain: kernel (bs=0)", size, r)

    # block_size variants
    for bs in [4096, 65536, 1048576]:
        r = measure_clichain(wc("-l").from_file(path, block_size=bs))
        report_row(f"clichain: thread (bs={bs})", size, r)

    # shasum — more CPU work per byte
    print()
    print(f"  {'method':<30s}  {'time':>8s}  {'throughput':>12s}  status")
    print("  " + "-" * 60)

    r = measure_bash(f"shasum -a 256 < {path}")
    report_row("bash: shasum < file", size, r)

    r = measure_clichain(shasum("-a", "256").from_file(path))
    report_row("clichain: kernel (bs=0)", size, r)

    for bs in [4096, 65536, 1048576]:
        r = measure_clichain(
            shasum("-a", "256").from_file(path, block_size=bs)
        )
        report_row(f"clichain: thread (bs={bs})", size, r)

    os.unlink(path)


def bench_pipe_chain(lines: int) -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        path = f.name
    size = generate_text_file(path, lines)

    grep = tool("grep")
    sort = tool("sort")
    wc = tool("wc")

    print(f"\n  Pipe chain: {fmt_size(size)}, {lines:,} lines")
    print(f"  cmd: grep '0000' | sort | wc -l")
    print(f"  {'method':<30s}  {'time':>8s}  {'throughput':>12s}  status")
    print("  " + "-" * 60)

    r = measure_bash(f"grep '0000' < {path} | sort | wc -l")
    report_row("bash", size, r)

    r = measure_clichain(
        grep("0000").from_file(path).pipe(sort()).pipe(wc("-l"))
    )
    report_row("clichain: kernel (bs=0)", size, r)

    for bs in [4096, 65536]:
        r = measure_clichain(
            grep("0000")
            .from_file(path, block_size=bs)
            .pipe(sort())
            .pipe(wc("-l"))
        )
        report_row(f"clichain: thread (bs={bs})", size, r)

    os.unlink(path)


def bench_memory(lines: int) -> None:
    """Check if block_size actually limits memory usage."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        path = f.name
    size = generate_text_file(path, lines)

    cat = tool("cat")

    print(f"\n  Memory test: {fmt_size(size)}, {lines:,} lines")
    print(f"  {'method':<30s}  {'time':>8s}  {'output len':>12s}  status")
    print("  " + "-" * 60)

    # Kernel — file descriptor passed directly, minimal Python memory
    r = measure_clichain(cat().from_file(path))
    report_row(f"kernel (bs=0) out={r['stdout_len']}", size, r)

    # Thread with small block — Python mediates in chunks
    for bs in [4096, 65536]:
        r = measure_clichain(cat().from_file(path, block_size=bs))
        report_row(
            f"thread (bs={bs}) out={r['stdout_len']}", size, r,
        )

    os.unlink(path)


if __name__ == "__main__":
    print("=" * 66)
    print("  from_file benchmark: kernel vs block_size")
    print("=" * 66)

    bench_text(10_000)
    bench_text(100_000)
    bench_pipe_chain(100_000)
    bench_memory(100_000)

    peak = resource.getrusage(resource.RUSAGE_CHILDREN)
    if sys.platform == "darwin":
        rss = peak.ru_maxrss / (1024 * 1024)
    else:
        rss = peak.ru_maxrss / 1024
    print(f"\n  Peak child RSS: {rss:.1f} MB")
    print("=" * 66)
