"""File input and output: from_file, redirect, feed."""

import tempfile
import os

from clichain import tool

sort = tool("sort")
uniq = tool("uniq")
wc = tool("wc")

# Create test data
tmpdir = tempfile.mkdtemp()
input_file = os.path.join(tmpdir, "words.txt")
output_file = os.path.join(tmpdir, "counted.txt")

with open(input_file, "w") as f:
    f.write("banana\napple\ncherry\napple\nbanana\napple\n")

# bash: sort < words.txt | uniq -c | sort -rn > counted.txt
result = (
    sort()
    .from_file(input_file)
    .pipe(uniq("-c"))
    .pipe(sort("-rn"))
    .redirect(stdout=output_file)
    .run()
)

print(f"ok: {result.ok}")
with open(output_file) as f:
    print(f.read())

# feed: provide a string as stdin
result = sort().feed("cherry\napple\nbanana\n").run()
print(f"Sorted: {result.lines}")

# Append mode
echo = tool("echo")
log_file = os.path.join(tmpdir, "log.txt")
echo("first entry").redirect(stdout=log_file).run()
echo("second entry").redirect(stdout=log_file, append=True).run()

with open(log_file) as f:
    print(f"Log:\n{f.read()}")
