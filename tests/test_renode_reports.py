"""``jig run`` on a real Renode: each report names the Renode that ran and
each model set's compile time, as the session recorded them in session.json.

Run with ``pytest -m renode`` (JIG_RENODE set, or a managed install)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from jig import cli

from .renode_support import real_renode

pytestmark = pytest.mark.renode


def test_reports_name_the_renode_that_ran(
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install = real_renode()
    monkeypatch.setenv("JIG_RENODE", str(install.executable))
    (project / "build" / "node.elf").write_bytes(b"\x7fELF")  # checked, never loaded
    out = tmp_path / "reports"
    plan = project / "plans" / "smoke.jig"
    arguments = ["--json", str(out / "summary.json"), "--junit", str(out / "junit.xml")]
    assert cli.main(["-q", "run", str(plan), *arguments, "--html", str(out / "r.html")]) == 0
    capsys.readouterr()

    (run,) = json.loads((out / "summary.json").read_text(encoding="utf-8"))["plans"]
    renode = run["renode"]
    assert (renode["version"], renode["build"], renode["runtime"]) == (
        install.version,
        install.build,
        install.runtime,
    )
    assert [entry["name"] for entry in renode["model_sets"]] == ["jig", "project"]
    assert all(entry["files"] > 0 and entry["seconds"] > 0 for entry in renode["model_sets"])
    assert renode["startup_seconds"] > sum(e["seconds"] for e in renode["model_sets"])

    record = json.loads(
        (Path(run["run_directory"]) / "renode" / "session.json").read_text(encoding="utf-8")
    )
    assert record["renode"]["build"] == renode["build"]
    assert [
        {"name": e["name"], "files": e["files"], "seconds": round(e["seconds"], 3)}
        for e in record["model_sets"]
    ] == renode["model_sets"]

    properties = {
        element.get("name"): element.get("value")
        for element in ET.parse(out / "junit.xml")
        .getroot()
        .findall("testsuite/properties/property")
    }
    assert properties["renode_version"] == install.version
    assert properties["renode_runtime"] == install.runtime
    assert float(properties["model_set.project.compile_seconds"]) > 0
    assert f"Renode {install.version} ({install.build}, {install.runtime})" in (
        out / "r.html"
    ).read_text(encoding="utf-8")
