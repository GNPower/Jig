"""``jig doctor``: what Jig would run on, checked end to end.

Checks, in order: Python, the platform, the Jig package, its paths, every
Renode discovery source and the winner, the winner's version against the
supported range, Jig-managed installs against their pinned digests, a
headless start with the bridge up (seconds, protocol, the Renode build and
.NET runtime it reports), the start-up compile of Jig's models and of each
``--board`` model set, the .NET SDK (needed only for ``--sdk``) and free
space for an install. A check passes, warns or fails; any failure fails the
command.
"""

from __future__ import annotations

import importlib.metadata
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jig import __version__, project
from jig.errors import JigError
from jig.renode import discover, install, models
from jig.renode.assets import ASSETS, host_platform
from jig.renode.discover import RenodeConfig, RenodeInstall
from jig.renode.process import RenodeProcess
from jig.renode.session import compile_model_set

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.status != FAIL for check in self.checks)

    def add(self, name: str, status: str, summary: str, **details: Any) -> Check:
        check = Check(name, status, summary, dict(details))
        self.checks.append(check)
        return check

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "jig": __version__, "checks": [c.as_dict() for c in self.checks]}

    def text(self) -> str:
        width = max((len(c.name) for c in self.checks), default=0)
        lines = [f"{c.status.upper():<5} {c.name:<{width}}  {c.summary}" for c in self.checks]
        failed = sum(c.status == FAIL for c in self.checks)
        warned = sum(c.status == WARN for c in self.checks)
        lines.append(f"{len(self.checks)} checks: {failed} failed, {warned} warnings")
        return "\n".join(lines)


def run(
    *,
    explicit: Path | None,
    config: RenodeConfig | None,
    allow_untested: bool = False,
    boards: Sequence[str | Path] = (),
    start: bool = True,
) -> Report:
    report = Report()
    _python(report)
    _platform(report)
    _package(report)
    _paths(report)
    renode = _discovery(report, explicit, config, allow_untested)
    _managed(report)
    if renode is not None and start:
        _start(report, renode, boards)
    _dotnet(report)
    _space(report)
    return report


def _python(report: Report) -> None:
    version = platform.python_version()
    status = PASS if sys.version_info >= (3, 11) else FAIL
    report.add("python", status, f"{version} ({sys.executable})", executable=sys.executable)


def _platform(report: Report) -> None:
    details = {"system": platform.platform(), "machine": platform.machine()}
    try:
        key = host_platform()
    except JigError as exc:
        report.add("platform", WARN, f"{details['system']}: {exc}", **details)
        return
    report.add(
        "platform", PASS, f"{details['system']} {details['machine']} ({key})", key=key, **details
    )


def _package(report: Report) -> None:
    location = str(Path(__file__).resolve().parents[1])
    try:
        importlib.metadata.distribution("jig")
    except importlib.metadata.PackageNotFoundError:
        report.add("jig", PASS, f"jig-dsl {__version__} ({location})", location=location)
        return
    report.add(
        "jig",
        WARN,
        f"jig-dsl {__version__} ({location}); the unrelated PyPI package 'jig' is installed "
        "too and also provides a 'jig' command",
        location=location,
    )


def _paths(report: Report) -> None:
    paths = {
        "project": str(project.root()),
        "cache": str(project.cache_dir()),
        "output": str(project.output_dir()),
        "user_config": str(discover.user_config_file()),
        "temp": tempfile.gettempdir(),
    }
    report.add("paths", PASS, f"project {paths['project']}, cache {paths['cache']}", **paths)


def _discovery(
    report: Report, explicit: Path | None, config: RenodeConfig | None, allow_untested: bool
) -> RenodeInstall | None:
    try:
        survey = discover.survey(explicit=explicit, config=config, allow_untested=allow_untested)
    except JigError as exc:
        report.add("discovery", FAIL, str(exc))
        return None
    sources = [c.as_dict() for c in survey.candidates]
    selected = survey.selected
    if selected is None:
        problem = survey.explicit.problem if survey.explicit is not None else "no usable Renode"
        report.add(
            "discovery",
            FAIL,
            f"{problem}; install one with `jig renode install`",
            sources=sources,
            requirement=str(survey.requirement),
        )
        return None
    report.add(
        "discovery",
        PASS,
        f"{selected.executable} (from {selected.source})",
        sources=sources,
        requirement=str(survey.requirement),
    )
    status = {"tested": PASS, "supported": WARN, "unsupported": WARN}[selected.status]
    notes = {
        "tested": f"Renode {selected.version}, the tested build",
        "supported": f"Renode {selected.version}, build {selected.build}: in the supported "
        f"range {discover.SUPPORTED} but not the tested build",
        "unsupported": f"Renode {selected.version}: outside {discover.SUPPORTED}, allowed by "
        "--allow-untested-renode",
    }
    report.add("version", status, notes[selected.status], renode=selected.as_dict())
    return selected


