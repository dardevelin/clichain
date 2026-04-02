"""Throughput monitoring with meter()."""

import sys

from clichain import set_output, tool

set_output(sys.stdout)

seq = tool("seq")
sort = tool("sort")
wc = tool("wc")

# Meter between steps -- reports throughput to clichain.output
result = (
    seq("100000")
    .meter(label="raw")
    .pipe(sort("-rn"))
    .meter(label="sorted")
    .pipe(wc("-l"))
    .run(validate=False)
)

print(f"Lines: {result.stdout.strip()}")

# Custom callback -- collect stats instead of printing
echo = tool("echo")
stats_log: list = []

result = (
    echo("a\nb\nc\nd\ne")
    .meter(
        to=lambda stats: stats_log.append({"bytes": stats.bytes, "lines": stats.lines}),
        interval=0,  # report on every line
    )
    .run(validate=False)
)

print(f"\nCollected {len(stats_log)} stats snapshots")
print(f"Final: {stats_log[-1]}")

# Meter adds bytes column to report
result.report()
