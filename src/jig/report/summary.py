"""``jig run --json PATH``: one JSON document describing every plan run::

    {
      "schema": 1,
      "jig": "0.1.0",
      "exit_code": 1,
      "plans": [
        {
          "name": "smoke", "source": "plans/smoke.jig", "status": "FAIL",
          "attempt": 1, "repeat": 1, "repeatable": null, "seed": 0,
          "fingerprint": "<sha256>", "exit_code": 1,
          "counts": {"PASS": 3, "FAIL": 1, "ERROR": 0, "SKIP": 0},
          "simulated_seconds": 12.5, "wall_seconds": 31.0,
          "run_directory": "out/runs/...", "aborted": "", "abort_kind": "",
          "renode": {
            "version": "1.17.0", "build": "1.17.0+20260906gitf1dd1b4af",
            "runtime": ".NET 8.0.10", "status": "tested",
            "executable": "/opt/renode/renode", "startup_seconds": 4.4,
            "model_sets": [{"name": "jig", "files": 26, "seconds": 1.7}]
          },
          "cases": [
            {"name": "(setup)", "block": "setup", "status": "PASS",
             "file": "plans/smoke.jig", "line": 12, "checks": 0,
             "simulated_seconds": 2.0, "wall_seconds": 4.1, "detail": ""}
          ]
        }
      ]
    }

``status`` is PASS, FAIL (a test or teardown failed) or ERROR (the plan could
not run); ``repeatable`` says, for a plan run with ``--repeat``, whether
every repetition gave one fingerprint (null for a single run). ``renode``
describes the Renode the run started, from the run directory's
``renode/session.json`` (``jig.report.report.renode_record``): its version,
build and .NET runtime, its status against Jig's tested builds (tested,
supported or unsupported), the session's start-up seconds and each model
set's compile seconds. It is null when the plan stopped before Renode
started.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jig import __version__
from jig.report.report import STATUSES, CaseReport, SuiteReport

SCHEMA = 1


@dataclass(frozen=True)
class PlanRun:
    """One run of one plan, as ``jig run`` made it."""

    report: SuiteReport
    attempt: int = 1
    repeat: int = 1
    repeatable: bool | None = None


def _case(case: CaseReport, block: str) -> dict[str, Any]:
    return {
        "name": case.name,
        "block": block,
        "status": case.status,
        "file": case.file,
        "line": case.line,
        "tags": list(case.tags),
        "checks": case.checks,
        "simulated_seconds": round(case.simulated_seconds, 9),
        "wall_seconds": round(case.wall_seconds, 3),
        "detail": case.detail,
    }


def plan_summary(run: PlanRun) -> dict[str, Any]:
    report = run.report
    cases = []
    if report.setup is not None:
        cases.append(_case(report.setup, "setup"))
    cases += [_case(case, "test") for case in report.cases]
    if report.teardown is not None:
        cases.append(_case(report.teardown, "teardown"))
    return {
        "name": report.name,
        "source": report.source,
        "status": report.status,
        "attempt": run.attempt,
        "repeat": run.repeat,
        "repeatable": run.repeatable,
        "seed": report.seed,
        "fingerprint": report.fingerprint(),
        "exit_code": report.exit_code(),
        "counts": {status: report.count(status) for status in STATUSES},
        "simulated_seconds": round(report.simulated_seconds, 9),
        "wall_seconds": round(report.wall_seconds, 3),
        "run_directory": report.run_directory,
        "aborted": report.aborted,
        "abort_kind": report.abort_kind,
        "renode": report.renode_details() or None,
        "cases": cases,
    }


def summary(runs: Sequence[PlanRun], exit_code: int) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "jig": __version__,
        "exit_code": exit_code,
        "plans": [plan_summary(run) for run in runs],
    }


def write_json(runs: Sequence[PlanRun], exit_code: int, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary(runs, exit_code), indent=2, default=repr) + "\n"
    destination.write_text(text, encoding="utf-8", newline="\n")
    return destination
