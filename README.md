<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/dardevelin/clichain/main/logos/clichain-horizontal-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/dardevelin/clichain/main/logos/clichain-horizontal-light.svg">
    <img alt="clichain" src="https://raw.githubusercontent.com/dardevelin/clichain/main/logos/clichain-horizontal-dark.svg" width="340">
  </picture>
</p>

<p align="center">
  Fluent CLI tool chaining for Python.<br>
  <code>pip install cmdchain</code>
</p>

<p align="center">
  <a href="https://github.com/dardevelin/clichain/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/dardevelin/clichain/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://pypi.org/project/cmdchain/"><img alt="PyPI" src="https://img.shields.io/pypi/v/cmdchain"></a>
  <a href="https://pypi.org/project/cmdchain/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/cmdchain"></a>
  <a href="LICENSE-MIT"><img alt="License" src="https://img.shields.io/badge/license-MIT%2FApache--2.0-blue"></a>
</p>

<p align="center">
  <a href="#install">Install</a> &middot;
  <a href="#quickstart">Quickstart</a> &middot;
  <a href="#api-reference">API</a> &middot;
  <a href="#cli">CLI</a> &middot;
  <a href="#performance">Performance</a> &middot;
  <a href="#security">Security</a> &middot;
  <a href="#license">License</a>
</p>

---

clichain replaces bash scripts with typed, inspectable, lazy-evaluated pipelines
of CLI commands connected by real kernel pipes. Zero runtime dependencies. Python 3.11+, POSIX
(Linux, macOS).

```bash
pip install cmdchain
```

```python
from clichain import tool  # module name is clichain

echo = tool("echo")
grep = tool("grep")
sort = tool("sort")
wc   = tool("wc")

result = (
    echo("cherry\napple\nbanana\napricot")
    .pipe(grep("a"))
    .pipe(sort())
    .pipe(wc("-l"))
    .run()
)

print(result.stdout.strip())  # 3
```

Nothing executes until `.run()`. The chain is data -- a list of step descriptions
that can be inspected, validated, and reused before any process spawns.

## Why not bash?

Bash works. Until it doesn't. Quoting breaks, error handling is `set -e` and hope,
debugging is `echo` statements, and CI failures give you exit code 1 with no context.

clichain gives you the same pipe-based execution model with:

- **Type safety** -- args are lists, never shell-interpreted
- **Streaming** -- kernel pipes between commands, same as bash `|`
- **Preflight checks** -- verify tools exist and versions match before execution
- **Error diagnostics** -- error codes, execution traces, source locations
- **Profiling** -- time, lines, bytes, spawns per step
- **Run records** -- a manifest of every execution, and a CycloneDX SBOM of the tools it used
- **Compilation** -- bundle scripts to single binaries for CI

## Install

The PyPI package name is **cmdchain**, the Python module is **clichain**.

```bash
pip install cmdchain
```

```python
from clichain import tool
```

Zero runtime dependencies. Python 3.11+ (including free-threaded builds) on Linux and macOS.

For compiling scripts to standalone binaries:

```bash
pip install cmdchain[compile]
```

## Development setup

