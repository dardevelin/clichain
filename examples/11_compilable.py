"""A script designed to be compiled to a standalone binary.

clichain compile examples/11_compilable.py -o log_analyzer
./log_analyzer
CLICHAIN_DESCRIBE=describe ./log_analyzer
"""

import os
import tempfile

from clichain import set_output, tool

set_output(None)

sort = tool("sort")
uniq = tool("uniq")
head = tool("head")
echo = tool("echo")

# Simulate a log file
tmpdir = tempfile.mkdtemp()
log_file = os.path.join(tmpdir, "access.log")

with open(log_file, "w") as f:
    for status in ["200", "200", "404", "200", "500", "404", "200", "301", "500", "200"]:
        f.write(f"{status}\n")

# Count status codes, show top 3
result = sort().from_file(log_file).pipe(uniq("-c")).pipe(sort("-rn")).pipe(head("-3")).run()

for line in result.lines:
    print(line.strip())
