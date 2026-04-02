"""Execution reports, SBOM, and error diagnostics."""

from clichain import tool

sort = tool("sort")
uniq = tool("uniq")
wc = tool("wc")
echo = tool("echo")

# --- Execution report ---
result = (
    echo("banana\napple\ncherry\napple\nbanana")
    .pipe(sort())
    .pipe(uniq("-c"))
    .pipe(sort("-rn"))
    .run()
)

print("--- report ---")
result.report()

# --- SBOM ---
print("\n--- sbom ---")
print(result.sbom_json(indent=2))

# --- Error diagnostics ---
missing = tool("nonexistent_tool_xyz", on_fail="warn")
result = missing().run()

print("\n--- explain ---")
result.explain()

# --- Describe (no execution) ---
pipeline = (
    sort()
    .from_file("/tmp/data.txt")
    .pipe(uniq("-c"))
    .redirect(stdout="/tmp/output.txt")
)

print("\n--- describe ---")
pipeline.describe()
