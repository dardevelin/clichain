"""A10: sbom() is a real CycloneDX document; manifest() is the run record."""

from __future__ import annotations

import json
import re

import clichain
from clichain import tool


def _result():
    return tool("echo")("hello").pipe(tool("grep")("hello")).run()


def test_sbom_is_cyclonedx():
    bom = _result().sbom()
    assert bom["bomFormat"] == "CycloneDX"
    assert bom["specVersion"] == "1.6"
    assert re.fullmatch(r"urn:uuid:[0-9a-f-]{36}", bom["serialNumber"])
    assert bom["version"] == 1
    generator = bom["metadata"]["tools"]["components"][0]
    assert generator["name"] == "clichain"
    assert generator["version"] == clichain.__version__
    assert bom["metadata"]["timestamp"]


def test_sbom_components_are_the_tools():
    bom = _result().sbom()
    names = [c["name"] for c in bom["components"]]
    assert names == ["echo", "grep"]
    for component in bom["components"]:
        assert component["type"] == "application"
        assert component["bom-ref"] == f"tool:{component['name']}"
        props = {p["name"]: p["value"] for p in component["properties"]}
        assert props["clichain:path"].endswith(component["name"])


def test_sbom_json_roundtrip():
    result = _result()
    assert json.loads(result.sbom_json())["bomFormat"] == "CycloneDX"
    assert json.loads(result.sbom_json(indent=4))["components"]


def test_manifest_keeps_run_record():
    data = _result().manifest()
    assert data["generator"]["name"] == "clichain"
    assert data["exit_code"] == 0
    assert [t["name"] for t in data["tools"]] == ["echo", "grep"]
    assert [n["type"] for n in data["call_tree"]] == ["CmdStep", "CmdStep"]
    assert json.loads(_result().manifest_json())["timestamp"]
