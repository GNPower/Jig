"""Run reports: what each test did, in simulated time, for people and for CI.

A ``SuiteReport`` describes one plan run: its setup, cases and teardown, each
with the steps the interpreter logged (checks, calls, waits, log lines), the
values it recorded, and the outcome. ``write_junit`` writes JUnit XML for CI;
``write_html`` (``jig.report.html``) writes a page per run and ``write_json``
(``jig.report.summary``) a summary per plan run.

``fingerprint`` hashes everything a run decided and measured in simulated
time, and nothing that depends on the host: wall time is left out, and paths
in error details become relative to the project (or ``<out>``, ``<cache>``)
first, so two runs of the same plan with the same inputs have the same
fingerprint on any host.

Exit codes (``exit_code``): 0 pass, 1 a test failed (or a teardown, or a run
was not repeatable), 2 a usage or configuration error, 3 an infrastructure
error (Renode, the model compile, a firmware build), 130 an interrupt.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jig import project
from jig.errors import (
    ConfigError,
    JigError,
    MachineAbortedError,
    RenodeError,
    SimulationError,
    ToolchainError,
)

STATUSES = ("PASS", "FAIL", "ERROR", "SKIP")

EXIT_PASS = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INFRASTRUCTURE = 3
EXIT_INTERRUPT = 130

# What stopped a plan before it finished, by kind.
ABORT_FAILURE = "failure"  # the system under test failed (setup, a machine aborted)
ABORT_CONFIG = "config"  # the plan, a board or the project is wrong
ABORT_INFRASTRUCTURE = "infrastructure"  # Renode, a model compile, a firmware build


def abort_kind(error: BaseException) -> str:
    """How an error that stopped a plan counts for the exit code."""
    if isinstance(error, MachineAbortedError):
        return ABORT_FAILURE
    if isinstance(error, (ToolchainError, RenodeError, SimulationError)):
        return ABORT_INFRASTRUCTURE
    if isinstance(error, (ConfigError, JigError)):
        return ABORT_CONFIG
    return ABORT_INFRASTRUCTURE


@dataclass
class Step:
    time: float  # simulated seconds when the step finished
    task: str
    where: str  # file:line:column
    kind: str  # check | call | wait | log | record | task | error
    text: str
    outcome: str = "ok"  # ok | fail | error | info
    detail: str = ""
    duration: float = 0.0  # simulated seconds the step took


@dataclass
class CaseReport:
    name: str
    status: str  # PASS | FAIL | ERROR | SKIP
    detail: str = ""
    simulated_seconds: float = 0.0
    started: float = 0.0
    finished: float = 0.0
    where: str = ""
    tags: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    records: dict[str, list[tuple[float, Any]]] = field(default_factory=dict)
    checks: int = 0
    wall_seconds: float = 0.0
    file: str = ""  # the plan file, relative to the project root
    line: int = 0
    error: BaseException | None = field(default=None, repr=False, compare=False)

    def record(self, name: str, time: float, value: Any) -> None:
        self.records.setdefault(name, []).append((time, value))


@dataclass
class SuiteReport:
    name: str
    source: str
    cases: list[CaseReport] = field(default_factory=list)
    aborted: str = ""  # why the suite could not run, if it could not
    abort_kind: str = ""  # ABORT_FAILURE, ABORT_CONFIG or ABORT_INFRASTRUCTURE
    wall_seconds: float = 0.0
    simulated_seconds: float = 0.0
    run_directory: str = ""
    system: list[str] = field(default_factory=list)  # one line per board, bus and node
    setup: CaseReport | None = None
    teardown: CaseReport | None = None
    seed: int = 0
    transcripts: dict[str, str] = field(default_factory=dict)  # label -> path
    recordings: dict[str, tuple[str, int]] = field(default_factory=dict)  # file -> (path, entries)
    events: dict[str, int] = field(default_factory=dict)  # "source kind" -> how many
    path_anchors: dict[str, str] = field(default_factory=dict)  # see project.relativize
    renode: dict[str, Any] = field(default_factory=dict)  # see renode_details

    def count(self, status: str) -> int:
        return sum(1 for case in self.cases if case.status == status)

    @property
    def ok(self) -> bool:
        """Nothing stopped the plan and every block passed (or was skipped)."""
        return not self.aborted and all(
            case.status in ("PASS", "SKIP") for case in self.all_cases()
        )

    @property
    def status(self) -> str:
        if self.ok:
            return "PASS"
        if self.aborted and self.abort_kind != ABORT_FAILURE:
            return "ERROR"
        return "FAIL"

    def exit_code(self) -> int:
        if self.aborted and self.abort_kind == ABORT_INFRASTRUCTURE:
            return EXIT_INFRASTRUCTURE
        if self.aborted and self.abort_kind == ABORT_CONFIG:
            return EXIT_USAGE
        return EXIT_PASS if self.ok else EXIT_FAILURE

    def fingerprint(self) -> str:
        """SHA-256 of the run's decisions and measurements in simulated time."""
        anchors = self.path_anchors
        body = {
            "aborted": project.relativize(self.aborted, anchors),
            "cases": [_case_digest(case, anchors) for case in self.all_cases()],
            "simulated": round(self.simulated_seconds, 9),
        }
        encoded = json.dumps(body, sort_keys=True, default=_json_default).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def all_cases(self) -> list[CaseReport]:
        """Setup, the cases and teardown, as they ran."""
        return [c for c in (self.setup, *self.cases, self.teardown) if c is not None]

    _all_cases = all_cases

    def renode_details(self) -> dict[str, Any]:
        """The Renode the run used: ``renode`` when set, else what the run
        directory's ``renode/session.json`` recorded (``renode_record``); {}
        when the plan stopped before Renode started."""
        if not self.renode and self.run_directory:
            self.renode = renode_record(Path(self.run_directory) / SESSION_RECORD)
        return self.renode


