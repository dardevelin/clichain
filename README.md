<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="logos/clichain-horizontal-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="logos/clichain-horizontal-light.svg">
    <img alt="clichain" src="logos/clichain-horizontal-dark.svg" width="340">
  </picture>
</p>

<p align="center">
  Fluent CLI tool chaining for Python.
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
of CLI commands connected by real kernel pipes. Zero runtime dependencies. Python 3.10+.

```python
from clichain import tool

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
- **SBOM** -- full bill of materials for every execution
- **Compilation** -- bundle scripts to single binaries for CI

## Install

```bash
pip install cmdchain
```

clichain has zero runtime dependencies. Python 3.10+ only.

For compiling scripts to standalone binaries:

```bash
pip install cmdchain[compile]
```

## Development setup

Clone the repository and install in editable mode with dev tooling:

```bash
git clone https://github.com/dardevelin/clichain.git
cd clichain
python -m venv .venv
source .venv/bin/activate      # or .venv\Scripts\activate on Windows
pip install -e .
pip install --dependency-group dev
```

This gives you:
- **pytest** -- test runner (`python -m pytest`)
- **ruff** -- linter and formatter (`ruff check src/ tests/` and `ruff format src/ tests/`)
- **basedpyright** -- type checker

Run the full check suite:

```bash
ruff check src/ tests/         # lint
ruff format --check src/ tests/ # format check
python -m pytest tests/         # unit tests (62 tests)
```

Compile end-to-end tests require pyinstaller and are skipped by default:

```bash
pip install cmdchain[compile]
python -m pytest tests/test_compile.py   # compile + run binary tests
```

### Project structure

```
src/clichain/
  __init__.py     # public API: tool, Cmd, Pipeline, Result, set_output
  core.py         # steps, pipeline execution, streaming, profiling, SBOM
  checks.py       # preflight checks: binary, env, file_exists
  cli.py          # CLI: explain, check, compile
  py.typed        # PEP 561 typed package marker
tests/
  test_core.py    # unit tests for all primitives
  test_compile.py # end-to-end compile tests (requires pyinstaller)
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

`.capture()` buffers the output and strips whitespace. `{name}` uses Python's
`str.format_map()` for substitution. Only the final output of a piped group
can be captured.

### Filter with Python logic

`.filter()` receives each line (stripped of newline), keeps lines where the function returns True.
It streams line-by-line through a thread -- no full buffering:

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

With a label (prints to `clichain.output`):

```python
.peek(label="after filter")
# [after filter] hello
# [after filter] world
```

### Feed

Provide a string as stdin to the first command:

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

**Write to file:**

```python
# bash: sort < input.txt > output.txt
sort().from_file("input.txt").redirect(stdout="output.txt").run()

# Append mode: >>
echo("new line").redirect(stdout="log.txt", append=True).run()

# Separate stderr:
cmd("make").redirect(stdout="build.log", stderr="errors.log").run()
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
- `to` -- custom destination: `None` (uses `clichain.output`), a callable receiving `MeterStats`, or a file-like object

```python
# Custom callback
stats_log = []
.meter(to=lambda stats: stats_log.append(stats), interval=0)

# MeterStats fields:
#   stats.label          str
#   stats.bytes          int    -- total bytes seen
#   stats.lines          int    -- total lines seen
#   stats.elapsed        float  -- seconds since start
#   stats.bytes_per_sec  float
#   stats.lines_per_sec  float
```

When a meter is present, the execution report includes a bytes column.
Without meter, the column is hidden -- no overhead unless you ask for it.

### Version constraints

```python
ffmpeg = tool("ffmpeg", version=">=6.0", msg="brew install ffmpeg")
jq     = tool("jq", version=">=1.7")
```

**`tool()` parameters:**
- `name` -- binary name (looked up on `$PATH`)
- `version` -- semver constraint. Supports `>=`, `<=`, `>`, `<`, `==` and comma-separated combinations like `>=6.0,<8.0`
- `on_fail` -- what to do if the check fails: `"error"` (default, stops execution), `"warn"` (logs, continues), `"pass"` (silent, continues)
- `msg` -- custom message shown on failure

Version detection tries `--version`, `-version`, `version`, `-v` flags in order
and regex-matches the first semver string from the output.

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
) -> Cmd
```

Creates a command wrapper for a binary. Call the result with arguments:

```python
grep = tool("grep")
grep("-ri", "pattern", ".")  # returns a new Cmd with those args
```

### Pipeline primitives

| Method | What it does | Streaming |
|---|---|---|
| `.pipe(cmd)` | Connect stdout to stdin via kernel pipe | yes |
| `.filter(fn)` | Keep lines where `fn(line)` is True | yes, via thread |
| `.each(fn, workers=1)` | Run command per input line, stops on first failure | per-line spawn |
| `.peek(label=, fn=)` | Observe data without modifying | yes, via thread |
| `.meter(label=, to=, interval=1.0)` | Count bytes/lines, report throughput | yes, via thread |
| `.collect()` | Explicit buffering boundary | no |
| `.capture(name)` | Buffer and bind to `{name}` for interpolation | no |
| `.feed(data)` | String as stdin (inserted at start) | no |
| `.from_file(path, block_size=0)` | File as stdin (`0` = kernel, `N` = chunked thread) | yes |
| `.redirect(stdout=, stderr=, append=False)` | Write to file | yes |
| `.merge_stderr()` | Combine stderr into stdout (2>&1) | yes |

