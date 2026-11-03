# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is 0.x,
a breaking change bumps the minor version and everything else bumps the patch version.

## [Unreleased]

## [0.2.0] - 2026-11-03

A rewrite of the executor for safety and correctness. Every item below has a test.

### Breaking changes

- **Python 3.11+** is required (3.10 reached end of life in October 2026). POSIX only.
- `result.sbom()` / `sbom_json()` now return a **CycloneDX 1.6** document. The previous
  run record is `result.manifest()` / `manifest_json()`.
- `redirect()` streams straight to its file and **consumes** what it writes, like `>`:
  redirected stdout no longer appears in `result.stdout`, and redirected stderr not in
  `result.stderr`. `redirect(stderr=...)` now writes the commands' stderr (it used to
  write stdout's text).
- A writer killed by **SIGPIPE** because its reader exited early (`yes | head -1`) is no
  longer a failure; use `run(ignore_sigpipe=False)` for the old behaviour.
- **Version checks fail closed**: if a tool's version cannot be determined, a `version=`
  constraint is not met (`on_fail="warn"` to continue). Invalid constraints raise
  `ValueError` when the tool is defined. Only `--version` is probed (`version_args=` for
  other flags).
- **Pipelines are immutable**: builder methods return a new pipeline instead of
  appending to the one they were called on. A second input source raises `ValueError`.
- After `.capture()`, only `{name}` for captured names is substituted; other braces are
  literal (they used to raise `KeyError`).
- `CLICHAIN_DESCRIBE` only enables describe mode for `1`, `true`, `yes`, `describe`,
  `text` or `json` (it used to be any non-empty value, including `0`), and raises
  `DescribeExit` (a `SystemExit` subclass) instead of calling `sys.exit()`.
- A signal during `run()` reaches the previously installed handler after cleanup:
  `KeyboardInterrupt` for SIGINT by default, instead of `SystemExit(130)`.
- Missing input files and unwritable redirects return a failed `Result` (returncode 1)
  instead of raising `OSError`. Exceptions from your own callbacks propagate instead of
  being printed by the thread and silently truncating output.
- `clichain check` exits 1 when a check fails, and no longer executes pipelines run by
  the script.
- `clichain.core` is gone; import from `clichain`.

### Added

- `tool(..., env=, inherit_env=, cwd=, version_args=, ok_codes=)`; `ok_codes={0, 1}` makes
  an expected status (grep with no match, diff with differences) a success.
- `feed()` accepts `bytes`.
- Live output: `run(live="auto" | "blocks" | "grouped" | "lines")` or `Live(...)`. Every
  job (a run, or an `each()` line; or every process with `unit="process"`) gets its own
  buffer, so concurrent tools never mangle each other's output. Blocks redraw in place on
  a terminal; grouped output uses `::group::` on GitHub Actions. Default: nothing live.
- `tool(..., tty=True)`: the command's stderr is a pseudo-terminal, so tools that only
  show progress on a terminal (git, curl, rsync) do; stdout stays a pipe.
- `run(..., timeout=, check=, encoding=, errors=, ignore_sigpipe=, output=)`;
  `PipelineError` for `check=True`; exit code 124 (`X124`) for timeouts.
- `result.stdout_bytes`, and lossless handling of binary / non-UTF-8 data
  (`errors="surrogateescape"` by default).
- `result.manifest(redact=True)`, which removes hostname, cwd and directory paths.
- `capture(name, reject_options=True)` against option injection.
- `pipe()` accepts a `Pipeline`, not only a `Cmd`.
- Free-threaded Python support, tested in CI.

### Changed

- `StepProfile.bytes_in` / `bytes_out` are measured at every group boundary (they were
  only set when a meter was present, and always equal); the report always has a bytes
  column. Both are now `int`, never `None`.

### Fixed

- Hangs: a command writing more than 64 KB to stderr; a filter/peek/meter callback that
  raises; a bridge in front of an early-exit reader (`... | filter | head -1`); a stopped
  run whose processes left descendants holding a pipe open.
- File descriptor leaks (stderr pipes, bridge inputs).
- Meter statistics accumulating across runs of the same pipeline; meter callbacks
  receiving a live, still-changing object.
- `explain` codes: invalid input to `clichain explain` no longer crashes; exit codes above
  128 that are not signals (e.g. 255) are explained as exit codes.
- Parallel `each()`: a callback error stops the remaining lines; a failing line's stderr
  is kept even when an earlier line was skipped.

### Security

- The version probe never passes positional words (`version`) or `-v`, which tools
  without a version flag treated as file operands (BSD `rm` deleted `./version`), and it
  runs with stdin closed (it consumed the caller's stdin).
- `import clichain` no longer runs `git`; the commit is looked up lazily, labelled
  `source_commit`, from the repository of the calling script.
- CI actions are pinned by commit SHA, tools come from `uv.lock`, Dependabot keeps them
  current, and publishing runs only after the full CI suite passes.

### Performance

- Bridges process data in blocks: filter about 6x, peek 11x and meter 49x faster on
  2M lines. Redirected output never passes through Python memory.

## [0.1.2] - 2026-04-02

### Added

- `cmdchain` import alias for the `clichain` module.

### Fixed

- Logo rendering on PyPI; the version is single-sourced from `pyproject.toml`.

## [0.1.1] - 2026-04-02

### Added

- Git commit hash in the SBOM and reports; README included on PyPI.

## [0.1.0] - 2026-04-02

First release: `tool()`, pipelines with `pipe`, `filter`, `each`, `peek`, `meter`,
`capture`, `feed`, `from_file`, `redirect`; preflight checks; reports and SBOM; the
`clichain` CLI (`explain`, `check`, `compile`).

[Unreleased]: https://github.com/dardevelin/clichain/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/dardevelin/clichain/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/dardevelin/clichain/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/dardevelin/clichain/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/dardevelin/clichain/releases/tag/v0.1.0
