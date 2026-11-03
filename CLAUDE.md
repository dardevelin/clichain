# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

clichain: fluent, lazily-evaluated pipelines of CLI commands connected by real kernel pipes. Zero runtime dependencies, Python 3.11+ (free-threaded builds included), POSIX only.

**Versioning:** Semantic Versioning, recorded in `CHANGELOG.md` (Keep a Changelog). While 0.x, a breaking change bumps the minor version, anything else the patch; bump `version` in `pyproject.toml` and run `uv lock`. Releases are git tags `vX.Y.Z`; publishing a GitHub release uploads to PyPI after CI passes.

**Naming split:** the PyPI distribution is `cmdchain`, the import module is `clichain`; `src/cmdchain/` only re-exports `clichain`. The version is single-sourced in `pyproject.toml` and read via `importlib.metadata.version("cmdchain")` (in `manifest.py`), so it reads `0.0.0` unless the package is installed.

## Commands

```bash
uv sync                                         # .venv + dev tools from uv.lock (CI uses --locked)
uv run ruff check src/ tests/ examples/
uv run ruff format --check src/ tests/ examples/
uv run basedpyright src/ tests/
uv run pytest                                   # all tests
uv run pytest tests/test_core.py::test_feed     # single test
uv run coverage run -m pytest && uv run coverage report   # also measures subprocesses
uv run -p 3.15t pytest                          # free-threaded interpreter
uv sync --extra compile && uv run pytest tests/test_compile.py   # pyinstaller; skipped without it
```

pytest config makes **every warning an error** (so a leaked fd's `ResourceWarning` fails the test) and gives each test a 60 s timeout. `tests/conftest.py` fails any test whose worker threads raised unhandled exceptions, and provides `run_snippet()` for anything that could hang or involves signals: it runs code in a fresh interpreter in its own session and kills the whole session on timeout. CI runs 3.11–3.15 and 3.15t on Ubuntu and macOS, plus an allowed-to-fail 3.16 job; tests shell out to coreutils, so code must work with both GNU and BSD tools.

## Architecture

Flow: `tool()` → immutable `Pipeline` of frozen step dataclasses → `run()` (`pipeline.py`) → `execute()` (`execute.py`) → `Result`.

- **Immutability.** Every builder method returns a new `Pipeline`; `Cmd` is a one-step `Pipeline` whose `__call__` appends args. Steps are frozen, and runtime state (meter stats, captured values, stderr) lives in per-run objects. Pipelines can therefore be shared and run concurrently. `.feed()` / `.from_file()` set the pipeline's stdin (stored at index 0) and a second source raises.
- **Steps own their description** (`steps.py`): `title()`, `node()` (manifest fields) and the `streamable` class flag. Only `execute.py` knows how to run them. A new step type needs a dataclass there plus handling in `_Run.run_group` (buffered) or `_StreamGroup` (streaming).
- **Planning** (`execute.plan`): consecutive streamable steps form one group; a `RedirectStep` right after them joins as the group's sink (streams to the file). Every other step is its own buffering boundary; text passes between groups as `str`.
- **Streaming group** (`_StreamGroup`): processes run in byte mode; Python bridges (filter/peek/meter) are `_Worker` threads processing line batches (`_batches`) and decoding only for callbacks. stderr is drained concurrently into a bounded `_Tail`.
- **Abandonable pipe I/O:** pipes are unbuffered, and every blocking read or write goes through `_read`/`_send`, which `poll()` and ask the group's `_give_up()`. That returns True once the run is stopped or aborting and none of the group's direct children is alive, so a descendant holding a pipe open can't block teardown. Children deliberately share the terminal's process group (no `process_group=0`), so tty prompts keep working; descendants are not signalled.
- **Resource invariants (keep them):** every fd has a single owner. The main thread holds it in `owned` until it hands it to a child (and closes its copy) or to a worker (which closes it in `finally`). Never close an fd another thread may use. Stopping signals processes and never closes fds. A worker that stops closes its output first, then its input.
- **Process supervision** (`process.py`): one `ProcessGroup` per top-level run is shared by nested `each()` runs, including thread-pool workers. `stop(reason, returncode, message)` is thread- and signal-safe (RLock, because a handler can interrupt the main thread mid-critical-section). The first stop decides the Result's returncode and message (timeout → 124); `finish()` makes later stops (a late timer) no-ops. `SignalGuard` installs handlers only on the main thread, skips ignored signals, and after cleanup `deliver()` re-sends the signal to the previous handler (KeyboardInterrupt, default SIGTERM death, or a custom handler → Result 128+n).
- **Error model:** command and environment failures (exit codes, 127 not found, missing files/dirs → 1, timeouts → 124) become a failed `Result`; exceptions from user callbacks propagate out of `run()` after cleanup; bad definitions (`tool(version="x")`, second source) raise at build time. `run(check=True)` raises `PipelineError`.
- **Interpolation:** after `.capture(name)`, only `{name}` for names captured so far is replaced (`CmdStep.argv`); other braces are literal.
- **Version probing** (`checks.detect_version`): only `--version` (or `version_args`), stdin closed, cached per (path, args) under a lock. Unknown version + constraint fails closed. Constraints are parsed at `tool()` time.
- **Live output** (`live.py`): `run(live=...)` builds a `Console`; the executor only calls `console.job(label)` (per run and per `each()` line), `job.source(label)` (per process, and for teed stdout), `source.write(bytes)` from the stderr drain threads, `source.done(rc)` and `job.finish(rc)`. Mode (capture/lines/grouped/blocks) and unit (job/process: with "process", `job.source()` makes a block of its own) live only in the console. One writer per stream: lines/grouped write whole lines under the console lock; in blocks mode the renderer thread is the only terminal writer and everything else updates the model under the lock. `tty=True` gives a child's stderr a raw pty (never stdout).
- **Output and records:** `render.py` holds the `set_output` destination (resolved once per run; `run(output=)` overrides) and a global emit lock, plus all explain/report/describe text. `manifest.py` owns the tool/file inventory used by `describe`, `manifest()`, `sbom()` (CycloneDX 1.6) and `report()`. `manifest(redact=True)` strips host details.
- **Side channels:** `CLICHAIN_DESCRIBE` (`1/true/yes/describe/text` or `json`) makes `run()` print and raise `DescribeExit` (a SystemExit). It is stripped from child envs. `clichain check` uses `dry_run()` (a process-wide flag, so threads the script starts are covered too) so `run()` records instead of executing, and exits 1 on blocking check failures.
- **Error codes** (`S<signal>` / `X<exit>`): tables live in `errors.py` and are shared by `Result.explain()` and `clichain explain`.