### Execution and inspection

| Method | What it does |
|---|---|
| `.run(validate=True, pre=None)` | Execute pipeline, returns `Result` |
| `.check()` | Validate binaries exist, returns `list[CheckResult]` |
| `.describe()` | Print steps and requirements without executing |

### Result

```python
result = pipeline.run()

# Status
result.ok            # bool: returncode == 0
result.returncode    # int: exit code or negative signal number
result.stdout        # str: captured output
result.stderr        # str: captured errors
result.lines         # list[str]: stdout split by newline, stripped

# Diagnostics
result.checks        # list[CheckResult]: preflight results
result.profile       # list[StepProfile]: timing per step group
result.elapsed       # float: total seconds

# Methods
result.report()      # print full execution report
result.explain()     # print error diagnosis with trace
result.sbom()        # dict: software bill of materials
result.sbom_json(indent=2)  # str: SBOM as JSON
```

**`StepProfile` fields:**
- `name` -- step group description
- `elapsed` -- seconds
- `lines_in`, `lines_out` -- line counts at group boundaries
- `spawns` -- number of processes created
- `bytes_in`, `bytes_out` -- byte counts (only when `.meter()` is present, otherwise `None`)

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
meter stats, reports, and error explanations. Set once at the top of your script.

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
CLICHAIN_DESCRIBE=describe ./my_binary   # human-readable
CLICHAIN_DESCRIBE=json ./my_binary       # machine-readable JSON
```

The JSON output includes generator info, environment (OS, arch), steps with types and names,
tools with paths/versions/found status, and files read/written.

### Error diagnostics

```python
result.explain()
```

```
error[S13]: signal 13 (SIGPIPE: broken pipe -- downstream closed
            before upstream finished)

  execution trace:
    [0] Cmd        seq 100000
    [1] Filter     filter at script.py:12
    [2] Cmd        sort -rn  <--
    [3] Cmd        head -10
    [4] Cmd        wc -l

  source: script.py:15

  This usually means a downstream command (like head or grep -m)
  closed its input before the upstream command finished writing.
  This is often normal behavior, not a real error.
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
| `S13` | SIGPIPE -- broken pipe (downstream closed early) |
| `S15` | SIGTERM -- terminated (process manager, CI shutdown) |
| `X1` | General error -- check stderr |
| `X2` | Invalid arguments |
| `X126` | Permission denied -- binary not executable |
| `X127` | Command not found |

### Execution report

```python
result.report()
```

```
========================================================================
  clichain 0.1.0  |  python 3.14.3  |  Darwin 25.4.0 arm64
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
When `.meter()` is present, a bytes column appears in the profile table.

### SBOM

Every execution produces a software bill of materials:

```python
result.sbom()       # dict
result.sbom_json()  # formatted JSON string
result.sbom_json(indent=4)  # custom indentation
```

```json
{
  "generator": {
    "name": "clichain",
    "version": "0.1.0",
    "python": "3.14.3"
  },
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

Preflight a script -- imports the module and validates all `tool()` declarations:

```bash
$ clichain check my_pipeline.py
tool: ffmpeg (ffmpeg)
  ok: ffmpeg (7.0.1 at /usr/bin/ffmpeg)
tool: jq (jq)
  error: jq — jq not found on $PATH
```

Note: `clichain check` executes the script to discover tools. Only run on trusted code.

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

The pipeline planner groups consecutive streamable steps (commands, filters, peeks, meters)
together so they run as a single streaming unit. Non-streamable steps force a boundary
between groups.

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
- `{name}` interpolation inserts captured values as literal arguments, not shell expressions. If captured data comes from untrusted sources, the values become arguments to downstream commands
- All spawned processes inherit the parent environment including sensitive variables
- `sbom()` and `report()` include host-identifying information (hostname, cwd, tool paths)
- `clichain check` executes the target script to discover tools -- only run on trusted code
- Version detection (`--version`) executes binaries during preflight

**Process management:**

On SIGINT/SIGTERM, clichain terminates all child processes gracefully (SIGTERM, 5s grace period,
SIGKILL), joins threads, and closes file handles. No orphan processes. Signal handlers are only
installed on the main thread.

## Future

The following features are being considered based on demand:

- **Plugin system** -- typed wrappers for specific tools with autocomplete and version-aware flag mapping
- **`from_bash`** -- parse bash one-liners into clichain pipelines (dev-only migration tool)
- **Stub generation** -- `.pyi` stubs from `$PATH` scanning for IDE autocomplete

## License

Dual licensed under [MIT](LICENSE-MIT) or [Apache 2.0](LICENSE-APACHE), at your option.
