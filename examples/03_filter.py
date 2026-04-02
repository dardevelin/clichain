"""Filter pipeline output with Python logic."""

from clichain import tool

ls = tool("ls")
wc = tool("wc")

# Count only .py files in the current directory
result = (
    ls(".")
    .filter(lambda f: f.endswith(".py"))
    .pipe(wc("-l"))
    .run()
)

print(f"Python files: {result.stdout.strip()}")

# Chained filters
echo = tool("echo")
result = (
    echo("1\n2\n3\n4\n5\n6\n7\n8\n9\n10")
    .filter(lambda n: int(n) > 3)
    .filter(lambda n: int(n) < 8)
    .run()
)

print(f"Numbers between 3 and 8: {result.lines}")
