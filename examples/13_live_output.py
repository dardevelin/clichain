"""Live output for concurrent tools.

On a terminal, live="auto" shows one block per running job, redrawn in place; finished
jobs collapse to a summary line. In CI logs or when piped, each job's output is printed
together when it finishes (live=Live("auto", fallback="lines") prints lines as they come).
"""

from clichain import set_output, tool

set_output(None)

sh = tool("sh")
JOB = (  # a progress line rewritten with carriage returns, then a final line
    'for i in 1 2 3 4 5; do printf "%s: step $i/5\\r" "$0" >&2; sleep 0.15; done; '
    'echo "$0 done" >&2'
)
files = "\n".join(f"file{n}.dat" for n in range(1, 7)) + "\n"

result = tool("printf")(files).each(lambda name: sh("-c", JOB, name), workers=3).run(live="auto")
print(f"ok={result.ok}")
