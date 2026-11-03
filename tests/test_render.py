"""explain / report / describe texts and the output destination."""

from __future__ import annotations

import io

import clichain
from clichain import tool
from clichain.render import wrap

from .conftest import captured_output


def test_explain_traces_the_failing_step():
    def keep(line: str) -> bool:
        return True

    result = tool("echo")("x").filter(keep).pipe(tool("false")()).pipe(tool("cat")()).run()
    text = "\n".join(captured_output(result.explain))
    assert "error[X1]: exit 1 — general error" in text
    assert "[2] Cmd        false  <--" in text
    assert "filter at test_render.py:" in text  # callback source location
    assert f"source: {__file__}:" in text
    assert "general error" in text


def test_explain_ok():
    assert captured_output(tool("true")().run().explain) == ["no errors"]


def test_report_includes_tools_profile_and_bytes_column(tmp_path):
    out = tmp_path / "o.txt"
    result = (
        tool("seq")("1", "3").meter(label="m", to=lambda s: None).redirect(stdout=str(out)).run()
    )
    text = "\n".join(captured_output(result.report))
    assert "seq" in text
    assert "bytes" in text
    assert f"> {out}" in text
    assert "total" in text


def test_describe_lists_steps_requirements_and_files(tmp_path):
    src = tmp_path / "in.txt"
    src.write_text("x\n")
    pipeline = tool("sort", version=">=1.0", on_fail="warn")().from_file(src).pipe(tool("uniq")())
    text = "\n".join(captured_output(pipeline.describe))
    assert "pipeline: 3 steps" in text
    assert "[0] FromFile" in text
    assert "requires:" in text
    assert f"< {src}" in text


def test_output_to_file_object_and_per_run_override():
    buffer, override = io.StringIO(), io.StringIO()
    clichain.set_output(buffer)
    try:
        tool("echo")("a").peek(label="p").run(validate=False)
        tool("echo")("b").peek(label="q").run(validate=False, output=override)
    finally:
        clichain.set_output(None)
    assert buffer.getvalue() == "[p] a\n"
    assert override.getvalue() == "[q] b\n"


def test_meter_to_file_object():
    buffer = io.StringIO()
    tool("seq")("1", "3").meter(label="m", to=buffer, interval=0).run()
    assert buffer.getvalue().splitlines()[-1].startswith("[m] ")
    assert buffer.getvalue().splitlines()[-1].endswith("3 lines")


def test_wrap_at_pipe_boundaries():
    lines = wrap("> ", "aaaa | bbbb | cccc", width=10, pad="  ")
    assert lines == ["> aaaa", "  | bbbb", "  | cccc"]


def test_reprs():
    assert repr(tool("echo")("hi")) == "Cmd(echo hi)"
    assert repr(tool("echo")("hi").pipe(tool("cat")())) == "Pipeline(echo hi | cat)"