def _managed(report: Report) -> None:
    installs = install.managed_installs()
    listed = [
        {"version": m.asset.version, "platform": m.asset.platform, "directory": str(m.directory),
         "sha256": m.asset.sha256, "legacy": m.legacy}
        for m in installs
    ]  # fmt: skip
    summary = (
        ", ".join(f"{m.asset.version} ({m.asset.platform}) at {m.directory}" for m in installs)
        if installs
        else f"none in {install.renode_dir()}"
    )
    report.add("managed", PASS, summary, installs=listed)


def _start(report: Report, renode: RenodeInstall, boards: Sequence[str | Path]) -> None:
    output = project.output_dir()
    output.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="doctor-", dir=output))
    process = RenodeProcess(workdir=workdir, install=renode, startup_timeout=180.0)
    jig_files = len(process.jig_models.files)
    try:
        process.start()
    except models.ModelCompileError as exc:
        report.add("start-up", FAIL, f"Jig's models do not compile on this Renode ({workdir})")
        report.add("models", FAIL, str(exc), diagnostics=[str(d) for d in exc.diagnostics])
        process.stop()
        return
    except JigError as exc:
        report.add("start-up", FAIL, f"{exc} ({workdir})", workdir=str(workdir))
        process.stop()
        return
    keep = False
    try:
        hello = process.client.hello
        version = process.client.execute("version").strip().splitlines()[0]
        report.add(
            "start-up",
            PASS,
            f"bridge up in {process.startup_seconds:.2f} s at {process.client.address} "
            f"(protocol {process.info.protocol}); {version}",
            seconds=process.startup_seconds,
            address=process.client.address,
            protocol=process.info.protocol,
            version=version,
            runtime=hello.get("runtime"),
            infrastructure=hello.get("infrastructure"),
        )
        compiled = [{"set": models.JIG_SET, "files": jig_files,
                     "seconds": process.startup_compile_seconds}]  # fmt: skip
        failures = []
        try:
            board_sets = models.board_sets(boards)
        except JigError as exc:
            board_sets = []
            failures.append(str(exc))
        for model_set in board_sets:
            try:
                timing = compile_model_set(process.client, model_set, context=board_sets)
            except JigError as exc:
                failures.append(str(exc))
                continue
            compiled.append({"set": timing.name, "files": timing.files, "seconds": timing.seconds})
        summary = ", ".join(
            f"{c['set']} ({c['files']} files) {c['seconds']:.2f} s" for c in compiled
        )
        if failures:
            keep = True
            report.add("models", FAIL, "; ".join(failures), compiled=compiled)
        else:
            report.add("models", PASS, f"compiled in Renode: {summary}", compiled=compiled)
    finally:
        process.stop()
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def _dotnet(report: Report) -> None:
    command = models.dotnet()
    if command is None:
        report.add(
            "dotnet",
            WARN,
            "no .NET SDK: only `jig models check --sdk` and `jig models ide` need one",
        )
        return
    started = time.monotonic()
    try:
        result = subprocess.run(
            [command, "--list-sdks"], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.add("dotnet", WARN, f"{command} --list-sdks failed: {exc}")
        return
    sdks = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    usable = [s for s in sdks if s.split(".")[0].isdigit() and int(s.split(".")[0]) >= 8]
    if usable:
        report.add("dotnet", PASS, f".NET SDK {', '.join(usable)} ({command})", sdks=sdks,
                   seconds=time.monotonic() - started)  # fmt: skip
    else:
        report.add("dotnet", WARN, f"no .NET SDK 8 or newer (found {', '.join(sdks) or 'none'}); "
                   "only --sdk checks need one", sdks=sdks)  # fmt: skip


def _space(report: Report) -> None:
    try:
        key = host_platform()
    except JigError:
        key = None
    largest = max((a.size for a in ASSETS if key is None or a.platform == key), default=0)
    needed = 4 * largest
    places = {"cache": project.cache_dir(), "temp": Path(tempfile.gettempdir())}
    free = {}
    for name, path in places.items():
        existing = path
        while not existing.exists() and existing.parent != existing:
            existing = existing.parent
        free[name] = shutil.disk_usage(existing).free
    low = [name for name, value in free.items() if value < needed]
    summary = ", ".join(f"{name} {value / 1e9:.1f} GB free" for name, value in free.items())
    if low:
        report.add("disk", WARN, f"{summary}; an install needs about {needed / 1e6:.0f} MB",
                   free=free, needed=needed)  # fmt: skip
    else:
        report.add("disk", PASS, summary, free=free, needed=needed)
