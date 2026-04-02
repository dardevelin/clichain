"""Preflight checks: versions, env vars, files."""

from clichain import env, file_exists, set_output, tool

set_output(None)  # handle output ourselves

# Version constraints on tools
bash = tool("bash", version=">=3.0")
sort = tool("sort")
grep = tool("grep")

# Check tools without running
pipeline = bash("-c", "echo hello").pipe(sort()).pipe(grep("hello"))

print("--- pipeline.check() ---")
checks = pipeline.check()
for c in checks:
    status = "ok" if c.ok else "FAIL"
    print(f"  {status}: {c.name} -> {c.found}")

# Custom checks with severity levels
echo = tool("echo")
result = echo("test").run(
    pre=[
        env("HOME", on_fail="error"),
        env("NONEXISTENT_VAR", on_fail="warn", msg="optional, defaulting to empty"),
        env("ANOTHER_MISSING", on_fail="pass"),  # silent
        file_exists("/tmp"),
        file_exists("/nonexistent/path", on_fail="warn"),
    ]
)

print("\n--- result ---")
print(f"ok: {result.ok}")
print(f"checks: {len(result.checks)}")
for c in result.checks:
    status = "ok" if c.ok else ("warn" if c.on_fail == "warn" else "FAIL")
    print(f"  [{status}] {c.name}: {c.found}")
