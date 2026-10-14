"""One Renode emulation, driven in simulated time.

``RenodeSession`` owns a Renode process (``jig.renode.process``): Renode
compiles Jig's models when it starts, then the session compiles each
``ModelSet`` it was given, in order, through the bridge, and advances the
emulation in whole nanoseconds. The harness never lets Renode run free:
every delay, timeout and poll is a step of simulated time, so a slow host
makes a run longer but never changes its outcome.

Renode pauses a machine that aborts (a CPU fetching code outside memory, for
instance) and a pending ``RunFor`` then never returns. A supervisor thread
follows Renode's log; on an abort, or when a step overruns its wall-clock
limit, it stops Renode and the step raises ``MachineAbortedError`` or
``SimulationError`` with the log lines that explain it.

``start`` writes ``session.json`` to the work directory: the Renode that ran
(path, version, build, runtime), the start-up time and each model set's
compile time.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from jig.errors import ConfigError, MachineAbortedError, RenodeError, SimulationError
from jig.renode import models
from jig.renode.bridge import BridgeClient, BridgeError
from jig.renode.discover import RenodeInstall, find_renode, load_project_config
from jig.renode.models import ModelSet
from jig.renode.process import RenodeProcess
from jig.runtime.scheduler import NANOSECONDS_PER_SECOND, seconds_to_ns

log = logging.getLogger(__name__)

_ABORTED = re.compile(r"(?P<machine>[\w.-]+): This machine has aborted")
_ERROR = re.compile(r"\[ERROR\]")


@dataclass(frozen=True)
class ModelTiming:
    """How long one model set took to compile."""

    name: str
    files: int
    seconds: float


def compile_model_set(
    client: BridgeClient,
    model_set: ModelSet,
    paths: Sequence[Path] | None = None,
    *,
    context: Sequence[ModelSet] = (),
) -> ModelTiming:
    """Compile ``model_set`` in the Renode behind ``client``; a compile error
    raises ``ModelCompileError`` with each message mapped to its set and file
    (``context``: the other sets a message may point into)."""
    files = paths if paths is not None else model_set.paths()
    try:
        seconds = client.compile(files)
    except BridgeError as exc:
        if exc.kind != "compile":
            raise
        sets = [model_set, models.jig_model_set(), *context]
        diagnostics = models.parse_diagnostics(exc.message, sets, default=model_set.name)
        raise models.ModelCompileError(model_set.name, diagnostics, exc.message) from None
    log.info("compiled model set %s (%d files) in %.2f s", model_set.name, len(files), seconds)
    return ModelTiming(model_set.name, len(files), seconds)


class _Supervisor:
    """Follows renode.log for machine aborts and enforces the per-step wall limit."""

    def __init__(self, log_path: Path, kill: Callable[[], None]) -> None:
        self._log_path = log_path
        self._kill = kill
        self._stop = threading.Event()
        self._recent: deque[str] = deque(maxlen=60)
        self._deadline: float | None = None
        self._lock = threading.Lock()
        self.aborted_machine: str | None = None
        self.abort_detail = ""
        self.wall_limit_hit = False
        self._thread = threading.Thread(target=self._run, name="renode-supervisor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def arm(self, wall_seconds: float | None) -> None:
        with self._lock:
            self._deadline = None if wall_seconds is None else time.monotonic() + wall_seconds

    def disarm(self) -> None:
        with self._lock:
            self._deadline = None

    def recent_log(self) -> list[str]:
        return list(self._recent)

    def _run(self) -> None:
        position = 0
        partial = ""
        while not self._stop.wait(0.05):
            position, partial = self._follow_log(position, partial)
            with self._lock:
                overdue = self._deadline is not None and time.monotonic() > self._deadline
                if overdue:
                    self._deadline = None
            if overdue and not self.wall_limit_hit:
                self.wall_limit_hit = True
                log.error("a simulation step exceeded its wall-clock limit; stopping Renode")
                self._kill()

    def _follow_log(self, position: int, partial: str) -> tuple[int, str]:
        try:
            with self._log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                chunk = handle.read()
                position = handle.tell()
        except FileNotFoundError:
            return position, partial
        except OSError as exc:
            # A shared mount (a container on a Windows checkout) can fail a
            # read while Renode writes; the next poll reads on.
            log.debug("reading %s: %s", self._log_path, exc)
            return position, partial
        if not chunk:
            return position, partial
        lines = (partial + chunk).split("\n")
        partial = lines.pop()
        for line in lines:
            self._recent.append(line.rstrip())
            match = _ABORTED.search(line)
            if match and self.aborted_machine is None:
                self.aborted_machine = match.group("machine")
                errors = [entry for entry in self._recent if _ERROR.search(entry)]
                self.abort_detail = "\n".join(errors[-6:]) or line.strip()
                log.error("machine %s aborted; stopping Renode", self.aborted_machine)
                self._kill()
        return position, partial


class RenodeSession:
    """A Renode process with the Jig models loaded, stepped in simulated time.

    ``install`` is the Renode to run (``jig.renode.discover.find_renode``
    picks one when it is ``None``); ``model_sets`` compile after Jig's own
    models, in order, each against the ones before it. Use as a context
    manager::

        with RenodeSession(workdir=run_dir, install=install) as session:
            session.create_machine("node1")
            session.run_for(0.5)
    """

    def __init__(
        self,
        *,
        workdir: Path,
        install: RenodeInstall | None = None,
        model_sets: Sequence[ModelSet] = (),
        seed: int = 0,
        step_wall_limit: float | None = None,
    ) -> None:
        self.workdir = workdir
        self._install = install
        self.model_sets = tuple(model_sets)
        self.seed = seed
        self.step_wall_limit = step_wall_limit
        self.model_timings: list[ModelTiming] = []
        self.startup_seconds: float | None = None
        self._process: RenodeProcess | None = None
        self._supervisor: _Supervisor | None = None
        self._elapsed_ns = 0
        self._current_machine: str | None = None

    # ------------------------------------------------------------ lifecycle

    @property
    def install(self) -> RenodeInstall:
        if self._install is None:
            self._install = find_renode(explicit=None, config=load_project_config())
        return self._install

    def start(self) -> None:
        if self._process is not None:
            raise RenodeError("the session has already started")
        names = [m.name for m in self.model_sets]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ConfigError(f"model set names must differ: {', '.join(duplicates)}")
        sets = [(model_set, model_set.paths()) for model_set in self.model_sets]
        started = time.monotonic()
        process = RenodeProcess(workdir=self.workdir, install=self.install)
        self._process = process
        process.start()
        self._supervisor = _Supervisor(process.log_path, process.kill)
        self._supervisor.start()
        try:
            self.model_timings = [
                ModelTiming(
                    models.JIG_SET,
                    len(process.jig_models.files),
                    process.startup_compile_seconds or 0.0,
                )
            ]
            compiled: set[tuple[Path, ...]] = set()
            for model_set, paths in sets:
                if paths in compiled:  # boards sharing one repository's models
                    continue
                compiled.add(paths)
                self.model_timings.append(self._compile(model_set, paths))
            self.execute("emulation SetGlobalAdvanceImmediately true")
            self.execute(f"emulation SetSeed {self.seed}")
        except BaseException:
            self.stop()
            raise
        self.startup_seconds = time.monotonic() - started
        self._write_record()

    def _compile(self, model_set: ModelSet, paths: Sequence[Path]) -> ModelTiming:
        self._raise_if_failed(None)
        return compile_model_set(self.client, model_set, paths, context=self.model_sets)

    def _write_record(self) -> None:
        process = self._process
        if process is None:
            return
        record: dict[str, Any] = {
            "renode": self.install.as_dict(),
            "bridge": {
                "protocol": process.info.protocol,
                "port": process.info.port,
                "pid": process.info.pid,
                "version": process.client.hello.get("version"),
                "runtime": process.client.hello.get("runtime"),
                "infrastructure": process.client.hello.get("infrastructure"),
            },
            "process_startup_seconds": process.startup_seconds,
            "startup_seconds": self.startup_seconds,
            "model_sets": [
                {"name": t.name, "files": t.files, "seconds": t.seconds} for t in self.model_timings
            ],
        }
        (self.workdir / "session.json").write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    def stop(self) -> None:
        if self._supervisor is not None:
            self._supervisor.stop()
        if self._process is not None:
            self._process.stop()

    def __enter__(self) -> RenodeSession:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # -------------------------------------------------------------- commands

    @property
    def process(self) -> RenodeProcess:
        if self._process is None:
            raise RenodeError("the session is not started")
        return self._process

    @property
    def client(self) -> BridgeClient:
        return self.process.client

    @property
    def log_path(self) -> Path:
        return self.workdir / "renode.log"

    def execute(self, command: str) -> str:
        """Run a monitor command and return its reply."""
        self._raise_if_failed(None)
        try:
            return self.client.execute(command)
        except RenodeError as exc:
            self._raise_if_failed(exc)
            raise

    def python(self, code: str) -> str:
        """Run IronPython inside Renode (its monitor scope); the value as text."""
        self._raise_if_failed(None)
        try:
            result = self.client.python(code)
        except RenodeError as exc:
            self._raise_if_failed(exc)
            raise
        return "" if result is None else str(result)

    def create_machine(self, name: str, *, own_time: bool = False) -> None:
        """Create a machine and make it the current one.

        ``mach create`` puts every machine on the emulation's master time
        source, which moves only as far as the slowest core of all machines
        has run. A machine's timers and scheduled actions then fire late and
        in bursts at quantum edges. With ``own_time`` the machine gets its own
        time source under the master, so its clock follows its own cores."""
        if own_time:
            self.python(
                "from Antmicro.Renode.Core import EmulationManager, Machine\n"
                f"EmulationManager.Instance.CurrentEmulation.AddMachine(Machine(True), '{name}')"
            )
            self.execute(f'mach set "{name}"')
        else:
            self.execute(f'mach create "{name}"')
        self._current_machine = name

    def select_machine(self, name: str) -> None:
        """Make ``name`` the monitor's current machine; machine-local names resolve in it."""
        if self._current_machine != name:
            self.execute(f'mach set "{name}"')
            self._current_machine = name

    # ----------------------------------------------------------------- time

    @property
    def elapsed_ns(self) -> int:
        """Simulated time since the emulation started, in nanoseconds."""
        return self._elapsed_ns

    @property
    def elapsed(self) -> float:
        return self._elapsed_ns / NANOSECONDS_PER_SECOND

    def run_for_ns(self, nanoseconds: int) -> None:
        """Advance every machine by ``nanoseconds`` of simulated time."""
        self._step(nanoseconds, "RunForNanoseconds")

    def step_and_collect(self, nanoseconds: int) -> str:
        """Advance by ``nanoseconds`` and return what the harness ports saw:
        one line per port with data (``U name stamps hex``, ``C name frames``)."""
        return self._step(nanoseconds, "RunForNanosecondsAndCollect")

    def _step(self, nanoseconds: int, command: str) -> str:
        if nanoseconds <= 0:
            return ""
        supervisor = self._supervisor
        if supervisor is None:
            raise RenodeError("the session is not started")
        self._raise_if_failed(None)
        supervisor.arm(self.step_wall_limit)
        try:
            reply = self.client.execute(f"emulation {command} {nanoseconds}")
        except RenodeError as exc:
            self._raise_if_failed(exc)
            raise
        finally:
            supervisor.disarm()
        self._raise_if_failed(None)
        self._elapsed_ns += nanoseconds
        return reply

    def run_for(self, seconds: float) -> None:
        self.run_for_ns(seconds_to_ns(seconds))

    def recent_log(self) -> list[str]:
        """The last lines of Renode's log (peripheral warnings, errors)."""
        return [] if self._supervisor is None else self._supervisor.recent_log()

    def _raise_if_failed(self, cause: BaseException | None) -> None:
        supervisor = self._supervisor
        if supervisor is None:
            return
        if supervisor.aborted_machine is not None:
            raise MachineAbortedError(
                supervisor.aborted_machine, supervisor.abort_detail, self.elapsed
            ) from cause
        if supervisor.wall_limit_hit:
            raise SimulationError(
                f"a simulation step ran longer than {self.step_wall_limit:.0f} s of wall time "
                f"at t={self.elapsed:.6f} s; see {self.log_path}"
            ) from cause
