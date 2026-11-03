"""Live output: capture / lines / grouped / blocks consoles, pty stderr, stdout blocks."""

from __future__ import annotations

import io
import re
import threading
import time

import pytest

from clichain import Live, tool
from clichain.live import console_for

echo, sh = tool("echo"), tool("sh")


class FakeTTY(io.StringIO):
    """A terminal-ish stream: reports a tty and a fixed size."""

    def isatty(self) -> bool:
        return True


class Screen:
    """Just enough VT100 to replay what a console wrote: text, \\n, \\r, CSI F/J/m."""

    _CSI = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])")

    def __init__(self, text: str) -> None:
        self.rows: list[str] = [""]
        row = col = 0
        i = 0
        while i < len(text):
            match = self._CSI.match(text, i)
            if match:
                arg, final = match[1], match[2]
                if final == "F":
                    n = int(arg or 1)
                    row, col = max(0, row - n), 0
                elif final == "J":
                    self.rows[row] = self.rows[row][:col]
                    del self.rows[row + 1 :]
                i = match.end()
                continue
            ch = text[i]
            if ch == "\n":
                row, col = row + 1, 0
                if row == len(self.rows):
                    self.rows.append("")
            elif ch == "\r":
                col = 0
            else:
                line = self.rows[row].ljust(col)
                self.rows[row] = line[:col] + ch + line[col + 1 :]
                col += 1
            i += 1

    @property
    def text(self) -> str:
        return "\n".join(self.rows).rstrip("\n")


# -- choosing a console --------------------------------------------------------------


