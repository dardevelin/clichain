"""Per-line command execution with each()."""

from clichain import tool

echo = tool("echo")
wc = tool("wc")

# Sequential: process each line one at a time
result = (
    echo("hello\nworld\nfoo")
    .each(lambda line: echo(f"processed: {line}"))
    .run()
)

for line in result.lines:
    print(line)

# Parallel: 3 workers
result = (
    echo("a\nb\nc\nd\ne\nf")
    .each(lambda line: echo(f"[{line}]"), workers=3)
    .run()
)

print(f"\nParallel results: {result.lines}")

# each() stops on first failure
false_cmd = tool("false")
result = (
    echo("a\nb\nc")
    .each(lambda line: false_cmd())
    .run()
)

print(f"\nFailed: ok={result.ok}, rc={result.returncode}")
