"""Reports and exit codes: the JUnit, HTML and JSON outputs, fingerprints
that read the same on any host, and the codes ``jig`` exits with."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from jig import cli
from jig.errors import (
    ConfigError,
    JigError,
    MachineAbortedError,
    RenodeError,
    SimulationError,
    ToolchainError,
)
from jig.lang import RunOptions
from jig.report.html import link, write_html
from jig.report.report import (
    CaseReport,
    Step,
    SuiteReport,
    abort_kind,
    renode_line,
    renode_record,
    write_junit,
)
from jig.report.summary import PlanRun, summary, write_json


def suite(**fields: object) -> SuiteReport:
    report = SuiteReport(name="plan", source="plans/plan.jig")
    for key, value in fields.items():
        setattr(report, key, value)
    return report


# ------------------------------------------------------------------ exit codes


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (MachineAbortedError("dut", "fetch outside memory", 1.0), "failure"),
        (SimulationError("a step ran too long"), "infrastructure"),
        (RenodeError("Renode exited"), "infrastructure"),
        (ToolchainError("no Renode"), "infrastructure"),
        (ConfigError("bad board.yaml"), "config"),
        (JigError("bad argument"), "config"),
        (RuntimeError("a bug"), "infrastructure"),
    ],
)
def test_what_an_error_that_stopped_a_plan_counts_as(error: BaseException, kind: str) -> None:
    assert abort_kind(error) == kind
    assert cli.exit_code_for(error) == {"failure": 1, "config": 2, "infrastructure": 3}[kind]


def test_a_suite_exit_code() -> None:
    assert suite().exit_code() == 0
    assert suite(cases=[CaseReport("t", "SKIP")]).exit_code() == 0
    assert suite(cases=[CaseReport("t", "FAIL")]).exit_code() == 1
    assert suite(cases=[CaseReport("t", "ERROR")]).exit_code() == 1
    assert suite(teardown=CaseReport("(teardown)", "ERROR")).exit_code() == 1
    assert suite(aborted="setup FAIL: x", abort_kind="failure").exit_code() == 1
    assert suite(aborted="unknown board", abort_kind="config").exit_code() == 2
    assert suite(aborted="no Renode", abort_kind="infrastructure").exit_code() == 3
    assert suite(aborted="no Renode", abort_kind="infrastructure").status == "ERROR"
    assert suite(aborted="setup FAIL: x", abort_kind="failure").status == "FAIL"


# ------------------------------------------------------------------- fingerprints


def test_fingerprints_ignore_where_the_project_is(tmp_path: Path) -> None:
    def run(root: str) -> SuiteReport:
        case = CaseReport("t", "FAIL", f"{root}/plans/x.jig:3:1: image {root}/build/a.bin missing")
        case.steps.append(Step(1.0, "main", "x.jig:3:1", "call", "load()", "fail", f"{root}\\b"))
        return suite(
            cases=[case],
            aborted=f"stopped: {root}/x",
            path_anchors={root: ""},
            wall_seconds=float(len(root)),
        )

    first, second = run("C:/work/one"), run("/home/ci/checkout")
    assert first.fingerprint() == second.fingerprint()
    assert run("C:/work/one").fingerprint() != suite().fingerprint()


# ---------------------------------------------------------------------- JUnit


def test_junit_has_setup_teardown_skips_and_times(tmp_path: Path) -> None:
    report = suite(
        setup=CaseReport("(setup)", "PASS", wall_seconds=1.5, simulated_seconds=2.0, line=3),
        cases=[
            CaseReport("passes", "PASS", wall_seconds=0.25, simulated_seconds=4.0, line=7),
            CaseReport("skipped", "SKIP", "not selected (-k, --tags)", line=9),
            CaseReport("fails", "FAIL", "expected 3\nmore", file="plans/plan.jig", line=11),
        ],
        teardown=CaseReport("(teardown)", "ERROR", "cleanup broke", line=20),
        wall_seconds=9.0,
    )
    for case in report.all_cases():
        case.file = "plans/plan.jig"
    root = ET.parse(write_junit([report], tmp_path / "junit.xml")).getroot()
    (element,) = root.findall("testsuite")
    assert {k: element.get(k) for k in ("tests", "failures", "errors", "skipped")} == {
        "tests": "5",
        "failures": "1",
        "errors": "1",
        "skipped": "1",
    }
    names = [case.get("name") for case in element.findall("testcase")]
    assert names == ["(setup)", "passes", "skipped", "fails", "(teardown)"]
    passes = element.find("testcase[@name='passes']")
    assert passes is not None
    assert (passes.get("time"), passes.get("file"), passes.get("line")) == (
        "0.250",
        "plans/plan.jig",
        "7",
    )
    simulated = passes.find("properties/property[@name='simulated_seconds']")
    assert simulated is not None and simulated.get("value") == "4.000000"
    skipped = element.find("testcase[@name='skipped']/skipped")
    assert skipped is not None and skipped.get("message") == "not selected (-k, --tags)"
    failure = element.find("testcase[@name='fails']/failure")
    assert failure is not None and failure.get("message") == "expected 3"
    assert element.find("testcase[@name='(teardown)']/error") is not None
    assert not report.ok


def test_junit_names_a_plan_that_stopped_before_its_tests(tmp_path: Path) -> None:
    stopped = suite(aborted="no board named 'x'", abort_kind="config")
    setup_failed = suite(
        aborted="setup FAIL: no boot", setup=CaseReport("(setup)", "FAIL", "no boot")
    )
    root = ET.parse(write_junit([stopped, setup_failed], tmp_path / "j.xml")).getroot()
    first, second = root.findall("testsuite")
    error = first.find("testcase[@name='(plan)']/error")
    assert error is not None and error.text == "no board named 'x'"
    assert (first.get("tests"), first.get("errors")) == ("1", "1")
    # The setup's own case explains a setup failure: no second one.
    assert [c.get("name") for c in second.findall("testcase")] == ["(setup)"]
    assert (second.get("tests"), second.get("failures"), second.get("errors")) == ("1", "1", "0")


# ----------------------------------------------------------------------- HTML


def test_html_links_are_relative_to_the_page(tmp_path: Path) -> None:
    run = tmp_path / "out" / "runs" / "20261009-120000-plan"
    report = suite(
        transcripts={"dut.console": str(run / "dut" / "console.log")},
        recordings={"events.jsonl": (str(run / "events.jsonl"), 3)},
    )
    page = write_html([report], tmp_path / "out" / "report" / "index.html").read_text()
    assert "href='../runs/20261009-120000-plan/dut/console.log'" in page
    assert "href='../runs/20261009-120000-plan/events.jsonl'>events.jsonl</a> (3)" in page
    assert link("relative/file name.log", None) == "relative/file%20name.log"
    assert link(str(tmp_path / "a b.log"), None).startswith("file:")


# ----------------------------------------------------------------------- JSON


def test_the_json_summary_describes_every_plan_run(tmp_path: Path) -> None:
    passed = suite(
        setup=CaseReport("(setup)", "PASS", file="plans/plan.jig", line=2),
        cases=[CaseReport("t", "PASS", simulated_seconds=1.5, checks=3, tags=["fast"])],
        simulated_seconds=1.5,
        run_directory="out/runs/x",
    )
    broken = suite(aborted="no Renode", abort_kind="infrastructure")
    runs = [PlanRun(passed, 1, 2, True), PlanRun(broken)]
    document = json.loads(write_json(runs, 3, tmp_path / "summary.json").read_text())
    assert document == summary(runs, 3) | {"plans": document["plans"]}
    assert document["schema"] == 1 and document["exit_code"] == 3
    first, second = document["plans"]
    assert first["status"] == "PASS" and first["exit_code"] == 0
    assert (first["attempt"], first["repeat"], first["repeatable"]) == (1, 2, True)
    assert first["counts"] == {"PASS": 1, "FAIL": 0, "ERROR": 0, "SKIP": 0}
    assert first["fingerprint"] == passed.fingerprint()
    assert [c["block"] for c in first["cases"]] == ["setup", "test"]
    assert first["cases"][1] | {"wall_seconds": 0} == {
        "name": "t",
        "block": "test",
        "status": "PASS",
        "file": "",
        "line": 0,
        "tags": ["fast"],
        "checks": 3,
        "simulated_seconds": 1.5,
        "wall_seconds": 0,
        "detail": "",
    }
    assert second["status"] == "ERROR" and second["abort_kind"] == "infrastructure"
    assert second["exit_code"] == 3 and second["repeatable"] is None


def test_a_run_without_renode_reports_none(tmp_path: Path) -> None:
    report = suite(aborted="no Renode", abort_kind="infrastructure")
    assert renode_line(report) == ""
    (plan,) = summary([PlanRun(report)], 3)["plans"]
    assert plan["renode"] is None
    root = write_junit([report], tmp_path / "junit.xml").read_text(encoding="utf-8")
    assert "renode_version" not in root and "model_set." not in root
    assert "Renode " not in write_html([report], tmp_path / "r.html").read_text(encoding="utf-8")


def test_the_renode_details_come_from_the_session_record(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "20261010-040000-plan"
    record = run / "renode" / "session.json"
    record.parent.mkdir(parents=True)
    # What RenodeSession.start writes (jig.renode.session).
    record.write_text(
        json.dumps(
            {
                "renode": {
                    "executable": "/opt/renode/renode",
                    "root": "/opt/renode",
                    "assemblies": "/opt/renode",
                    "version": "1.17.0",
                    "build": "1.17.0+20260907gitf1dd1b4af",
                    "build_type": "Release",
                    "runtime": ".NET 8",
                    "status": "tested",
                    "source": "JIG_RENODE",
                },
                "bridge": {"protocol": 1, "port": 50123, "pid": 4242},
                "process_startup_seconds": 4.1,
                "startup_seconds": 4.36049,
                "model_sets": [
                    {"name": "jig", "files": 26, "seconds": 2.2412},
                    {"name": "board", "files": 1, "seconds": 0.3},
                ],
            }
        ),
        encoding="utf-8",
    )
    report = suite(run_directory=str(run))
    assert report.renode_details() == {
        "version": "1.17.0",
        "build": "1.17.0+20260907gitf1dd1b4af",
        "runtime": ".NET 8",
        "status": "tested",
        "executable": "/opt/renode/renode",
        "startup_seconds": 4.36,
        "model_sets": [
            {"name": "jig", "files": 26, "seconds": 2.241},
            {"name": "board", "files": 1, "seconds": 0.3},
        ],
    }
    assert renode_line(report) == (
        "Renode 1.17.0 (1.17.0+20260907gitf1dd1b4af, .NET 8); "
        "models: jig 26 files 2.24 s, board 1 file 0.30 s"
    )
    (plan,) = summary([PlanRun(report)], 0)["plans"]
    assert plan["renode"]["model_sets"][1] == {"name": "board", "files": 1, "seconds": 0.3}
    record.write_text("{not json", encoding="utf-8")
    assert renode_record(record) == {}
    assert renode_record(tmp_path / "absent.json") == {}


# ------------------------------------------------------------------------ CLI


def fake_runs(monkeypatch: pytest.MonkeyPatch, reports: list[SuiteReport]) -> list[Path]:
    """Make ``jig run`` return ``reports`` in turn; returns the plans it ran."""
    import jig.lang

    ran: list[Path] = []
    queue = list(reports)

    def run_plan(path: Path, options: RunOptions, *, seed: int = 0) -> SuiteReport:
        ran.append(path)
        return queue.pop(0)

    monkeypatch.setattr(jig.lang, "run_plan", run_plan)
    return ran


def test_jig_run_exits_with_the_highest_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("a.jig", "b.jig"):
        (tmp_path / name).write_text("", encoding="utf-8")
    ran = fake_runs(
        monkeypatch,
        [suite(cases=[CaseReport("t", "FAIL", "x")]), suite(aborted="bad", abort_kind="config")],
    )
    out = tmp_path / "result.json"
    pattern = str(tmp_path / "*.jig")
    assert cli.main(["-q", "run", pattern, "--json", str(out)]) == 2
    assert [p.name for p in ran] == ["a.jig", "b.jig"]  # the wildcard expanded, sorted
    assert json.loads(out.read_text())["exit_code"] == 2
    assert "0 of 2 plan runs passed" in capsys.readouterr().out


def test_jig_run_counts_a_run_that_did_not_repeat_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "p.jig"
    plan.write_text("", encoding="utf-8")
    fake_runs(monkeypatch, [suite(simulated_seconds=1.0), suite(simulated_seconds=2.0)])
    out = tmp_path / "result.json"
    assert cli.main(["-q", "run", str(plan), "--repeat", "2", "--json", str(out)]) == 1
    assert "NOT REPEATABLE: 2 different results in 2 runs" in capsys.readouterr().out
    plans = json.loads(out.read_text())["plans"]
    assert [p["repeatable"] for p in plans] == [False, False]
    fake_runs(monkeypatch, [suite(), suite()])
    assert cli.main(["-q", "run", str(plan), "--repeat", "2"]) == 0


def test_jig_run_without_renode_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JIG_RENODE", str(tmp_path / "no-renode-here"))
    plan = tmp_path / "plan.jig"
    plan.write_text('system { board b: "example-stm32" }\ntest "t" { check true }\n')
    out = tmp_path / "junit.xml"
    assert cli.main(["-q", "run", str(plan), "--junit", str(out)]) == 3
    assert "no Renode executable there" in capsys.readouterr().out
    assert ET.parse(out).getroot().find("testsuite/testcase[@name='(plan)']/error") is not None


def test_jig_run_with_a_broken_plan_exits_2(tmp_path: Path) -> None:
    plan = tmp_path / "plan.jig"
    plan.write_text('test "t" { check }\n', encoding="utf-8")
    assert cli.main(["-q", "run", str(plan)]) == 2
    missing = tmp_path / "missing" / "*.jig"
    assert cli.main(["-q", "run", str(missing)]) == 2


def test_jig_exits_130_when_interrupted_and_3_on_a_bug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jig.lang

    plan = tmp_path / "p.jig"
    plan.write_text("", encoding="utf-8")

    def interrupted(*args: object, **kwargs: object) -> SuiteReport:
        raise KeyboardInterrupt

    def broken(*args: object, **kwargs: object) -> SuiteReport:
        raise RuntimeError("a bug in Jig")

    monkeypatch.setattr(jig.lang, "run_plan", interrupted)
    assert cli.main(["-q", "run", str(plan)]) == 130
    monkeypatch.setattr(jig.lang, "run_plan", broken)
    assert cli.main(["-q", "run", str(plan)]) == 3


def test_jig_boards_lists_where_each_board_comes_from(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from .conftest import FIXTURES

    monkeypatch.setenv("JIG_BOARD_PATH", str(FIXTURES / "extra" / "boards"))
    assert cli.main(["-q", "boards"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == [
        "example-extra",
        "example-node",
        "example-package",
        "example-stm32",
        "example-zynq",
    ]
    assert lines[0].endswith("(JIG_BOARD_PATH)")
    assert lines[1].endswith("(jig.toml [paths] boards)")
    monkeypatch.setenv("JIG_BOARD_PATH", str(FIXTURES / "shadow" / "boards"))
    assert cli.main(["-q", "boards"]) == 2


def test_jig_check_loads_plugins_and_checks_codecs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["-q", "check", "example-node", "example-package"]) == 1
    out = capsys.readouterr().out
    assert "example-node: 1 problem(s)" in out and "firmware app:" in out
    assert "example-package: 1 problem(s)" in out and "stm32.firmware.elf:" in out


def test_python_runs_the_module(tmp_path: Path) -> None:
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "jig", "-q", "list", str(tmp_path / "absent.jig")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stderr