def test_auto_picks_blocks_on_a_terminal_and_the_fallback_elsewhere(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    assert console_for(Live("auto", stream=FakeTTY())).mode == "blocks"
    assert console_for(Live("auto", stream=io.StringIO())).mode == "grouped"
    assert console_for(Live("auto", fallback="lines", stream=io.StringIO())).mode == "lines"
    monkeypatch.setenv("TERM", "dumb")
    assert console_for(Live("auto", stream=FakeTTY())).mode == "grouped"


def test_string_shorthand_and_default_capture():
    assert console_for(None).mode == "capture"
    assert console_for("lines").mode == "lines"
    with pytest.raises(ValueError, match="live"):
        console_for("loud")


# -- lines -----------------------------------------------------------------------------


def test_lines_never_mix_partial_writes_and_collapse_progress():
    out = io.StringIO()
    console = console_for(Live("lines", stream=out))
    a, b = console.job("job-a").source("cmd a"), console.job("job-b").source("cmd b")
    a.write(b"hel")
    b.write(b"wor")
    a.write(b"lo\n10%\r50%\r100%\n")
    b.write(b"ld\n")
    a.done(0)
    b.done(0)
    console.close()
    assert out.getvalue().splitlines() == [
        "[job-a] hello",  # both complete with job-a's third write
        "[job-a] 100%",  # only the last carriage-return rewrite of the line
        "[job-b] world",
    ]


def test_lines_flush_an_unterminated_last_line_and_strip_escapes():
    out = io.StringIO()
    console = console_for(Live("lines", stream=out))
    src = console.job("j").source("c")
    src.write(b"\x1b[31mred\x1b[0m\x1b[2K tail")
    src.done(0)
    console.close()
    assert out.getvalue() == "[j] red tail\n"


# -- grouped ---------------------------------------------------------------------------


def test_grouped_prints_each_job_together_when_it_finishes():
    out = io.StringIO()
    console = console_for(Live("grouped", stream=out))
    first, second = console.job("first"), console.job("second")
    s1, s2 = first.source("a"), second.source("b")
    s1.write(b"one\n")
    s2.write(b"uno\n")
    s1.write(b"two\n")
    s2.done(0)
    second.finish(0)  # finishes first: printed first, in one piece
    s1.done(0)
    first.finish(3)
    console.close()
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("✓ second")
    assert lines[1] == "│ uno"
    assert lines[2].startswith("✗ first") and "exit 3" in lines[2]
    assert lines[3:] == ["│ one", "│ two"]


def test_grouped_uses_github_actions_groups(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    out = io.StringIO()
    console = console_for(Live("grouped", stream=out))
    job = console.job("build")
    job.source("make").write(b"compiling\n")
    job.finish(0)
    console.close()
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("::group::✓ build")
    assert lines[-1] == "::endgroup::"


# -- blocks ----------------------------------------------------------------------------


def test_blocks_draw_running_jobs_in_place_and_collapse_finished_ones(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    tty = FakeTTY()
    console = console_for(Live("blocks", tail=2, stream=tty, fps=50))
    convert = console.job("ffmpeg b.mkv")
    src = convert.source("ffmpeg")
    src.write(b"Stream mapping\nsize=1kB\rsize=2kB\rsize=")
    time.sleep(0.15)
    live = Screen(tty.getvalue()).text
    assert "ffmpeg b.mkv" in live
    assert "│ Stream mapping" in live
    assert "│ size=" in live  # the progress line, rewritten in place
    assert live.count("ffmpeg b.mkv") == 1  # redrawn, not appended

    src.write(b"3kB\n")
    src.done(0)
    convert.finish(0)
    failing = console.job("unzip x")
    s = failing.source("unzip")
    s.write(b"bad zip\n")
    s.done(9)
    failing.finish(9)
    console.close()
    final = Screen(tty.getvalue()).text.splitlines()
    assert final[0].startswith("✓ ffmpeg b.mkv")
    assert final[1].startswith("✗ unzip x") and "exit 9" in final[1]
    assert final[2] == "│ bad zip"  # a failed job keeps its tail on screen
    assert len(final) == 3  # no leftover live area


def test_blocks_messages_appear_above_the_live_area(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    tty = FakeTTY()
    console = console_for(Live("blocks", stream=tty, fps=50))
    job = console.job("work")
    console.message("  ok: sort (/usr/bin/sort)")
    time.sleep(0.1)
    job.finish(0)
    console.close()
    lines = Screen(tty.getvalue()).text.splitlines()
    assert lines == ["  ok: sort (/usr/bin/sort)", lines[1]]
    assert lines[1].startswith("✓ work")


def test_blocks_are_thread_safe_under_many_writers(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    tty = FakeTTY()
    console = console_for(Live("blocks", stream=tty, fps=100))

    def work(n: int) -> None:
        job = console.job(f"job {n}")
        src = job.source("cmd")
        for i in range(200):
            src.write(f"line {i}\n".encode())
        src.done(0)
        job.finish(0)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    console.close()
    final = Screen(tty.getvalue()).text.splitlines()
    assert len(final) == 16  # one summary per job, nothing torn or left over
    assert sorted(line.split()[2] for line in final) == sorted(str(n) for n in range(16))
    assert all(line.startswith("✓ job ") for line in final)


def test_blocks_renderer_thread_is_released():
    before = threading.active_count()
    console = console_for(Live("blocks", stream=FakeTTY()))
    console.job("x").finish(0)
    console.close()
    assert threading.active_count() == before


# -- through run() -------------------------------------------------------------------------


def test_run_stays_quiet_by_default(capfd):
    sh("-c", "echo err >&2").run()
    assert capfd.readouterr().err == ""


def test_run_lines_tags_each_line_with_its_job():
    out = io.StringIO()
    script = 'for i in 1 2 3; do echo "$0 $i" >&2; sleep 0.01; done'
    result = (
        echo("a\nb\nc")
        .each(lambda x: sh("-c", script, x), workers=3)
        .run(live=Live("lines", stream=out))
    )
    assert result.ok
    lines = out.getvalue().splitlines()
    assert len(lines) == 9
    for line in lines:  # the job label leads with the input line: what tells jobs apart
        match = re.fullmatch(r"\[each\[\d\] ([abc]) → sh -c .*\] ([abc]) [123]", line)
        assert match and match[1] == match[2], line
    assert "a 1" in result.stderr  # still captured


def test_unit_process_gives_each_process_its_own_block():
    out = io.StringIO()
    pipeline = sh("-c", "echo first >&2; echo x").pipe(sh("-c", "cat >/dev/null; echo second >&2"))
    pipeline.run(live=Live("grouped", unit="process", stream=out))
    lines = out.getvalue().splitlines()
    first = lines.index(next(x for x in lines if x.startswith("✓ sh -c 'echo first >&2; echo x'")))
    second = lines.index(next(x for x in lines if x.startswith("✓ sh -c 'cat >/dev/null;")))
    assert lines[first + 1] == "│ first"
    assert lines[second + 1] == "│ second"
    assert len(lines) == 4  # no block for the run itself: with unit="process" it is a container


def test_unit_job_groups_all_processes_of_a_run():
    out = io.StringIO()
    pipeline = sh("-c", "echo first >&2; echo x").pipe(sh("-c", "cat >/dev/null; echo second >&2"))
    pipeline.run(live=Live("grouped", stream=out))
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("✓ ")
    assert sorted(lines[1:]) == ["│ first", "│ second"]


def test_stdout_blocks_show_the_output_that_reaches_python():
    out = io.StringIO()
    result = tool("printf")("x\\ny\\n").run(live=Live("grouped", stdout=True, stream=out))
    assert result.stdout == "x\ny\n"
    assert out.getvalue().splitlines()[1:] == ["│ x", "│ y"]


def test_labels_are_single_line():
    out = io.StringIO()
    tool("printf")("a\nb\n").run(live=Live("grouped", stream=out))
    (summary,) = out.getvalue().splitlines()
    assert "a\\nb" in summary  # the newline is shown escaped, not printed


def test_failed_job_status_in_grouped_output():
    out = io.StringIO()
    sh("-c", "echo boom >&2; exit 4").run(live=Live("grouped", stream=out))
    first = out.getvalue().splitlines()[0]
    assert first.startswith("✗ ") and "exit 4" in first


def test_clichain_messages_go_through_the_console(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    tty = FakeTTY()
    echo("x").peek(label="p").run(live=Live("blocks", stream=tty, fps=50), output=tty)
    lines = Screen(tty.getvalue()).text.splitlines()
    assert "[p] x" in lines
    assert lines[-1].startswith("✓ ")


# -- pty -----------------------------------------------------------------------------------


def test_tty_gives_stderr_a_terminal_and_keeps_stdout_a_pipe():
    probe = tool("sh", tty=True)
    result = probe("-c", "[ -t 2 ] && echo err-tty >&2; [ -t 1 ] || echo out-pipe").run()
    assert result.stdout == "out-pipe\n"
    assert "err-tty" in result.stderr
    assert "\r\n" not in result.stderr  # raw mode: no newline translation


def test_tty_progress_reaches_blocks(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    tty = FakeTTY()
    progress = tool("sh", tty=True)("-c", "printf '10%%\\r50%%\\r100%%\\n' >&2")
    progress.run(live=Live("grouped", stream=tty))
    assert "│ 100%" in tty.getvalue()


def test_tty_terminal_size_follows_the_console(monkeypatch):
    out = io.StringIO()
    result = tool("sh", tty=True)("-c", "stty size <&2 >&2").run(
        live=Live("grouped", stream=out, columns=100)
    )
    assert result.stderr.split()[-1] == "98"  # the block's inner width


def test_tty_and_merge_stderr_conflict():
    with pytest.raises(ValueError, match="tty"):
        tool("sh", tty=True)("-c", "true").merge_stderr().run()


def test_messages_to_another_terminal_also_go_above_the_live_area(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    screen, other = FakeTTY(), FakeTTY()  # e.g. live on stderr, meter told to use stdout
    tool("seq")("1", "3").meter(label="m", to=other, interval=0).run(
        live=Live("blocks", stream=screen, fps=50)
    )
    assert other.getvalue() == ""  # nothing drawn across the blocks
    assert "[m] " in Screen(screen.getvalue()).text


# -- code review follow-ups ------------------------------------------------------------------


def test_every_escape_sequence_is_stripped_except_colour():
    from clichain.live import _display

    raw = "a\x1bcb\x1b7x\x1b8\x1b(By\x1b"
    assert _display(raw, keep_colour=False) == "abxy"
    assert _display("\x1b[31mred\x1b[0m\x1bc", keep_colour=True) == "\x1b[31mred\x1b[0m"


def test_callback_error_still_closes_live_blocks():
    out = io.StringIO()

    def boom(_):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        sh("-c", "echo note >&2; echo a").filter(boom).run(
            live=Live("grouped", unit="process", stream=out)
        )
    assert "│ note" in out.getvalue()


def test_accepted_exits_are_shown_as_success():
    out = io.StringIO()
    lenient = tool("sh", ok_codes={0, 1})("-c", "echo warn >&2; exit 1")
    assert lenient.run(live=Live("grouped", unit="process", stream=out)).ok
    tool("yes")().pipe(tool("head")("-1")).run(live=Live("grouped", unit="process", stream=out))
    assert "✗" not in out.getvalue()


def test_each_output_is_not_shown_twice():
    out = io.StringIO()
    echo("a\nb").each(lambda x: echo(x + "!")).run(live=Live("grouped", stdout=True, stream=out))
    assert out.getvalue().count("│ a!") == 1
    assert out.getvalue().count("│ b!") == 1


def test_no_stdout_block_when_nothing_reaches_python(tmp_path):
    out = io.StringIO()
    tool("false")().run(live=Live("grouped", unit="process", stdout=True, stream=out))
    tool("seq")("1", "3").redirect(stdout=str(tmp_path / "f")).run(
        live=Live("grouped", unit="process", stdout=True, stream=out)
    )
    assert "stdout" not in out.getvalue()


def test_frame_never_fills_the_whole_screen():
    console = console_for(Live("blocks", tail=1, stream=FakeTTY(), fps=1))
    for n in range(16):  # 2-row blocks: 11 of them fill the budget exactly
        console.job(f"job {n}").source("cmd").write(b"x\n")
    with console.lock:
        snap = console._snapshot()  # type: ignore[attr-defined]
    frame = console._frame(snap, 80, 24)  # type: ignore[attr-defined]
    console.close()
    assert len(frame) <= 23  # one row spare, or each newline would scroll the screen
    assert any(row.startswith("+") and "more running" in row for row in frame)


def test_fit_counts_terminal_cells():
    from clichain.live import _fit

    assert _fit("日本語ファイル名", 4) == "日本"
    assert _fit("ab日", 3) == "ab"  # 日 takes two cells: it does not fit
    assert _fit("éx", 2) == "éx"  # a combining accent takes none


def test_a_broken_display_never_breaks_the_run():
    class Broken(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError

    result = sh("-c", "for i in 1 2 3; do echo $i >&2; done; echo ok").run(
        live=Live("lines", stream=Broken())
    )
    assert result.ok
    assert result.stdout == "ok\n"
    assert "3" in result.stderr


@pytest.mark.parametrize(
    "kwargs",
    [{"fps": 0}, {"fps": -1}, {"unit": "proc"}, {"fallback": "auto"}, {"tail": 0}, {"columns": 3}],
)
def test_live_settings_are_validated(kwargs):
    with pytest.raises(ValueError):
        Live("blocks", **kwargs)
