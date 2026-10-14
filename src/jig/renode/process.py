"""Launch, reach and stop a headless Renode with Jig's models and bridge.

Each Renode runs in its own run directory, which holds its stdout, its log,
its config, its PID, its temporary files (``TMP``, ``TEMP`` and ``TMPDIR``
point at ``<run>/tmp``, where the runtime compiler writes its assemblies),
the start-up script and the bridge's info file. Renode starts as::

    renode --disable-gui --hide-log --plain --console
           --config <run>/renode.config --pid-file <run>/renode.pid
           -e "include @jig-bootstrap.py"

from the run directory, with ``RENODE_CI_MODE=YES`` (no shared config lock,
no compiled-code cache) and stdin on a pipe Jig keeps open. ``--console``
puts the monitor on stdin and stdout: without it, ``--disable-gui`` alone
opens a telnet monitor on port 1234 of every interface (0.0.0.0), and the
output of start-up commands would be lost. With it, that output lands in
``renode.stdout.log``, and the end of input ends a Renode whose monitor is
idle (one whose start-up script failed). The monitor resolves
``@jig-bootstrap.py`` against the directory Renode started in, then moves
the process into Renode's own directory, so the script finds the run
directory in ``JIG_RUN_DIR``.

The script (IronPython 2.7) sets the log file, adds Renode's root and Jig's
platforms directory to the monitor's search path (``path add``), compiles
Jig's models in one include (``Monitor.TryCompilePlugin``) and starts the
bridge, which then serves requests on the monitor's thread until Jig quits
or disconnects. A start-up failure lands in ``bridge.error``; the compiler's
messages in it are mapped to Jig's model files.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import time
from pathlib import Path
from types import TracebackType
from typing import IO

from jig import project
from jig.errors import ConfigError, RenodeError
from jig.renode import models
from jig.renode.bridge import ERROR_FILE, INFO_FILE, BridgeClient, BridgeInfo
from jig.renode.discover import RenodeInstall, find_renode, load_project_config

log = logging.getLogger(__name__)

BOOTSTRAP = "jig-bootstrap.py"
MODEL_LIST = "jig-models.txt"
ENV_RUN_DIR = "JIG_RUN_DIR"
ENV_PLATFORMS = "JIG_PLATFORMS_DIR"

# Assemblies the bridge and typical models use, loaded before the compile:
# Renode's compiler references only assemblies already in the process.
_PRELOAD = (
    "System.Collections",
    "System.Collections.Concurrent",
    "System.Linq",
    "System.Memory",
    "System.Net.Primitives",
    "System.Net.Sockets",
    "System.Security.Cryptography",
    "System.Text.Json",
    "System.Text.RegularExpressions",
)

BOOTSTRAP_SOURCE = f"""\
# Jig's start-up script for one Renode process (IronPython 2.7, run by the
# monitor's include; written by jig.renode.process). It sets the log file,
# compiles Jig's C# models in one include and starts the bridge, which serves
# requests from here until Jig quits. Paths come from {ENV_RUN_DIR}: the
# monitor has moved the process into Renode's own directory.
import clr
for _jig_assembly in {_PRELOAD!r}:
    try:
        clr.AddReference(_jig_assembly)
    except Exception:
        pass
from System import Array, Environment, String
from System.Diagnostics import Stopwatch
from System.IO import File, Path
from Antmicro.Renode.UserInterface import CommandInteractionEater
from Antmicro.Renode.Utilities import Misc


