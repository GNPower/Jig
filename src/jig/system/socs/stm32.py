"""STM32 family: firmware loaded into flash, booted from the vector table.

``device mcu: stm32(...)`` takes:

``part``
    The part number, which picks the platform: STM32F407 parts load Renode's
    ``platforms/cpus/stm32f4.repl`` with the F407's 128 KiB of SRAM1 and
    SRAM2 and without the SVD download its init does; STM32U575 and STM32U595
    parts load Jig's ``st/stm32u575`` and ``st/stm32u595``.
``platform``
    For a part Jig has no table entry for: a platform description, a path
    relative to the board file or a Renode platform (``platforms/cpus/...``).
``clock``
    The core clock (``168 MHz``), set where the part's platform takes it:
    the SysTick frequency on an STM32F407, the DWT cycle counter's on an
    STM32U5.
``vector_table``
    Where the core finds SP and PC at reset (0x08000000 by default).
``mips``
    The core's performance in the emulation (100 by default).

The device's firmware takes one of three forms::

    firmware: "build/app.elf"                         # relative to the root
    firmware: command(["cmake", "--build", "--preset", "app"], output: "build/app/app.elf")
    firmware: build("firmware/build.py", sources: "firmware", output: "firmware.elf")

A command runs in the board's root once per process before the first boot of
the board; the ``build`` form, whose paths are relative to the board file,
runs ``python build.py <output-dir> --cc <gcc>`` and caches the ELF by the
sources and the compiler's version line, the compiler being
ARM_NONE_EABI_GCC or arm-none-eabi-gcc on PATH. Boot loads the ELF's
segments at their physical (load) addresses, the way a debugger does, points
the core at the vector table and lets it run. Power-off halts the core;
power-on resets the machine, which restarts the core from the vector table
with flash intact, as a supply cycle does.

The family's interfaces and internal peripherals are the platform's names
(``usart2``, ``can1``, ``fdcan1``, ``i2c4``, ``spi3``, ``adc4``, ``rtc``),
and its pins ``pa[0]`` to ``pk[15]`` (``gpioPortA`` to ``gpioPortK``).

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
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jig import artifacts
from jig.errors import ConfigError, RenodeError, ToolchainError
from jig.renode.session import RenodeSession
from jig.system.boards import Board
from jig.system.generate import ANALOG_INPUTS, CHANNEL_SOURCE, Pin
from jig.system.model import Device, FirmwareBuild, FirmwareCommand, PinRef
from jig.system.socs import MachineConfig, power_cycled

DEFAULT_CPU = "cpu"
DEFAULT_VECTOR_TABLE = 0x08000000
RCC = "rcc"
"""The peripheral whose ``NextResetCause`` the driver sets before a reset."""
ARGUMENTS = ("part", "platform", "clock", "vector_table", "mips")
# One IronPython expression (self is Renode's monitor): True when the current
# machine's RCC takes NextResetCause. It sends no monitor command.
_RECORDS_RESET_CAUSES = (
    f"'sysbus.{RCC}' in self.Machine.GetAllNames() "
    f"and hasattr(self.Machine['sysbus.{RCC}'], 'NextResetCause')"
)
_PIN_PORT = re.compile(r"p([a-k])")
_ENTRY = re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<type>[\w.]+)\s*@", re.MULTILINE)
_USING = re.compile(r'^using\s+"(?P<path>[^"]+)"', re.MULTILINE)

_F407_LINES = (
    "// The F407's 128 KiB of SRAM1 and SRAM2 at 0x20000000 (ST RM0090, memory map);",
    "// the stock platform maps 256 KiB",
    "sram:",
    "    size: 0x20000",
    "",
    "// The stock platform's sysbus init applies an SVD file it downloads when the",
    "// platform loads. This init keeps the stock file's own tags, so a run needs",
    "// no network.",
    "sysbus:",
    "    init:",
    '        Tag <0x40021000, 0x40021003> "GPIOE_MODER" 0xFFFFFFFF',
    '        Tag <0x40021004, 0x40021007> "GPIOE_OTYPER" 0x00000008',
    '        Tag <0x50000010, 0x5000003f> "USB:RESET" 0x80000000',
    "",
)


@dataclass(frozen=True)
class Part:
    """A part the family knows: its platform (a Jig or Renode platform
    name), what the part changes in it, its core's name, where the core
    clock goes, and its converters that implement IAnalogInputs."""

    platform: str
    lines: tuple[str, ...] = ()
    core: str = DEFAULT_CPU
    clock: tuple[str, str] | None = None
    analog_inputs: frozenset[str] = field(default_factory=frozenset)


PARTS = {
    "STM32F407": Part(
        "platforms/cpus/stm32f4.repl", _F407_LINES, clock=("nvic", "systickFrequency")
    ),
    "STM32U575": Part(
        "st/stm32u575.repl", clock=("dwt", "frequency"), analog_inputs=frozenset({"adc4"})
    ),
    "STM32U595": Part(
        "st/stm32u595.repl", clock=("dwt", "frequency"), analog_inputs=frozenset({"adc4"})
    ),
}

_INTERFACE_KINDS = (
    ("lpuart", "uart"),
    ("usart", "uart"),
    ("uart", "uart"),
    ("fdcan", "can"),
    ("can", "can"),
    ("octospi", "qspi"),
    ("quadspi", "qspi"),
    ("spi", "spi"),
    ("i2c", "i2c"),
)


def part_entry(part: str) -> Part | None:
    """The table entry whose part number ``part`` starts with."""
    upper = part.upper()
    for key in sorted(PARTS, key=len, reverse=True):
        if upper.startswith(key):
            return PARTS[key]
    return None


class Stm32Driver:
    name = "stm32"
    boot_modes: tuple[str, ...] = ("flash",)

    def __init__(self) -> None:
        self._built: dict[tuple[Path, tuple[str, ...]], Path] = {}
        self._reset_causes: dict[str, bool] = {}  # machine -> its RCC records causes
        self._booted: set[str] = set()

    # ------------------------------------------------------------ arguments

    def arguments(self, values: Mapping[str, Any], where: str) -> dict[str, Any]:
        """The device's arguments, checked."""
        unknown = sorted(set(values) - set(ARGUMENTS))
        if unknown:
            raise ConfigError(
                f"{where}: stm32() takes {', '.join(ARGUMENTS)}, not {', '.join(unknown)}"
            )
        checked = dict(values)
        part = checked.get("part")
        if not isinstance(part, str) or not part.upper().startswith("STM32"):
            raise ConfigError(f'{where}: stm32() needs part, such as "STM32F407VG"')
        platform = checked.get("platform")
        if platform is not None and (not isinstance(platform, str) or not platform):
            raise ConfigError(f"{where}: platform is a platform description's path")
        if platform is None and part_entry(part) is None:
            known = ", ".join(PARTS)
            raise ConfigError(
                f"{where}: Jig has no table entry for {part} (has: {known}); "
                "give its platform description with platform:"
            )
        clock = checked.get("clock")
        if clock is not None and (
            not isinstance(clock, (int, float)) or isinstance(clock, bool) or clock <= 0
        ):
            raise ConfigError(f"{where}: clock is the core clock, such as 168 MHz")
        table = checked.get("vector_table", DEFAULT_VECTOR_TABLE)
        if not isinstance(table, int) or isinstance(table, bool) or table < 0:
            raise ConfigError(f"{where}: vector_table is an address")
        mips = checked.get("mips")
        if mips is not None and (not isinstance(mips, int) or isinstance(mips, bool) or mips <= 0):
            raise ConfigError(f"{where}: mips is a positive whole number")
        return checked

    def _part(self, board: Board) -> Part:
        arguments = board.soc_settings
        platform = arguments.get("platform")
        entry = part_entry(str(arguments.get("part", "")))
        if platform is None:
            if entry is None:
                raise ConfigError(f"board {board.name}: no platform for {arguments.get('part')}")
            return entry
        target = str(platform)
        local = (board.directory / target).resolve()
        if local.is_file():
            return Part(local.as_posix(), core=_core_of(local))
        return Part(target, core=DEFAULT_CPU)

    # ------------------------------------------------------------ settings

    def _settings(self, board: Board) -> dict[str, Any]:
        firmware = board.device.firmware if board.devices else None
        if isinstance(firmware, str) and firmware:
            return {"form": "elf", "elf": firmware}
        if isinstance(firmware, FirmwareCommand):
            return {
                "form": "command",
                "command": list(firmware.arguments),
                "output": firmware.output,
            }
        if isinstance(firmware, FirmwareBuild):
            return {
                "form": "build",
                "build": firmware.script,
                "sources": firmware.sources,
                "output": firmware.output,
            }
        raise ConfigError(
            f"board {board.name}: an STM32's firmware is an ELF path, build(...) or command(...)"
        )

    def _cpu(self, board: Board) -> str:
        return self._part(board).core

    def _vector_table(self, board: Board) -> int:
        value = board.soc_settings.get("vector_table", DEFAULT_VECTOR_TABLE)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"board {board.name}: stm32 vector_table must be an address")
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

    # ----------------------------------------------------------- the platform

    def platform_lines(self, board: Board, device: Device) -> list[str]:
        """Steps 1 and 2 of the description: the part's platform, what the
        part changes in it, and the core clock."""
        part = self._part(board)
        lines = [f'using "{part.platform}"', "", *part.lines]
        clock = board.soc_settings.get("clock")
        if clock is not None:
            if part.clock is None:
                lines += [
                    f"// clock: {round(clock)} Hz; Jig has no table entry for this part, so a",
                    "// repl fragment sets the platform's clock properties",
                    "",
                ]
            else:
                peripheral, prop = part.clock
                lines += [
                    "// The core clock",
                    f"{peripheral}:",
                    f"    {prop}: {round(clock)}",
                    "",
                ]
        return lines

    def pin(self, device: Device, ref: Any) -> Pin:
        """``pa[9]`` is gpioPortA's pin 9."""
        if isinstance(ref, PinRef) and ref.index is not None:
            found = _PIN_PORT.fullmatch(ref.port)
            if found is not None:
                if not 0 <= ref.index < 16:
                    raise ConfigError(f"{ref!r}: a GPIO port has pins 0-15")
                port = f"gpioPort{found.group(1).upper()}"
                return Pin(
                    f"{port}@{ref.index}",
                    (port, ref.index),
                    (port, f"OnGPIO {ref.index} true"),
                    None,
                )
        raise ConfigError(f"{ref!r} is not an STM32 pin (pa[0] to pk[15])")

    def analog(self, device: Device, internal: str) -> str:
        """How one of the device's converters takes a voltage: through
        IAnalogInputs (Jig's models), or an ADCChannelSource (Renode's)."""
        if device.arguments.get("platform") is None:
            entry = part_entry(str(device.arguments.get("part", "")))
            if entry is not None and internal in entry.analog_inputs:
                return ANALOG_INPUTS
        return CHANNEL_SOURCE

    def interface_kind(self, board: Board, device: Device, name: str) -> str | None:
        """What an interface is, by the platform's naming (``usart2`` is a
        UART); None for a name these prefixes don't cover."""
        for prefix, kind in _INTERFACE_KINDS:
            if name.startswith(prefix):
                return kind
        return None

    # ------------------------------------------------------------ firmware

    def required_files(self, config: MachineConfig) -> dict[str, Path]:
        """Nothing beyond the firmware: an ELF path is the board's ``app``
        firmware, and the ``command`` and ``build`` forms make their ELF at the
        first boot, so they have nothing on disk to check yet."""
        return {}

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
        script = Path(spec["build"])
        sources = Path(spec["sources"])
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
        for peripheral in power_cycled(config, session):
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
            elf = (board.root / str(spec["elf"])).resolve()
            if not elf.is_file():
                problems.append(f"firmware app: {elf} not found (build the firmware)")
        elif spec["form"] == "command":
            program = str(spec["command"][0])
            if (
                shutil.which(program, path=os.environ.get("PATH")) is None
                and not (board.root / program).is_file()
            ):
                problems.append(f"firmware command: {program} is not on PATH")
        else:
            for key in ("build", "sources"):
                path = Path(spec[key])
                if not path.exists():
                    problems.append(f"firmware build {key}: {path} not found")
            try:
                self.compiler()
            except ToolchainError as exc:
                problems.append(str(exc))
        return problems


def _core_of(path: Path) -> str:
    """The name of the first CPU entry in a platform description (following
    ``using`` lines to files beside it), else ``cpu``."""
    seen: set[Path] = set()
    pending = [path]
    while pending:
        current = pending.pop(0)
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        text = current.read_text(encoding="utf-8")
        for entry in _ENTRY.finditer(text):
            if entry.group("type").startswith("CPU."):
                return entry.group("name")
        pending += [(current.parent / u.group("path")).resolve() for u in _USING.finditer(text)]
    return DEFAULT_CPU


__all__ = ["PARTS", "Part", "Stm32Driver", "part_entry"]
