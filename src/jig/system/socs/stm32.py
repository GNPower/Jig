"""STM32 driver: firmware loaded into flash, booted from the vector table.

An STM32 board names its firmware in board.yaml, in one of three forms:

    stm32:
      firmware:
        elf: build/app.elf            # a prebuilt ELF, relative to the board's root

    stm32:
      firmware:
        command: [cmake, --build, --preset, app]   # run in the board's root
        output: build/app/app.elf                  # what it leaves, root-relative

    stm32:
      firmware:
        build: firmware/build.py      # python build.py <output-dir> --cc <gcc>
        sources: firmware             # the files the build reads (the cache key)
        output: firmware.elf          # both relative to this board.yaml

    stm32:
      cpu: cpu                        # the core's name in the platform (default cpu)
      vector_table: 0x08000000        # where the core finds SP and PC at reset

A command runs once per process before the first boot of the board; the
``build`` form's ELF is a cached artifact keyed by the sources and the
compiler's version line, the compiler being ARM_NONE_EABI_GCC or
arm-none-eabi-gcc on PATH. Boot loads the ELF's segments at their physical
(load) addresses, the way a debugger does, points the core at the vector
table and lets it run. Power-off halts the core; power-on resets the machine,
which restarts the core from the vector table with flash intact, as a supply
cycle does.

Reset causes. When the machine's ``rcc`` peripheral records why it was reset
(it has a ``NextResetCause`` property, as ``Jig.ST.STM32U5_RCC`` on Jig's
``st/stm32u5*`` platforms does), the driver tells it the cause of every reset
it makes: ``Pin`` for ``reset`` (the NRST input) and ``PowerOn`` for the
reset of ``power_on``, so firmware reads RCC_CSR as it would on silicon and
the backup domain (RTC, backup registers) is lost at power-on. Power-off of
such a board also puts it through a power-on reset and halts the core, so
a watchdog the firmware started cannot reset the machine and restart the
core while the supply is off. On an RCC without the property, resets go to
Renode unannounced.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from jig import artifacts
from jig.errors import ConfigError, RenodeError, ToolchainError
from jig.renode.session import RenodeSession
from jig.system.boards import Board
from jig.system.socs import MachineConfig

DEFAULT_CPU = "cpu"
DEFAULT_VECTOR_TABLE = 0x08000000
RCC = "rcc"
"""The peripheral whose ``NextResetCause`` the driver sets before a reset."""
_FORMS = ("elf", "command", "build")
_KEYS = {
    "elf": ("elf",),
    "command": ("command", "output"),
    "build": ("build", "sources", "output"),
}
# One IronPython expression (self is Renode's monitor): True when the current
# machine's RCC takes NextResetCause. It sends no monitor command.
_RECORDS_RESET_CAUSES = (
    f"'sysbus.{RCC}' in self.Machine.GetAllNames() "
    f"and hasattr(self.Machine['sysbus.{RCC}'], 'NextResetCause')"
)


class Stm32Driver:
    name = "stm32"
    boot_modes: tuple[str, ...] = ("flash",)

    def __init__(self) -> None:
        self._built: dict[tuple[Path, tuple[str, ...]], Path] = {}
        self._reset_causes: dict[str, bool] = {}  # machine -> its RCC records causes
        self._booted: set[str] = set()

    # ------------------------------------------------------------ settings

    def _settings(self, board: Board) -> dict[str, Any]:
        firmware = board.soc_settings.get("firmware")
        if not isinstance(firmware, dict):
            raise ConfigError(
                f"board {board.name}: stm32.firmware must give elf, command and output, "
                "or build, sources and output"
            )
        forms = [form for form in _FORMS if form in firmware]
        if len(forms) != 1:
            raise ConfigError(
                f"board {board.name}: stm32.firmware gives one of elf, command or build"
                + (f", not {' and '.join(forms)}" if forms else "")
            )
        form = forms[0]
        allowed = _KEYS[form]
        unknown = sorted(set(firmware) - set(allowed))
        if unknown:
            raise ConfigError(
                f"board {board.name}: stm32.firmware.{unknown[0]} does not go with {form}"
            )
        for key in allowed:
            if not firmware.get(key):
                raise ConfigError(f"board {board.name}: stm32.firmware.{key} is missing")
        if form == "command" and (
            not isinstance(firmware["command"], list)
            or not all(isinstance(item, str) and item for item in firmware["command"])
        ):
            raise ConfigError(
                f"board {board.name}: stm32.firmware.command is a list of arguments, "
                "e.g. [cmake, --build, --preset, app]"
            )
        return {"form": form, **firmware}

    def _cpu(self, board: Board) -> str:
        value = board.soc_settings.get("cpu", DEFAULT_CPU)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"board {board.name}: stm32.cpu must be a peripheral name")
        return value

    def _vector_table(self, board: Board) -> int:
        value = board.soc_settings.get("vector_table", DEFAULT_VECTOR_TABLE)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"board {board.name}: stm32.vector_table must be an address")
        return value

    @staticmethod
    def compiler() -> str:
        explicit = os.environ.get("ARM_NONE_EABI_GCC")
        if explicit:
            if not Path(explicit).is_file():
                raise ToolchainError(f"ARM_NONE_EABI_GCC={explicit} does not exist")
            return explicit
        found = shutil.which("arm-none-eabi-gcc")
        if found is None:
            raise ToolchainError(
                "arm-none-eabi-gcc is not on PATH: set ARM_NONE_EABI_GCC to an ARM GCC"
            )
        return found

    # ------------------------------------------------------------ firmware

    def required_files(self, config: MachineConfig) -> dict[str, Path]:
        """Files a boot needs that board.yaml's ``firmware`` does not list, so
        ``Simulation.start`` can name them before Renode starts: a prebuilt
        ``stm32.firmware.elf``. The ``command`` and ``build`` forms make their
        ELF at the first boot, so they have nothing on disk to check yet."""
        spec = self._settings(config.board)
        if spec["form"] != "elf":
            return {}
        return {"stm32.firmware.elf": (config.board.root / str(spec["elf"])).resolve()}

    def firmware(self, config: MachineConfig) -> Path:
        """The board's ELF: prebuilt, made by its command, or built and cached."""
        board = config.board
        spec = self._settings(board)
        if spec["form"] == "elf":
            elf = (board.root / str(spec["elf"])).resolve()
            if not elf.is_file():
                raise ConfigError(
                    f"board {board.name}: firmware {elf} not found (build the firmware first)"
                )
            return elf
        if spec["form"] == "command":
            return self._command(board, [str(item) for item in spec["command"]], spec["output"])
        return self._build(config, spec)

    def _command(self, board: Board, command: list[str], output: str) -> Path:
        key = (board.source, tuple(command))
        elf = (board.root / output).resolve()
        if key not in self._built:
            try:
                result = subprocess.run(
                    command, cwd=board.root, capture_output=True, text=True, check=False
                )
            except OSError as exc:
                raise ToolchainError(
                    f"board {board.name}: cannot run {' '.join(command)}: {exc}"
                ) from None
            if result.returncode != 0:
                tail = (result.stdout + result.stderr).strip().splitlines()[-40:]
                raise ToolchainError(
                    f"board {board.name}: {' '.join(command)} failed "
                    f"(exit {result.returncode}):\n" + "\n".join(tail)
                )
            self._built[key] = elf
        if not elf.is_file():
            raise ToolchainError(f"board {board.name}: {' '.join(command)} left no {elf}")
        return elf

    def _build(self, config: MachineConfig, spec: dict[str, Any]) -> Path:
        board = config.board
        script = (board.directory / str(spec["build"])).resolve()
        sources = (board.directory / str(spec["sources"])).resolve()
        if not script.is_file() or not sources.is_dir():
            raise ConfigError(f"board {board.name}: {script} or {sources} not found")
        compiler = self.compiler()
        try:
            version = subprocess.run(
                [compiler, "--version"], capture_output=True, text=True, check=True
            ).stdout.splitlines()[0]
        except (OSError, subprocess.CalledProcessError, IndexError) as exc:
            raise ToolchainError(f"{compiler} does not run: {exc}") from None
        inputs = sorted(
            p for p in sources.rglob("*") if p.is_file() and "__pycache__" not in p.parts
        )

        def build(directory: Path) -> None:
            result = subprocess.run(
                [sys.executable, str(script), str(directory), "--cc", compiler],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise ToolchainError(
                    f"building the {board.name} firmware failed:\n{result.stdout}{result.stderr}"
                )

        entry = artifacts.cached(f"{board.name}-firmware", inputs, build, {"compiler": version})
        elf = entry / str(spec["output"])
        if not elf.is_file():
            raise ToolchainError(f"{script} did not produce {spec['output']}")
        return elf

    # -------------------------------------------------------------- driver

    def overlay(self, config: MachineConfig) -> str:
        cpu = self._cpu(config.board)
        return f"{cpu}:\n    PerformanceInMips: {config.performance_mips}\n"

    def configure(self, config: MachineConfig, session: RenodeSession) -> None:
        session.select_machine(config.name)

    def boot(self, config: MachineConfig, session: RenodeSession) -> None:
        if config.boot != "flash":
            raise ConfigError(f"{config.name}: boot mode {config.boot!r} is not flash")
        elf = self.firmware(config)
        cpu = self._cpu(config.board)
        session.select_machine(config.name)
        # false: each segment goes to its physical (load) address.
        session.execute(f"sysbus LoadELF @{elf.as_posix()} false")
        # The core takes SP and PC from this table whenever it leaves reset.
        session.execute(f"{cpu} InitVectorTableOffset {self._vector_table(config.board):#x}")
        session.execute(f"{cpu} IsHalted false")
        self._booted.add(config.name)

    def records_reset_causes(self, config: MachineConfig, session: RenodeSession) -> bool:
        """Whether the machine's RCC takes ``NextResetCause`` (asked once per machine)."""
        known = self._reset_causes.get(config.name)
        if known is None:
            session.select_machine(config.name)
            try:
                known = session.python(_RECORDS_RESET_CAUSES) == "True"
            except RenodeError:
                known = False
            self._reset_causes[config.name] = known
        return known

    def _reset_machine(self, config: MachineConfig, session: RenodeSession, cause: str) -> None:
        session.select_machine(config.name)
        if self.records_reset_causes(config, session):
            session.execute(f"{RCC} NextResetCause {cause}")
        session.execute("machine Reset")

    def reset(self, config: MachineConfig, session: RenodeSession) -> None:
        """NRST: the core restarts from the vector table; flash keeps the firmware.
        The RCC, when it records reset causes, reports a pin reset."""
        self._reset_machine(config, session, "Pin")

    def power_off(self, config: MachineConfig, session: RenodeSession) -> None:
        cpu = self._cpu(config.board)
        session.select_machine(config.name)
        session.execute(f"{cpu} IsHalted true")
        for peripheral in config.board.power_restart:
            session.execute(f"{peripheral} PowerCycle")
        if config.name in self._booted and self.records_reset_causes(config, session):
            # Held in a power-on reset until the supply returns; the reset
            # starts the core, so halt it again.
            self._reset_machine(config, session, "PowerOn")
            session.execute(f"{cpu} IsHalted true")

    def power_on(self, config: MachineConfig, session: RenodeSession) -> None:
        """The supply returns: a power-on reset, then the core runs."""
        self._reset_machine(config, session, "PowerOn")
        session.execute(f"{self._cpu(config.board)} IsHalted false")

    def images(self, config: MachineConfig) -> dict[str, Path]:
        return {"app": self.firmware(config)}

    def boot_table(
        self, config: MachineConfig, slots: dict[str, dict[int, bytes]], first: dict[str, int]
    ) -> bytes:
        raise ConfigError(f"board {config.board.name}: an STM32 has no boot table")

    def check(self, board: Board) -> list[str]:
        problems = []
        try:
            spec = self._settings(board)
            self._cpu(board)
            self._vector_table(board)
        except ConfigError as exc:
            return [str(exc)]
        if spec["form"] == "elf":
            elf = board.root / str(spec["elf"])
            if not elf.is_file():
                problems.append(f"stm32.firmware.elf: {elf} not found (build the firmware)")
        elif spec["form"] == "command":
            program = str(spec["command"][0])
            if (
                shutil.which(program, path=os.environ.get("PATH")) is None
                and not (board.root / program).is_file()
            ):
                problems.append(f"stm32.firmware.command: {program} is not on PATH")
        else:
            for key in ("build", "sources"):
                path = board.directory / str(spec[key])
                if not path.exists():
                    problems.append(f"stm32.firmware.{key}: {path} not found")
            try:
                self.compiler()
            except ToolchainError as exc:
                problems.append(str(exc))
        return problems
