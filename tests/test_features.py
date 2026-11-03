"""0.2.0 additions: accepted exit codes, binary feed, byte counts at every group boundary."""

from __future__ import annotations

import pytest

from clichain import tool

from .conftest import captured_output

echo, cat, grep = tool("echo"), tool("cat"), tool("grep")


# -- ok_codes: exit statuses that are not failures ------------------------------------


def test_exit_code_listed_in_ok_codes_is_success():
    lenient = tool("grep", ok_codes={0, 1})
    result = echo("abc").pipe(lenient("zzz")).run()
    assert result.ok
    assert result.returncode == 0
    assert result.stdout == ""


def test_exit_code_not_listed_still_fails():
    assert echo("abc").pipe(grep("zzz")).run().returncode == 1  # default: only 0 is ok
    lenient = tool("grep", ok_codes={0, 1})
    result = echo("abc").pipe(lenient("-E", "(")).run()  # invalid regex: exit 2
    assert result.returncode == 2


def test_ok_codes_in_the_middle_of_a_pipeline():
    lenient = tool("grep", ok_codes={0, 1})
    result = echo("abc").pipe(lenient("zzz")).pipe(tool("wc")("-l")).run()
    assert result.ok
    assert result.stdout.strip() == "0"


def test_ok_codes_survive_adding_arguments_and_show_in_manifest():
    lenient = tool("grep", ok_codes=(1, 0))("-q")("zzz")
    assert lenient.step.ok_codes == (0, 1)
    node = echo("x").pipe(lenient).run().manifest()["call_tree"][1]
    assert node["ok_codes"] == [0, 1]


@pytest.mark.parametrize("codes", [set(), {"1"}, {-1}, {256}])
def test_invalid_ok_codes_rejected(codes):
    with pytest.raises(ValueError, match="ok_codes"):
        tool("grep", ok_codes=codes)


# -- feed(bytes) ----------------------------------------------------------------------


def test_feed_bytes_round_trips_binary():
    data = b"\xff\x00raw\r\n\xfe"
    result = cat().feed(data).run()
    assert result.ok
    assert result.stdout_bytes == data


def test_feed_bytes_through_a_filter():
    result = cat().feed(b"keep\n\xffdrop\nkeep too\n").filter(lambda line: "keep" in line).run()
    assert result.stdout_bytes == b"keep\nkeep too\n"


def test_feed_title_counts_bytes():
    assert cat().feed("é").steps[0].title() == "feed (2 bytes)"
    assert cat().feed(b"\x00\x01\x02").steps[0].title() == "feed (3 bytes)"


# -- bytes_in / bytes_out at every group boundary ----------------------------------------


def test_stream_group_bytes_in_and_out_differ_through_a_filter():
    result = tool("seq")("1", "10").filter(lambda line: int(line) % 2 == 0).run()
    (group,) = result.profile
    assert group.bytes_in == 0
    assert group.bytes_out == len(b"2\n4\n6\n8\n10\n")


def test_bytes_carry_across_buffering_boundaries(tmp_path):
    src = tmp_path / "in.txt"
    src.write_bytes(b"b\na\n")
    result = cat().from_file(src).collect().pipe(tool("sort")()).run()
    first, collect, sort = result.profile
    assert first.bytes_in == 4  # the file's size: its fd went straight to cat
    assert first.bytes_out == collect.bytes_in == collect.bytes_out == sort.bytes_in == 4
    assert sort.bytes_out == 4


def test_redirect_sink_counts_bytes_written(tmp_path):
    out = tmp_path / "out.txt"
    result = tool("seq")("1", "1000").redirect(stdout=str(out)).run()
    assert result.profile[0].bytes_out == out.stat().st_size


def test_report_shows_bytes_without_a_meter():
    lines = captured_output(echo("hello").run().report)
    assert any("bytes" in line for line in lines)


def test_redirect_bytes_survive_the_command_removing_its_file(tmp_path):
    out = tmp_path / "out.txt"
    result = tool("sh")("-c", f"rm -f {out}; echo hi").redirect(stdout=str(out)).run()
    assert result.ok
    assert result.profile[0].bytes_out == 3  # measured on the descriptor, not the path