def _jig_start():
    run = Environment.GetEnvironmentVariable('{ENV_RUN_DIR}')
    failure = Path.Combine(run, '{ERROR_FILE}')
    eater = CommandInteractionEater()
    try:
        log = Path.Combine(run, 'renode.log').Replace(chr(92), '/')
        commands = ['logFile "%s" true' % log]
        # The search path for relative names in include, using and @paths:
        # Jig's platforms first, then Renode's root (platforms/cpus/...).
        found, root = Misc.TryGetRootDirectory()
        platforms = Environment.GetEnvironmentVariable('{ENV_PLATFORMS}')
        for directory in (root if found else None, platforms):
            if directory:
                commands.append('path add "%s"' % directory.Replace(chr(92), '/'))
        for command in commands:
            if not monitor.Parse(command, eater):
                File.WriteAllText(failure, eater.GetError())
                return
        files = [line for line in File.ReadAllLines(Path.Combine(run, '{MODEL_LIST}')) if line]
        watch = Stopwatch.StartNew()
        if not monitor.TryCompilePlugin(Array[String](files), eater):
            File.WriteAllText(failure, eater.GetError())
            return
        Environment.SetEnvironmentVariable('JIG_STARTUP_COMPILE_MS', str(watch.ElapsedMilliseconds))
        info = Path.Combine(run, '{INFO_FILE}').Replace(chr(92), '/')
        if not monitor.Parse('emulation StartJigBridge "%s"' % info, eater):
            File.WriteAllText(failure, eater.GetError())
    except Exception as error:
        File.WriteAllText(failure, 'Jig start-up failed: %s' % error)


