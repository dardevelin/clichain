"""each() result merging, including the shapes only a race between workers produces.

With workers, a line can be skipped (``None``: it saw the cancel flag) even though it
comes *before* the line that failed. Those shapes cannot be produced reliably end to end,
so the merge step is tested directly.
"""

from __future__ import annotations

from clichain.execute import Outcome, RunOptions, _GroupOutcome, _Run
from clichain.live import console_for
from clichain.process import ProcessGroup
from clichain.result import StepProfile


def _line(stdout: str = "", returncode: int = 0, stderr: str = "") -> Outcome:
    return Outcome(
        stdout=stdout,
        returncode=returncode,
        stderr=[stderr] if stderr else [],
        diagnostics=[f"cmd: exit {returncode}"] if returncode else [],
        profile=[StepProfile("cmd", spawns=1)],
    )


def _merge(results: list[Outcome | None]) -> tuple[_Run, _GroupOutcome]:
    run = _Run(RunOptions(), ProcessGroup(), console_for(None).job("each"))
    return run, run.merge_lines(results)


def test_failure_after_a_skipped_line_keeps_its_details():
    run, merged = _merge([_line("a\n"), _line("b\n"), None, _line("", 3, "BOOM\n"), None])
    assert merged.returncode == 3
    assert merged.output == "a\nb\n"
    assert merged.spawns == 3
    assert run.stderr == ["BOOM\n"]
    assert run.diagnostics == ["cmd: exit 3"]


def test_first_failure_in_input_order_wins():
    run, merged = _merge(
        [_line("a\n"), _line("x\n", 2, "E1\n"), _line("late\n"), _line("", 5, "E2\n")]
    )
    assert merged.returncode == 2
    assert merged.output == "a\nx\n"  # the failing line's own output is kept
    assert run.stderr == ["E1\n"]
    assert run.diagnostics == ["cmd: exit 2"]


def test_all_lines_succeed():
    run, merged = _merge([_line("a\n"), _line(""), _line("c\n")])
    assert merged.returncode == 0
    assert merged.output == "a\nc\n"
    assert merged.spawns == 3
    assert run.stderr == []


def test_everything_skipped():
    _, merged = _merge([None, None])
    assert merged.returncode == 0
    assert merged.output == ""
    assert merged.spawns == 0