Clone the repository and install it with the locked dev tooling, using
[uv](https://docs.astral.sh/uv/) (what CI uses):

```bash
git clone https://github.com/dardevelin/clichain.git
cd clichain
uv sync                          # .venv with clichain (editable) + dev tools from uv.lock
```

or with pip 25.1 or later:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e . --group dev
```

This gives you pytest (with pytest-timeout), coverage, ruff and basedpyright.
Run the full check suite:

```bash
uv run ruff check src/ tests/ examples/           # lint
uv run ruff format --check src/ tests/ examples/  # format check
uv run basedpyright src/ tests/                   # type check
uv run pytest                                     # tests (warnings are errors, 60 s timeout each)
uv run coverage run -m pytest && uv run coverage report
```

Compile end-to-end tests require pyinstaller and are skipped without it:

```bash
uv sync --extra compile
uv run pytest tests/test_compile.py
```

### Project structure

```
src/clichain/
  __init__.py     # public API
  pipeline.py     # tool(), Cmd, Pipeline (immutable builder) and run()
  steps.py        # step types: what each step is, its label and manifest node
  execute.py      # planner and executor: streaming groups, bridges, each()
  process.py      # child process supervision, timeouts, signal handling
  checks.py       # preflight checks and version constraints
  result.py       # Result, StepProfile, MeterStats
  render.py       # output destination; explain / report / describe text
  manifest.py     # tool and file inventory, run manifest, CycloneDX SBOM
  errors.py       # error codes, PipelineError, DescribeExit
  cli.py          # CLI: explain, check, compile
tests/            # one file per concern; hang- and signal-prone cases run in subprocesses
examples/         # runnable examples covering all features
benchmarks/       # performance comparisons vs bash
```

## Quickstart

### Basic pipe

```python
from clichain import tool

echo = tool("echo")
grep = tool("grep")

result = echo("hello\nworld").pipe(grep("hello")).run()
print(result.stdout)  # hello\n
print(result.ok)      # True
```

### Multi-stage pipe

Consecutive `.pipe()` calls are connected by kernel pipes -- all processes run concurrently,
data streams through without Python buffering:

```python
result = (
    echo("foo\nbar\nbaz")
    .pipe(grep("ba"))
    .pipe(sort())
    .pipe(wc("-l"))
    .run()
)
```

### Adding arguments

`tool()` returns a callable. Call it with arguments to get a command ready to execute:

```python
grep = tool("grep")

# These are equivalent:
grep("-r", "-i", "pattern", ".")
grep("-ri", "pattern", ".")
```

Arguments are passed as a list to `subprocess.Popen` -- never through a shell.
No quoting issues, no injection.

### Capture and interpolate

Capture a step's output and reference it in later arguments with `{name}`:

```python
pwd = tool("pwd")
ls  = tool("ls")

result = (
    pwd()
    .capture("cwd")
    .pipe(ls("{cwd}"))
    .run()
)
```

`.capture()` buffers the output and strips whitespace. In later arguments, `{name}` is
replaced for names captured so far; every other brace is literal, so
`awk("{print $1}")` and `find(..., "-exec", "rm", "{}", ";")` work unchanged.

Captured values become arguments verbatim. A value starting with `-` would be read
as an option by most tools; put `"--"` before it, or have the run refuse such values:

```python
find().capture("target", reject_options=True).pipe(rm("--", "{target}"))
```

### Filter with Python logic

`.filter()` receives each line without its terminator (`\n` or `\r\n`) and keeps lines where
the function returns True. Kept lines pass through byte-for-byte. It streams through a
thread -- no full buffering:

```python
ls = tool("ls")
wc = tool("wc")

result = (
    ls(".")
    .filter(lambda f: f.endswith(".py"))
    .pipe(wc("-l"))
    .run()
)
```

Filters can be chained:

```python
result = (
    echo("1\n2\n3\n4\n5")
    .filter(lambda line: int(line) > 2)
    .filter(lambda line: int(line) < 5)
    .run()
)
# result.lines == ["3", "4"]
```

### Peek

Observe data flowing through without modifying it. Useful for debugging:

```python
seen = []
result = (
    echo("hello\nworld")
    .peek(fn=lambda line: seen.append(line))
    .pipe(wc("-l"))
    .run()
)
# seen == ["hello\n", "world\n"]
# result.stdout.strip() == "2"  -- data passed through unchanged
```

With a label (prints to the configured output destination):

```python
.peek(label="after filter")
# [after filter] hello
# [after filter] world
```

### Feed

Provide a string, or bytes, as the pipeline's stdin. Like `.from_file()`, it sets the input of the
whole pipeline wherever it appears in the chain, and a pipeline can have only one input
source:

```python
sort = tool("sort")

result = sort().feed("cherry\napple\nbanana\n").run()
# result.lines == ["apple", "banana", "cherry"]
```

### File I/O

**Read from file** -- the file descriptor is passed directly to the process.
The kernel handles buffering, no Python overhead:

```python
sort = tool("sort")

# bash: sort < input.txt
sort().from_file("input.txt").run()
```

With explicit block size -- a thread reads N bytes at a time.
Useful when Python needs to mediate (e.g., meter on binary data).
~5-10% throughput cost vs kernel mode:

```python
sort().from_file("large.bin", block_size=65536).run()
```

**Write to file.** A redirect right after streaming steps streams straight to the file,
so large outputs never pass through Python memory. What is redirected is consumed:
it no longer appears in `result.stdout` / `result.stderr`. `stderr=` receives the
stderr of every command up to that point.

```python
echo = tool("echo")
sort = tool("sort")
make = tool("make")

# bash: sort < input.txt > output.txt
sort().from_file("input.txt").redirect(stdout="output.txt").run()

# Append mode: >>
echo("new line").redirect(stdout="log.txt", append=True).run()

# Separate stderr:
make().redirect(stdout="build.log", stderr="errors.log").run()
```

### Merge stderr

Combine stderr into stdout (bash `2>&1`):

```python
bash = tool("bash")
result = bash("-c", "echo out; echo err >&2").merge_stderr().run()
# result.stdout contains both "out" and "err"
```

### Collect

Explicit buffering boundary. Forces all data to be collected in memory before
the next step proceeds:

```python
echo = tool("echo")
result = echo("hello").collect().run()
```

Most useful before `.each()` when you want to ensure all input is gathered
before spawning per-line commands.

### Per-line execution with each

`.each()` runs a command for every line of input. Each line becomes different arguments --
a separate process per line:

```python
find   = tool("find")
ffmpeg = tool("ffmpeg")

# Convert all .mkv files to .mp3
(
    find(".", "-name", "*.mkv")
    .each(lambda f: ffmpeg("-i", f, "-vn", f.replace(".mkv", ".mp3")))
    .run()
)
```

**Parallel execution** with `workers`:

```python
# 4 ffmpeg processes at a time
.each(lambda f: ffmpeg("-i", f, "-vn", f + ".mp3"), workers=4)
```

`.each()` stops on first failure -- remaining lines are skipped.

**When to use `.each()` vs `.pipe()`:**
- `.each()` -- command args change per line (different `-i` file each time)
- `.pipe()` -- command reads stdin line by line (grep, sort, awk)

`.pipe()` is one process, streaming. `.each()` is one process per line.

### Meter

Count bytes and lines flowing through the pipeline. Streams line-by-line,
reports throughput periodically:

```python
seq  = tool("seq")
sort = tool("sort")
wc   = tool("wc")

result = (
    seq("1000000")
    .meter(label="raw")
    .pipe(sort("-rn"))
    .meter(label="sorted")
    .pipe(wc("-l"))
    .run()
)
```

Parameters:
- `label` -- prefix for the output
- `interval` -- seconds between reports (default `1.0`, use `0` for every line)
- `to` -- custom destination: `None` (uses configured output), a callable receiving `MeterStats`, or a file-like object

```python
# Custom callback -- collect stats programmatically
stats_log = []
echo("a\nb\nc").meter(to=lambda stats: stats_log.append(stats), interval=0).run()

# MeterStats fields:
#   stats.label          str
#   stats.bytes          int    -- total bytes seen
#   stats.lines          int    -- total lines seen
#   stats.elapsed        float  -- seconds since start
#   stats.bytes_per_sec  float
#   stats.lines_per_sec  float
```

Every profile row reports `bytes_in` / `bytes_out`; meters add live throughput on top.
Without meter, the column is hidden -- no overhead unless you ask for it.

### Live output

By default `run()` shows nothing while it runs: stderr is captured into the `Result`.
Pass `live=` to watch concurrent tools without their output mangling each other.
Each job (a run, or one `.each()` line) gets its own buffer:

```python
pipeline.run(live="auto")   # blocks on a terminal, grouped output in logs
```

| `live=` | What you see |
|---|---|
| `"blocks"` | One region per running job, redrawn in place: its last lines, with progress bars updating inside the block. Finished jobs collapse to `✓ label  1.2s` (a failure keeps its last lines) |
| `"grouped"` | Each job's output printed together when it finishes; collapsible `::group::` sections on GitHub Actions |
| `"lines"` | Every complete line as it arrives, tagged `[job]`: live, never mixed mid-line |
| `"auto"` | `blocks` on a terminal, otherwise the fallback (`grouped` by default) |
| `None` / `"capture"` | Nothing live (the default) |

```
✓ each[1] a.mkv → ffmpeg -i a.mkv -vn a.mp3         12.1s
⠼ each[2] b.mkv → ffmpeg -i b.mkv -vn b.mp3          8.3s
│ size=   10240kB time=00:05:12.34 bitrate= 268.6kbits/s speed=37.9x
⠼ each[3] c.mkv → ffmpeg -i c.mkv -vn c.mp3          3.0s
│ size=    2048kB time=00:01:01.02 bitrate= 274.9kbits/s speed=20.1x
1 done · 3 running · 0 failed
```

`Live(...)` sets the rest: `Live("auto", fallback="lines", unit="process", tail=5,
stdout=True, stream=sys.stdout)`. `unit="process"` gives every process its own block
instead of every job; `stdout=True` also shows the output that reaches Python.

Many tools (git, curl, rsync) only draw progress on a terminal. `tool("rsync",
tty=True)` gives the command's stderr a pseudo-terminal so its progress shows up in
its block; stdout stays a pipe, so the data is never altered.

While blocks are drawn, clichain's own messages (checks, peek, meter), including those
sent to another terminal, appear above the live area. Output you `print()` yourself
during a run would draw over it: send it through `set_output()` instead. Escape
sequences from tools are stripped (colour is kept), wide characters are measured in
terminal cells, and if the display itself fails (a closed or broken stream), live output
stops while the run carries on.

### Version constraints

```python
ffmpeg = tool("ffmpeg", version=">=6.0", msg="brew install ffmpeg")
jq     = tool("jq", version=">=1.7")
```

**`tool()` parameters:**
- `name` -- binary name (looked up on `$PATH`)
- `version` -- version constraint, e.g. `>=6.0,<8.0` (see above)
- `on_fail` -- what to do if the check fails: `"error"` (default, stops execution), `"warn"` (logs, continues), `"pass"` (silent, continues)
- `msg` -- custom message shown on failure

Constraints are validated when the tool is defined: `>=`, `<=`, `>`, `<`, `==`, `!=`
and `~=` (compatible release), comma-separated. `==1.7` matches `1.7.0`.

The version is detected by running `<tool> --version` once per binary, with stdin
closed, and taking the first `N.N[.N]` it prints. Nothing else is ever passed, so tools
without a version flag cannot mistake the probe for an operand. Use `version_args` for
tools with a different flag, e.g. `tool("ffmpeg", version=">=6", version_args=("-version",))`.
If the version cannot be determined, the check fails (`on_fail="warn"` to proceed anyway).

### Preflight checks

Binary checks run automatically before execution by default. You can also add
custom checks for environment variables, files, or arbitrary conditions:

```python
from clichain import env, file_exists

result = pipeline.run(pre=[
    env("API_KEY", on_fail="error", msg="set in .env"),
    env("DEBUG", on_fail="warn"),
    file_exists("input.mkv"),
])
```

**Check levels:**
| Level | Stops execution | Shows message |
|---|---|---|
| `error` | yes | yes |
| `warn` | no | yes |
| `pass` | no | no |

**Skip all validation:**

```python
result = pipeline.run(validate=False)
```

**Check without running:**

```python
checks = pipeline.check()  # returns list[CheckResult]
for c in checks:
    print(c.ok, c.name, c.found, c.msg)
```

`CheckResult` fields: `ok`, `name`, `expected`, `found`, `msg`, `on_fail`.

## API reference

### tool()

```python
tool(
    name: str,
    version: str | None = None,
    on_fail: "error" | "warn" | "pass" = "error",
    msg: str = "",
    *,
    version_args: Iterable[str] = ("--version",),  # how to ask the tool for its version
    ok_codes: Iterable[int] = (0,),                 # exit statuses that are success, e.g. {0, 1}
    tty: bool = False,                              # stderr on a pseudo-terminal (progress bars)
    env: Mapping[str, str] | None = None,           # variables set for this command
    inherit_env: bool = True,                       # False: start from an empty environment
                                                    # (the binary is still found on your PATH)
    cwd: str | PathLike | None = None,              # working directory for this command
) -> Cmd
```

Some tools use a non-zero exit for an expected outcome: `grep` exits 1 when nothing
matches, `diff` when files differ. List those statuses in `ok_codes` instead of
appending `|| true`:

```python
grep = tool("grep", ok_codes={0, 1})   # no match is not a failure; 2 (an error) still is
```

`Cmd` is a one-step `Pipeline`. Pipelines are immutable: every method returns a new
pipeline, so a shared prefix can be branched, reused and run from several threads.

Creates a command wrapper for a binary. Call the result with arguments:

```python
grep = tool("grep")
grep("-ri", "pattern", ".")  # returns a new Cmd with those args
```

### Pipeline primitives

| Method | What it does | Streaming |
|---|---|---|
| `.pipe(cmd)` | Connect stdout to stdin via kernel pipe (a `Cmd` or a `Pipeline`) | yes |
| `.filter(fn)` | Keep lines where `fn(line)` is True | yes, via thread |
| `.each(fn, workers=1)` | Run command per input line, stops on first failure | per-line spawn |
| `.peek(label=, fn=)` | Observe data without modifying | yes, via thread |
| `.meter(label=, to=, interval=1.0)` | Count bytes/lines, report throughput | yes, via thread |
| `.collect()` | Explicit buffering boundary | no |
| `.capture(name, reject_options=False)` | Buffer and bind to `{name}` for interpolation | no |
| `.feed(data)` | String as the pipeline's stdin | yes, via thread |
| `.from_file(path, block_size=0)` | File as the pipeline's stdin (`0` = kernel, `N` = chunked thread) | yes |
| `.redirect(stdout=, stderr=, append=False)` | Write to file (consumes what it writes) | yes |
| `.merge_stderr()` | Combine stderr into stdout (2>&1) | yes |

### Execution and inspection

| Method | What it does |
|---|---|
| `.run(validate=True, pre=None, *, timeout=None, check=False, encoding="utf-8", errors="surrogateescape", ignore_sigpipe=True, output=..., live=None)` | Execute pipeline, returns `Result` |
| `.check()` | Validate binaries exist, returns `list[CheckResult]` |
| `.describe()` | Print steps and requirements without executing |

`run()` options:
- `timeout` -- seconds; when exceeded every process is stopped and `returncode` is 124
- `check` -- raise `PipelineError` (with `.result`) instead of returning a failed `Result`
- `encoding` / `errors` -- how bytes become text. The default `surrogateescape` round-trips
  any bytes, so binary or non-UTF-8 data is never lost (`result.stdout_bytes`)
- `ignore_sigpipe` -- a writer killed by SIGPIPE because its reader stopped early (`yes | head -1`)
  is not a failure, as in bash. `False` reports it
- `output` -- where this run's check/peek/meter output goes (default: `set_output()`)
- `live` -- show output while running (see [Live output](#live-output); default: nothing)

**Error model.** Anything the commands or the environment do -- non-zero exits, signals,
missing binaries (127), missing input files or unwritable redirects (1), timeouts (124) --
produces a failed `Result`. Exceptions raised by your own callbacks (`filter`, `peek`,
`meter(to=...)`, `each`) propagate out of `run()` after every child process is cleaned up.

### Result

```python
result = pipeline.run()

# Status
result.ok            # bool: returncode == 0
result.returncode    # int: exit code or negative signal number
result.stdout        # str: captured output
result.stderr        # str: captured errors
result.lines         # list[str]: stdout split by newline, stripped
result.stdout_bytes  # bytes: stdout exactly as the pipeline produced it

# Diagnostics
result.checks        # list[CheckResult]: preflight results
result.profile       # list[StepProfile]: timing per step group
result.elapsed       # float: total seconds

# Methods
result.report()      # print full execution report
result.explain()     # print error diagnosis with trace
result.manifest(redact=False)     # dict: run record (see below)
result.manifest_json(indent=2)    # str: run record as JSON
result.sbom()                     # dict: CycloneDX 1.6 SBOM of the tools used
result.sbom_json(indent=2)        # str: SBOM as JSON
```

**`StepProfile` fields:**
- `name` -- step group description
- `elapsed` -- seconds
- `lines_in`, `lines_out` -- line counts at group boundaries
- `spawns` -- number of processes created
- `bytes_in`, `bytes_out` -- bytes entering the group and bytes it produced (for a redirect: how much the file grew)

### Output control

```python
import sys
import logging
import clichain

clichain.set_output(sys.stderr)            # default -- stderr
clichain.set_output(None)                  # silent -- no output
clichain.set_output(logging.info)          # callable -- receives each line as a string
clichain.set_output(open("log.txt", "a"))  # file-like -- anything with .write()
```

All library output goes through this single destination: check results, peek output,
meter stats, reports, and error explanations. Set once at the top of your script, or
per run with `run(output=...)`. Lines are written whole even when several threads emit
at once.

### Describe

Inspect what a pipeline will do without executing:

```python
pipeline.describe()
```

```
========================================================================
  pipeline: 3 steps
------------------------------------------------------------------------
  [0] FromFile   < input.txt
  [1] Cmd        sort
  [2] Cmd        wc -l
------------------------------------------------------------------------
  requires:
    sort             /usr/bin/sort                  ok
    wc               /usr/bin/wc                    ok
------------------------------------------------------------------------
  < input.txt
========================================================================
```

Available via environment variable -- works with compiled binaries:

```bash
CLICHAIN_DESCRIBE=describe ./my_binary   # human-readable (also 1, true, yes, text)
CLICHAIN_DESCRIBE=json ./my_binary       # machine-readable JSON
```

Any other value (`0`, `false`, empty) runs normally. In describe mode the first `run()`
prints the description and raises `clichain.DescribeExit` (a `SystemExit` with code 0),
so nothing executes. The variable is removed from the environment of child processes.

The JSON output includes generator info, environment (OS, arch), steps with types and names,
tools with paths/versions/found status, and files read/written.

### Error diagnostics

```python
result.explain()
```

```
error[X2]: exit 2 — misuse of shell builtin / invalid arguments

  execution trace:
    [0] Cmd        seq 100000
    [1] Filter     filter at script.py:5
    [2] Cmd        sort --bogus  <--
    [3] Cmd        head -10
    [4] Cmd        wc -l

  source: script.py:10

  The command received invalid arguments. Check the flags and
  parameters passed to the command.
```

The execution trace shows every step, marks the failure with `<--`, and includes
source locations for Python callables (filter/each lambdas) and the `.run()` call site.

Error codes follow the pattern `S{n}` for signals, `X{n}` for exit codes.
Each has a registry entry with a detailed explanation:

| Code | Meaning |
|---|---|
| `S2` | SIGINT -- interrupted (ctrl+c) |
| `S9` | SIGKILL -- killed (OOM killer, timeout) |
| `S11` | SIGSEGV -- segmentation fault (bug in the tool) |
| `S13` | SIGPIPE -- broken pipe (downstream closed early; only reported with `ignore_sigpipe=False`) |
| `S15` | SIGTERM -- terminated (process manager, CI shutdown) |
| `X1` | General error -- check stderr |
| `X2` | Invalid arguments |
| `X124` | Timed out (`run(timeout=...)`) |
| `X126` | Permission denied -- binary not executable |
| `X127` | Command not found |

### Execution report

```python
result.report()
```

```
========================================================================
  clichain 0.2.0  |  python 3.15.2  |  Darwin 25.4.0 arm64
  hostname  /path/to/project
  2026-04-02T08:56:47.959010+00:00
  OK  0.0069s
========================================================================
  tool                    version   path
------------------------------------------------------------------------
  sort                        2.3   /usr/bin/sort
  uniq                          -   /usr/bin/uniq
------------------------------------------------------------------------
  < /tmp/input.txt
  > /tmp/output.txt
------------------------------------------------------------------------
    #      time       in      out  spawn  command
------------------------------------------------------------------------
    0    0.007s        0        3      3  < input.txt | sort | uniq -c
                                       | sort -rn
    1    0.000s        3        3      0  redirect > output.txt
------------------------------------------------------------------------
         0.007s                           total
------------------------------------------------------------------------
  [ok] sort: /usr/bin/sort
  [ok] uniq: /usr/bin/uniq
========================================================================
```

Long command chains wrap at pipe (`|`) boundaries to stay within 72 columns.
The `bytes` column is what each group produced.

### Run manifest and SBOM

Every execution produces a run manifest -- what ran, where, with which tools and files:

```python
result.manifest()                 # dict
result.manifest_json(indent=4)    # formatted JSON string
result.manifest(redact=True)      # without hostname, cwd, and the directories of absolute paths
```

```json
{
  "generator": {
    "name": "clichain",
    "version": "0.2.0",
    "python": "3.15.2"
  },
  "source_commit": "6454ed2",
  "environment": {
    "os": "Darwin",
    "os_version": "25.4.0",
    "arch": "arm64",
    "hostname": "build-runner-01",
    "cwd": "/home/ci/project"
  },
  "timestamp": "2026-04-02T08:40:24.941071+00:00",
  "elapsed": 0.0086,
  "exit_code": 0,
  "tools": [
    {"name": "sort", "path": "/usr/bin/sort", "version": "2.3"},
    {"name": "wc", "path": "/usr/bin/wc"}
  ],
  "files_read": ["/tmp/input.txt"],
  "files_written": ["/tmp/output.txt"],
  "call_tree": [
    {"step": 0, "type": "FromFileStep", "path": "/tmp/input.txt"},
    {"step": 1, "type": "CmdStep", "cmd": "sort"},
    {"step": 2, "type": "CmdStep", "cmd": "wc -l"},
    {"step": 3, "type": "RedirectStep", "stdout": "/tmp/output.txt"}
  ]
}
```

`source_commit` is the short `HEAD` of the git repository containing the script that
called `run()`, when there is one.

`result.sbom()` is a [CycloneDX 1.6](https://cyclonedx.org/) document that SBOM
tooling can ingest: one `application` component per tool, with its detected version
and resolved path.

## CLI

```bash
clichain explain S13           # explain an error code
clichain check script.py       # preflight all tools in a script
clichain compile script.py     # bundle to standalone binary
clichain compile script.py -o name --clean
```

### explain

Look up any error code interactively:

```bash
$ clichain explain X127
error[X127]: exit 127
  command not found

  The binary was not found in any directory listed in $PATH.
  Check that the tool is installed and its location is in your PATH.
```

### check

Preflight a script -- imports the module and validates every tool it uses. Exits 1 if any
check with `on_fail="error"` fails, so it can gate CI. While the script is imported,
`run()` executes nothing: pipelines run at module level are recorded and checked too.

```bash
$ clichain check my_pipeline.py
tool: ffmpeg (ffmpeg)
  ok: ffmpeg (7.0.1 at /usr/bin/ffmpeg)
tool: jq (jq)
  error: jq — jq not found on $PATH
```

Note: `clichain check` still imports (executes) the script's own Python code. Only run it on trusted code.

### compile

Bundle a clichain script to a standalone binary. Requires `pip install cmdchain[compile]`.

```bash
clichain compile my_pipeline.py -o my_pipeline
clichain compile my_pipeline.py -o my_pipeline --clean  # clean build artifacts
```

The compiled binary supports `CLICHAIN_DESCRIBE`:

```bash
./my_pipeline                              # runs normally
CLICHAIN_DESCRIBE=describe ./my_pipeline   # shows requirements
CLICHAIN_DESCRIBE=json ./my_pipeline       # JSON description
```

## Streaming architecture

Consecutive commands connected by `.pipe()` run as concurrent OS processes linked by kernel pipes.
Data flows in ~64KB chunks managed by the kernel -- Python never touches it:

```
[echo ...] --kernel pipe--> [grep ...] --kernel pipe--> [sort] --kernel pipe--> [wc -l]
```

When a Python step (`.filter()`, `.peek()`, `.meter()`) sits between commands, clichain creates
an OS pipe pair and runs a thread to bridge them. The thread reads lines from the upstream pipe,
processes them, and writes to the downstream pipe:

```
[echo ...] --kernel pipe--> [thread: filter(fn)] --os pipe--> [wc -l]
```

Steps that require all data before proceeding are explicit buffering boundaries:
- `.collect()` -- explicit
- `.capture()` -- needs full output to store
- `.each()` -- needs lines to iterate

The pipeline planner groups consecutive streamable steps (commands, filters, peeks, meters,
the input source) together so they run as a single streaming unit, with a redirect right
after them as the group's sink. Non-streamable steps force a boundary between groups.

**Memory.** Within a group nothing is buffered in Python. The final output that reaches
the `Result` is held in memory, as is the input to a buffering boundary; redirect large
outputs to a file. Each process's stderr keeps its last 1 MB (earlier bytes are counted
and marked as truncated).

**Threads.** Every pipe has a reader running concurrently with its writer, stderr
included, so no process blocks on a full pipe. Each file descriptor has a single owner
that closes it; a bridge that stops early closes both ends, so downstream sees EOF and
upstream gets SIGPIPE. Callbacks (`filter`, `peek`, `meter(to=...)`) run on worker
threads; a callback that never returns cannot be interrupted.

## Performance

Benchmarked against equivalent bash on macOS arm64:

| Workload | bash | clichain | ratio |
|---|---|---|---|
| 3-stage pipe | 9.1ms | 6.6ms | 0.73x |
| 4-stage pipe | 8.5ms | 7.8ms | 0.92x |
| Python filter between cmds | 35.5ms | 18.2ms | 0.51x |
| 1M lines through pipes | 123ms | 117ms | 0.96x |
| Feed + sort 10k lines | 13.8ms | 5.7ms | 0.42x |

Streaming pipes are on par or faster than bash.

`.each()` spawns a process per line -- inherently expensive. Use `workers=N` for parallelism,
or `.pipe()` when the tool reads stdin (one process, streaming).

`from_file(block_size=0)` (default) matches native bash `< file` redirection.
`block_size=N` adds ~5-10% overhead for the control it provides.

## Security

clichain has the same trust model as bash: you trust the commands you run and the inputs
you provide. The difference is clichain makes that trust boundary visible and auditable.

**By design:**

- Commands are passed as argument lists to `subprocess.Popen`, never through `shell=True`
- `from_file()` and `redirect()` operate on any path the process has access to -- no sandboxing
- `{name}` interpolation inserts captured values as literal arguments, not shell expressions. A value starting with `-` can still be read as an option: use `"--"` or `capture(..., reject_options=True)` for untrusted data
- Spawned processes inherit the parent environment, including sensitive variables, unless you pass `tool(..., inherit_env=False)` (and `env=` for what they need)
- `manifest()` and `report()` include host-identifying information (hostname, cwd, tool paths); `manifest(redact=True)` removes it and strips the directories from absolute paths in file lists and command arguments (other argument text is kept)
- `clichain check` imports (executes) the target script's Python code -- only run on trusted code
- Version detection runs `<tool> --version` (or your `version_args`) with stdin closed, once per binary, during preflight and reporting

**Process management:**

While `run()` executes on the main thread, SIGINT and SIGTERM stop all child processes --
including those of parallel `.each()` workers -- gracefully (SIGTERM, 5 s grace period, then
SIGKILL; a second signal kills at once). Once everything is cleaned up, the signal is handed
to the handler that was installed before: by default SIGINT raises `KeyboardInterrupt` and
SIGTERM terminates the process. If your own handler returns instead, `run()` returns a
`Result` with `128 + signal` (130 for SIGINT). Ignored signals stay ignored. From other
threads no handlers are installed. Timeouts use the same stop path.

Stopping signals clichain's own children, not their descendants: children stay in your
terminal's process group, so tools that prompt on the terminal (`sudo`, `ssh`) keep
working. A descendant that outlives its parent (`sh -c 'sleep 60; ...'`) cannot hold up
a stopped run, because clichain stops waiting on its pipes once the children it started
have exited.

## Future

The following features are being considered based on demand:

- **Plugin system** -- typed wrappers for specific tools with autocomplete and version-aware flag mapping
- **`from_bash`** -- parse bash one-liners into clichain pipelines (dev-only migration tool)
- **Stub generation** -- `.pyi` stubs from `$PATH` scanning for IDE autocomplete

## License

Dual licensed under [MIT](LICENSE-MIT) or [Apache 2.0](LICENSE-APACHE), at your option.