_jig_start()
"""


def check_run_directory(path: Path) -> None:
    """Refuse a directory the monitor cannot quote (its start-up commands name paths)."""
    if '"' in str(path):
        raise ConfigError(f"Renode's run directory may not contain a double quote: {path}")


def _next_free(path: Path) -> Path:
    index = 1
    while path.with_name(f"{path.name}.{index}").exists():
        index += 1
    return path.with_name(f"{path.name}.{index}")


class RenodeProcess:
    """A Renode child process with Jig's models compiled and the bridge up.

    Use as a context manager::

        with RenodeProcess(workdir=run_dir, install=install) as renode:
            renode.client.execute('mach create "m"')
    """

    def __init__(
        self,
        *,
        workdir: Path,
        install: RenodeInstall | None = None,
        startup_timeout: float = 120.0,
        call_timeout: float | None = None,
    ) -> None:
        self.workdir = Path(workdir).resolve()
        self._install = install
        self.startup_timeout = startup_timeout
        self.call_timeout = call_timeout
        self.startup_seconds: float | None = None
        self.startup_compile_seconds: float | None = None
        self.jig_models = models.jig_model_set()
        self._process: subprocess.Popen[bytes] | None = None
        self._stdout: IO[bytes] | None = None
        self._client: BridgeClient | None = None
        self._info: BridgeInfo | None = None

    @property
    def install(self) -> RenodeInstall:
        """The Renode this process runs (discovered when none was given)."""
        if self._install is None:
            self._install = find_renode(explicit=None, config=load_project_config())
        return self._install

    @property
    def client(self) -> BridgeClient:
        if self._client is None:
            raise RenodeError("Renode is not running")
        return self._client

    @property
    def info(self) -> BridgeInfo:
        if self._info is None:
            raise RenodeError("Renode is not running")
        return self._info

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def log_path(self) -> Path:
        """Renode's own log (peripheral warnings, unmapped accesses, ...)."""
        return self.workdir / "renode.log"

    @property
    def stdout_path(self) -> Path:
        return self.workdir / "renode.stdout.log"

    def start(self) -> BridgeClient:
        if self._process is not None:
            raise RenodeError("Renode already started")
        check_run_directory(self.workdir)
        install = self.install
        self.workdir.mkdir(parents=True, exist_ok=True)
        tmp = self.workdir / "tmp"
        tmp.mkdir(exist_ok=True)
        for stale in (INFO_FILE, ERROR_FILE, f"{INFO_FILE}.tmp"):
            (self.workdir / stale).unlink(missing_ok=True)
        if self.log_path.exists():  # Renode rotates only native paths; keep the old log
            self.log_path.replace(_next_free(self.log_path))
        files = self.jig_models.paths()
        (self.workdir / MODEL_LIST).write_text(
            "".join(f"{path}\n" for path in files), encoding="utf-8", newline="\n"
        )
        (self.workdir / BOOTSTRAP).write_text(BOOTSTRAP_SOURCE, encoding="utf-8", newline="\n")
        command = [
            str(install.executable),
            "--disable-gui",
            "--hide-log",
            "--plain",
            "--console",
            "--config",
            str(self.workdir / "renode.config"),
            "--pid-file",
            str(self.workdir / "renode.pid"),
            "-e",
            f"include @{BOOTSTRAP}",
        ]
        environment = dict(os.environ)
        environment.update(
            {
                "TMP": str(tmp),
                "TEMP": str(tmp),
                "TMPDIR": str(tmp),
                "RENODE_CI_MODE": "YES",
                ENV_RUN_DIR: str(self.workdir),
                ENV_PLATFORMS: str(project.package_file("platforms")),
            }
        )
        log.info("starting %s (%s)", install.describe(), self.workdir)
        started = time.monotonic()
        self._stdout = self.stdout_path.open("wb")
        windows = os.name == "nt"
        self._process = subprocess.Popen(
            command,
            cwd=self.workdir,
            stdin=subprocess.PIPE,
            stdout=self._stdout,
            stderr=subprocess.STDOUT,
            env=environment,
            # No console window on Windows; elsewhere the terminal's Ctrl-C goes to Jig only.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if windows else 0,
            start_new_session=not windows,
        )
        try:
            self._info = self._wait_for_bridge(started)
            client = BridgeClient(self._info, timeout=self.call_timeout)
            client.connect()
            self._client = client
        except BaseException:
            self.stop()
            raise
        self.startup_seconds = time.monotonic() - started
        self.startup_compile_seconds = self._info.startup_compile_ms / 1000
        log.info(
            "Renode ready in %.2f s (Jig's models compiled in %.2f s); bridge at %s",
            self.startup_seconds,
            self.startup_compile_seconds,
            client.address,
        )
        return client

    def _wait_for_bridge(self, started: float) -> BridgeInfo:
        assert self._process is not None
        deadline = started + self.startup_timeout
        info_path = self.workdir / INFO_FILE
        error_path = self.workdir / ERROR_FILE
        while True:
            if error_path.exists():
                time.sleep(0.05)  # let the script finish writing
                self._fail(error_path.read_text(encoding="utf-8", errors="replace"))
            info = BridgeInfo.load(info_path)
            if info is not None:
                return info
            if self._process.poll() is not None:
                raise RenodeError(
                    f"Renode exited with code {self._process.returncode} during start-up; "
                    f"see {self.stdout_path}"
                )
            if time.monotonic() > deadline:
                raise RenodeError(
                    f"Renode's bridge did not start within {self.startup_timeout:.0f} s; "
                    f"see {self.log_path} and {self.stdout_path}"
                )
            time.sleep(0.02)

    def _fail(self, text: str) -> None:
        diagnostics = models.parse_diagnostics(text, [self.jig_models], default=models.JIG_SET)
        if diagnostics:
            raise models.ModelCompileError(models.JIG_SET, diagnostics, text)
        raise RenodeError(f"Renode did not start Jig's bridge: {text.strip()}")

    def kill(self) -> None:
        """End Renode at once (it hangs, or its emulation can no longer run).
        Thread-safe; ``stop`` still cleans up afterwards."""
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def wait(self, timeout: float) -> int:
        """Wait for Renode to exit by itself; its exit code. Raises
        ``subprocess.TimeoutExpired`` when it is still running after ``timeout``."""
        if self._process is None:
            raise RenodeError("Renode is not running")
        return self._process.wait(timeout=timeout)

    def stop(self, timeout: float = 20.0) -> None:
        """Ask Renode to quit (the bridge's ``quit``, then the end of its input),
        wait up to ``timeout`` seconds, and kill it if it is still running."""
        process, self._process = self._process, None
        client, self._client = self._client, None
        if client is not None:
            if process is not None and process.poll() is None:
                with contextlib.suppress(RenodeError):
                    client.quit()
            client.close()
        if process is not None:
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("Renode did not exit within %.0f s; killing it", timeout)
                process.kill()
                process.wait(timeout=10)
        if self._stdout is not None:
            self._stdout.close()
            self._stdout = None

    def __enter__(self) -> RenodeProcess:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
