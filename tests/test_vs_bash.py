"""Differential tests: clichain pipelines must produce exactly the bytes bash produces.

Each case is a seeded random pipeline over seeded random input: binary bytes, CRLF,
unicode, empty lines, a missing final newline, lines longer than the 64 KB read chunk,
and early-exit readers (head). Python bridges are compared with their shell meaning:
filter(needle in line) is a perl line filter that prints matching lines exactly as read
(grep would add a newline to an unterminated last line), peek and meter are ``cat``.
"""

from __future__ import annotations

import os
import random
import shlex
import string
import subprocess
from pathlib import Path

import pytest

from clichain import Pipeline, tool

COMMANDS: list[list[str]] = [
    ["sort"],
    ["sort", "-r"],
    ["uniq"],
    ["cat"],
    ["tr", "a-z", "A-Z"],
    ["cut", "-c", "1-8"],
    ["head", "-n", "3"],
    ["tail", "-n", "5"],
]
NEEDLES = ["a", "e", "x", "ö", "Z"]


def _random_line(rng: random.Random) -> bytes:
    kind = rng.random()
    if kind < 0.55:
        return "".join(rng.choices(string.ascii_letters + " \t", k=rng.randint(0, 40))).encode()
    if kind < 0.7:
        return rng.choice(["héllo wörld ✓", "naïve café", "Zürich ö"]).encode()
    if kind < 0.85:  # arbitrary bytes, invalid UTF-8 and \r included (never \n)
        return bytes(rng.choice(_NOT_NEWLINE) for _ in range(rng.randint(1, 30)))
    return b""


_NOT_NEWLINE = [b for b in range(256) if b != 10]


def _random_input(rng: random.Random) -> bytes:
    count = rng.choice([0, 1, 3, 50, 2_000, 30_000])
    lines = [_random_line(rng) for _ in range(count)]
    for _ in range(rng.choice([0, 0, 1, 3]) if lines else 0):
        # longer than one 64 KB read, so lines straddle chunk boundaries
        lines[rng.randrange(len(lines))] = b"y" * rng.randint(70_000, 150_000)
    data = b"".join(line + rng.choice([b"\n", b"\n", b"\r\n"]) for line in lines)
    if data and rng.random() < 0.3:
        data = data.rstrip(b"\r\n")  # no final newline
    return data


def _random_case(seed: int, tmp_path: Path) -> tuple[Pipeline, str, bytes | None, Path | None]:
    """A clichain pipeline, the equivalent bash command, its stdin, and a redirect target."""
    rng = random.Random(seed)
    data = _random_input(rng)
    stages: list[tuple[str, object]] = []
    for _ in range(rng.randint(1, 4)):
        kind = rng.choices(["cmd", "filter", "peek", "meter"], weights=[4, 2, 1, 1])[0]
        stages.append((kind, rng.choice(COMMANDS) if kind == "cmd" else rng.choice(NEEDLES)))
    if stages[0][0] != "cmd":
        stages.insert(0, ("cmd", ["cat"]))  # a pipeline starts with a command

    pipeline: Pipeline | None = None
    shell: list[str] = []
    for kind, arg in stages:
        if kind == "cmd":
            argv = list(arg)  # type: ignore[call-overload]
            cmd = tool(argv[0])(*argv[1:])
            pipeline = cmd if pipeline is None else pipeline.pipe(cmd)
            shell.append(shlex.join(argv))
        elif kind == "filter":
            needle = str(arg)
            pipeline = pipeline.filter(lambda line, n=needle: n in line)  # type: ignore[union-attr]
            perl = "print if index($_, $ENV{NEEDLE}) >= 0"
            shell.append(f"NEEDLE={shlex.quote(needle)} perl -ne {shlex.quote(perl)}")
        elif kind == "peek":
            pipeline = pipeline.peek(fn=lambda line: None)  # type: ignore[union-attr]
            shell.append("cat")
        else:
            interval = rng.choice([0, 1.0])
            pipeline = pipeline.meter(to=lambda stats: None, interval=interval)  # type: ignore[union-attr]
            shell.append("cat")
    assert pipeline is not None

    stdin: bytes | None = None
    source = rng.choice(["feed", "file", "none"])
    if source == "feed":
        pipeline = pipeline.feed(data.decode("utf-8", "surrogateescape"))
        stdin = data
    elif source == "file":
        path = tmp_path / f"input-{seed}"
        path.write_bytes(data)
        # Tiny blocks only on small inputs: block_size=1 on megabytes is slow by design.
        sizes = [0, 1, 7, 4096, 65536] if len(data) < 200_000 else [0, 4096, 65536]
        pipeline = pipeline.from_file(path, block_size=rng.choice(sizes))
        shell[0] += f" < {shlex.quote(str(path))}"

    redirect = None
    if rng.random() < 0.25:
        redirect = tmp_path / f"out-{seed}"
        pipeline = pipeline.redirect(stdout=str(redirect))
        shell[-1] += f" > {shlex.quote(str(redirect) + '.bash')}"
    return pipeline, " | ".join(shell), stdin, redirect


@pytest.fixture(autouse=True)
def _c_locale(monkeypatch):
    monkeypatch.setenv("LC_ALL", "C")  # byte-wise sort/tr/cut, identical on both sides


@pytest.mark.parametrize("seed", range(200))
def test_same_bytes_as_bash(seed, tmp_path):
    pipeline, command, stdin, redirect = _random_case(seed, tmp_path)
    expected = subprocess.run(
        ["bash", "-c", command],
        input=stdin if stdin is not None else b"",
        capture_output=True,
        env={**os.environ, "LC_ALL": "C"},
        timeout=60,
    )
    assert expected.returncode == 0, expected.stderr

    result = pipeline.run(validate=False)

    assert result.ok, f"{command}\n{result.stderr}"  # early-exit readers are not failures
    assert result.stdout_bytes == expected.stdout, command
    if redirect is not None:
        assert redirect.read_bytes() == Path(str(redirect) + ".bash").read_bytes(), command
        assert result.stdout == ""


def test_cases_cover_the_interesting_shapes(tmp_path):
    """Guard against the generator drifting into only easy cases."""
    commands = [_random_case(seed, tmp_path)[1] for seed in range(60)]
    joined = "\n".join(commands)
    for needle in ("perl -ne", "head -n", "sort", "< ", "> ", "| cat"):
        assert needle in joined, needle