# Where a run directory keeps the record RenodeSession.start writes (the
# simulation's session works in <run>/renode).
SESSION_RECORD = Path("renode") / "session.json"


def renode_record(path: Path) -> dict[str, Any]:
    """What a report says about the Renode behind a run, from the session's
    ``session.json`` (``jig.renode.session``): the version, build and .NET
    runtime ``renode --version`` gave, Jig's verdict on that build (tested,
    supported or unsupported), the executable, how long the session took to
    start, and each model set's compile time, Jig's own first. {} when the
    file is missing or unreadable. None of it enters the fingerprint: it
    describes the host, not the run."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        install = record["renode"]
        sets = record.get("model_sets") or []
        startup = record.get("startup_seconds")
        return {
            "version": str(install["version"]),
            "build": str(install.get("build", "")),
            "runtime": str(install.get("runtime", "")),
            "status": str(install.get("status", "")),
            "executable": str(install.get("executable", "")),
            "startup_seconds": None if startup is None else round(float(startup), 3),
            "model_sets": [
                {
                    "name": str(entry["name"]),
                    "files": int(entry["files"]),
                    "seconds": round(float(entry["seconds"]), 3),
                }
                for entry in sets
            ],
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def renode_line(suite: SuiteReport) -> str:
    """One line for people: ``Renode 1.17.0 (<build>, <runtime>); models: jig 26
    files 1.52 s, ...``; empty when no Renode ran."""
    renode = suite.renode_details()
    if not renode:
        return ""
    line = f"Renode {renode['version']} ({renode['build']}, {renode['runtime']})"
    sets = renode.get("model_sets") or []
    if sets:
        line += "; models: " + ", ".join(
            f"{entry['name']} {entry['files']} file{'' if entry['files'] == 1 else 's'} "
            f"{entry['seconds']:.2f} s"
            for entry in sets
        )
    return line


def _case_digest(case: CaseReport, anchors: dict[str, str]) -> dict[str, Any]:
    def plain(text: str) -> str:
        return project.relativize(text, anchors)

    return {
        "name": case.name,
        "status": case.status,
        "detail": plain(case.detail),
        "finished": round(case.finished, 9),
        "steps": [
            (round(s.time, 9), s.task, s.kind, s.text, s.outcome, plain(s.detail))
            for s in case.steps
        ],
        "records": {
            name: [(round(t, 9), v) for t, v in values]
            for name, values in sorted(case.records.items())
        },
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "plain"):
        return value.plain()
    return repr(value)


def step_log(case: CaseReport) -> str:
    """The case's steps as text, one per line."""
    lines = []
    for step in case.steps:
        mark = {"ok": " ", "fail": "!", "error": "E", "info": "-"}.get(step.outcome, "?")
        line = f"{step.time:12.6f} {mark} [{step.task}] {step.text}"
        if step.detail:
            line += f"  -- {step.detail}"
        lines.append(line)
    return "\n".join(lines)


