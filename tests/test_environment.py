"""The suite must not depend on the environment it runs in (local shell vs CI)."""

from __future__ import annotations

import re
from pathlib import Path

import clichain
from clichain import errors

from .conftest import HERMETIC_ENV

# os.environ.get("NAME") / os.environ["NAME"] / "NAME" in os.environ, or a module
# constant in their place (e.g. DESCRIBE_ENV).
_READS = re.compile(
    r'os\.environ(?:\.get\(|\[)\s*(?:"(?P<lit>[A-Z_]+)"|(?P<const>[A-Z_]+))'
    r'|(?:"(?P<lit2>[A-Z_]+)"|(?P<const2>[A-Z_]{4,})) (?:not )?in os\.environ'
)


def test_every_variable_the_library_reads_is_pinned_in_tests():
    read: set[str] = set()
    for path in Path(clichain.__file__).parent.glob("*.py"):
        for match in _READS.finditer(path.read_text()):
            literal = match["lit"] or match["lit2"]
            constant = match["const"] or match["const2"]
            read.add(literal or getattr(errors, constant))
    assert read, "the scan found nothing: the pattern is out of date"
    assert read <= set(HERMETIC_ENV), f"pin these in tests/conftest.py: {read - set(HERMETIC_ENV)}"
