"""The plan language (``.jig``): parse a plan, build its system, run it.

    report = run_plan(Path("plans/smoke.jig"), RunOptions(boot="jtag"))

A plan declares the boards, buses and nodes it needs. ``run_plan`` builds
them into a fresh emulation, runs setup, the selected tests and teardown, and
returns a ``SuiteReport``. The plan runs in its project (``jig.project``):
the nearest ``jig.toml`` above it, unless ``$JIG_PROJECT`` names another.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path

from jig import project
from jig.errors import JigError
from jig.lang.interpreter import (
    CheckFailedError,
    Interpreter,
    RunOptions,
    StepFailedError,
    describe_plan,
)
from jig.lang.parser import PlanError, Program, load
from jig.report.report import ABORT_CONFIG, SuiteReport, abort_kind
from jig.system.system import DEFAULT_STEP, Simulation

__all__ = [
    "CheckFailedError",
    "Interpreter",
    "PlanError",
    "Program",
    "RunOptions",
    "StepFailedError",
    "describe_plan",
    "load",
    "run_plan",
]

log = logging.getLogger(__name__)


def plan_title(program: Program) -> str:
    systems = program.main.items("System")
    if systems and systems[0].title is not None:
        from jig.lang.parser import unquote

        return unquote(systems[0].title.value)
    return program.main.path.stem


def run_plan(
    path: Path, options: RunOptions | None = None, *, seed: int = 0, step: float | None = None
) -> SuiteReport:
    """Run one plan in its own emulation and report what happened."""
    options = options or RunOptions()
    started = time.monotonic()
    report = SuiteReport(name=path.stem, source=str(path), seed=seed)
    try:
        chosen = project.discover(path)
    except JigError as exc:
        report.aborted, report.abort_kind = str(exc), ABORT_CONFIG
        report.wall_seconds = time.monotonic() - started
        return report
    with project.activate(chosen):
        report.path_anchors = project.path_anchors(chosen)
        _run(path, options, seed, step, report)
    report.wall_seconds = time.monotonic() - started
    return report


def _run(
    path: Path, options: RunOptions, seed: int, step: float | None, report: SuiteReport
) -> None:
    try:
        program = load(path)
    except PlanError as exc:
        report.aborted, report.abort_kind = str(exc), ABORT_CONFIG
        return
    report.name = plan_title(program)
    try:
        simulation = Simulation(
            name=path.stem,
            seed=seed,
            step=DEFAULT_STEP if step is None else step,
            renode=options.renode,
            allow_untested_renode=options.allow_untested_renode,
        )
        with simulation:
            report.run_directory = str(simulation.workdir)
            interpreter = Interpreter(program, simulation, options)
            report.system = interpreter.build_system()
            interpreter.start_system()
            for machine in simulation.machines.values():
                for interface in machine.ports:
                    report.transcripts[f"{machine.name}.{interface}"] = str(
                        machine.config.workdir / f"{interface}.log"
                    )
            interpreter.run(report)
            report.simulated_seconds = simulation.now
            recorder = simulation.recorder
            report.recordings = {
                name: (str(path), recorder.counts.get(name, 0))
                for name, path in sorted(recorder.files.items())
            }
            report.events = dict(
                sorted(Counter(f"{e.source} {e.kind}" for e in simulation.events).items())
            )
    except JigError as exc:
        if not report.aborted:
            report.aborted, report.abort_kind = str(exc), abort_kind(exc)