def _testcase(parent: ET.Element, suite: SuiteReport, case: CaseReport) -> None:
    attributes = {
        "classname": suite.name,
        "name": case.name,
        "time": f"{case.wall_seconds:.3f}",
    }
    if case.file:
        attributes["file"] = case.file
    if case.line:
        attributes["line"] = str(case.line)
    element = ET.SubElement(parent, "testcase", attributes)
    properties = ET.SubElement(element, "properties")
    ET.SubElement(
        properties, "property", name="simulated_seconds", value=f"{case.simulated_seconds:.6f}"
    )
    if case.status == "FAIL":
        ET.SubElement(element, "failure", message=_first_line(case.detail)).text = case.detail
    elif case.status == "ERROR":
        ET.SubElement(element, "error", message=_first_line(case.detail)).text = case.detail
    elif case.status == "SKIP":
        ET.SubElement(element, "skipped", message=_first_line(case.detail or "skipped"))
    if case.steps:
        ET.SubElement(element, "system-out").text = step_log(case)


def write_junit(suites: list[SuiteReport], destination: Path) -> Path:
    """JUnit XML: one testsuite per plan run; setup and teardown are test
    cases; ``time`` is wall seconds and each case carries its simulated
    seconds as a property."""
    root = ET.Element("testsuites")
    for suite in suites:
        cases = suite.all_cases()
        # A plan that stopped before its setup reported (or without one) gets a
        # case of its own, so CI shows why.
        stopped = suite.aborted and (suite.setup is None or suite.setup.status == "PASS")
        failures = sum(1 for case in cases if case.status == "FAIL")
        errors = sum(1 for case in cases if case.status == "ERROR")
        element = ET.SubElement(
            root,
            "testsuite",
            name=suite.name,
            file=suite.source,
            tests=str(len(cases) + (1 if stopped else 0)),
            failures=str(failures),
            errors=str(errors + (1 if stopped else 0)),
            skipped=str(sum(1 for case in cases if case.status == "SKIP")),
            time=f"{suite.wall_seconds:.3f}",
        )
        properties = ET.SubElement(element, "properties")
        for name, value in (
            ("simulated_seconds", f"{suite.simulated_seconds:.6f}"),
            ("run_directory", suite.run_directory),
            ("seed", str(suite.seed)),
            ("fingerprint", suite.fingerprint()),
            *_renode_properties(suite),
        ):
            ET.SubElement(properties, "property", name=name, value=value)
        for case in cases:
            _testcase(element, suite, case)
        if stopped:
            case_element = ET.SubElement(element, "testcase", classname=suite.name, name="(plan)")
            ET.SubElement(case_element, "error", message="plan aborted").text = suite.aborted
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)
    return destination


def _renode_properties(suite: SuiteReport) -> list[tuple[str, str]]:
    """``renode_version``, ``renode_build``, ``renode_runtime``,
    ``renode_startup_seconds`` and ``model_set.<name>.compile_seconds``."""
    renode = suite.renode_details()
    if not renode:
        return []
    properties = [
        ("renode_version", str(renode["version"])),
        ("renode_build", str(renode["build"])),
        ("renode_runtime", str(renode["runtime"])),
    ]
    if renode.get("startup_seconds") is not None:
        properties.append(("renode_startup_seconds", f"{renode['startup_seconds']:.3f}"))
    for entry in renode.get("model_sets") or []:
        properties.append((f"model_set.{entry['name']}.compile_seconds", f"{entry['seconds']:.3f}"))
    return properties


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "failed"
    return line[:200]
