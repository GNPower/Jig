"""Zynq-7000 driver: artifacts, platform overlay and boot flows.

Settings (board.yaml ``zynq7000:``), every one optional:

``xsa``
    The hardware design (relative to the board's root). With it, a JTAG boot
    replays the design's ``ps7_init`` and ``ps7_post_config`` register writes
    and configures the PL with its bitstream, and ``jig check`` compares the
    board's clock properties with the clocks ``ps7_init`` sets up. Without it,
    the SLCR keeps its power-on state, the firmware configures the clocks it
    uses, and the board's platform description states the clock frequencies.
``ps_version``, ``ps_clk_hz``
    The silicon version that selects ``ps7_init``'s tables, and PS_CLK; both
    needed with an ``xsa``.
``straps``
    The BOOT_MODE straps (``jtag``, ``qspi``, ``nor``, ``nand``, ``sd``); without
    it the SLCR keeps what the platform description sets.

Boot modes:

``jtag``
    What a debugger does over JTAG: the core is held at reset while the
    BootROM's hardware set-up runs, the XSA's register writes are replayed and
    the PL is configured (when the board has them), and the application ELF
    (``firmware.app``) is loaded at its physical addresses with the PC at its
    entry. The boot-mode straps stay as they are, so the firmware sees the same
    BOOT_MODE it sees on the bench.

``qspi``
    Power-on through the BootROM model: it searches the flash on chip select 0
    for a boot header and starts the FSBL found there, which configures the PL
    and loads the application from its slot. The flash starts from a prebuilt
    image passed to the run, or from one the board's plugin builds
    (``api.zynq_hooks(qspi_image=...)``).

After a reset the BootROM always follows the straps, as on the board. A power
cycle halts the core, resets the machine, restarts the chips on the board's
rails and runs the BootROM again with the power-on reset cause.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jig import artifacts, repl
from jig.errors import ConfigError
from jig.ext import airhdl
from jig.ext.zynq7000.clocks import ZynqClocks
from jig.ext.zynq7000.ps7init import Ps7Init, write_program
from jig.renode.session import RenodeSession
from jig.system.boards import Board, RegisterMapSource
from jig.system.socs import MachineConfig

log = logging.getLogger(__name__)

FAMILY = "zynq7000"

# BOOT_MODE[2:0] straps (UG585 table 6-4).
STRAPS = {"jtag": 0, "qspi": 1, "nor": 2, "nand": 4, "sd": 5}

QspiImageHook = Callable[[MachineConfig], Path]
ImagesHook = Callable[[MachineConfig], dict[str, Path]]
BootTableHook = Callable[[MachineConfig, dict[str, dict[int, bytes]], dict[str, int]], bytes]


def _clock_value(clocks: ZynqClocks, name: str) -> float | None:
    if name.startswith("fclk") and name[4:].isdigit() and int(name[4:]) < len(clocks.fclk):
        return clocks.fclk[int(name[4:])]
    value = getattr(clocks, name, None)
    if value is None or isinstance(value, tuple):
        if not hasattr(clocks, name):
            raise ConfigError(f"unknown clock {name!r}")
        return None
    return float(value)


class ImageBuilder(Protocol):
    """Boot images a board makes for itself.

    How a board packs its FSBL, bitstream and application into a flash image,
    and the boot metadata its FSBL reads, belong to the board. A driver
    without an image builder boots Quad-SPI only from a flash image passed to
    the run."""

    def qspi_image(self, config: MachineConfig) -> Path:
        """A flash image for a Quad-SPI boot."""

    def images(self, config: MachineConfig) -> dict[str, Path]:
        """Firmware images by role, as an over-the-air update would send them."""

    def boot_table(
        self, config: MachineConfig, slots: dict[str, dict[int, bytes]], first: dict[str, int]
    ) -> bytes:
        """Boot metadata that populates the given slots and boots the given ones first."""


@dataclass(frozen=True)
class ZynqHooks:
    """The image hooks a board's plugin registers (``api.zynq_hooks``)."""

    qspi_image: QspiImageHook | None = None
    images: ImagesHook | None = None
    boot_table: BootTableHook | None = None


class Zynq7000Driver:
    name: str = FAMILY
    boot_modes: tuple[str, ...] = ("jtag", "qspi")

    def __init__(self, image_builder: ImageBuilder | None = None) -> None:
        self.image_builder = image_builder

    # ------------------------------------------------------------ settings

    @staticmethod
    def _setting(config: MachineConfig, key: str) -> object:
        settings = config.board.soc_settings
        if key not in settings:
            raise ConfigError(f"board {config.board.name}: zynq7000.{key} is not set")
        return settings[key]

    def required_files(self, config: MachineConfig) -> dict[str, Path]:
        """Files a boot needs that board.yaml's ``firmware`` does not list, so
        ``Simulation.start`` can name them before Renode starts: the
        ``zynq7000.xsa``, when the board sets one."""
        value = config.board.soc_settings.get("xsa")
        if value is None:
            return {}
        return {"zynq7000.xsa": (config.board.root / str(value)).resolve()}

    def _xsa(self, config: MachineConfig) -> Path | None:
        value = config.board.soc_settings.get("xsa")
        if value is None:
            return None
        xsa = (config.board.root / str(value)).resolve()
        if not xsa.is_file():
            raise ConfigError(f"board {config.board.name}: XSA {xsa} not found")
        return xsa

    def _straps(self, config: MachineConfig) -> int | None:
        value = config.board.soc_settings.get("straps")
        if value is None:
            return None
        straps = str(value)
        if straps not in STRAPS:
            raise ConfigError(
                f"board {config.board.name}: unknown straps {straps!r} ({', '.join(STRAPS)})"
            )
        return STRAPS[straps]

    # ----------------------------------------------------------- artifacts

    def register_program(self, config: MachineConfig) -> Path | None:
        """ps7_init + ps7_post_config of the XSA as a register program; None
        for a board without an XSA."""
        xsa = self._xsa(config)
        if xsa is None:
            return None
        ps_version = int(str(self._setting(config, "ps_version")))

        def build(directory: Path) -> None:
            ps7 = Ps7Init.from_xsa(xsa, ps_version)
            write_program(ps7.init + ps7.post_config, directory / "ps7_init.prog")

        entry = artifacts.cached("zynq-ps7init", [xsa], build, {"ps_version": ps_version})
        return entry / "ps7_init.prog"

    def bitstream(self, config: MachineConfig) -> Path | None:
        """The board's bitstream file, or the one inside its XSA; None when it
        has neither."""
        explicit = config.board.firmware.get("bitstream")
        if explicit is not None and explicit.is_file():
            return explicit
        xsa = self._xsa(config)
        if xsa is None:
            return None

        def build(directory: Path) -> None:
            with zipfile.ZipFile(xsa) as archive:
                names = [n for n in archive.namelist() if n.endswith(".bit")]
                if len(names) != 1:
                    raise ConfigError(f"{xsa} holds {len(names)} bitstreams; expected one")
                (directory / "design.bit").write_bytes(archive.read(names[0]))

        return artifacts.cached("zynq-bitstream", [xsa], build) / "design.bit"

    def register_maps(self, config: MachineConfig) -> dict[str, Path]:
        """JSON register maps for the PL models, generated from the RTL."""
        result: dict[str, Path] = {}
        for source in config.register_map_sources():
            inputs = [source.package] + ([source.module] if source.module else [])

            def build(directory: Path, source: RegisterMapSource = source) -> None:
                airhdl.write_json(
                    airhdl.parse(source.package, source.module), directory / "map.json"
                )

            result[source.target] = artifacts.cached("airhdl-map", inputs, build) / "map.json"
        return result

    def _hook(self, config: MachineConfig, name: str) -> Any:
        if self.image_builder is not None:
            return getattr(self.image_builder, name)
        hooks = config.hooks.get(FAMILY)
        hook = getattr(hooks, name, None) if hooks is not None else None
        if hook is None:
            raise ConfigError(
                f"board {config.board.name}: no image builder is registered for it "
                f"(its plugin's api.zynq_hooks gives no {name}); pass a flash image to the run"
            )
        return hook

    def qspi_image(self, config: MachineConfig) -> Path:
        """The flash image for a Quad-SPI boot, from the board's image builder."""
        image: Path = self._hook(config, "qspi_image")(config)
        return image

    def images(self, config: MachineConfig) -> dict[str, Path]:
        """The firmware images by role, from the board's image builder."""
        images: dict[str, Path] = self._hook(config, "images")(config)
        return images

    def boot_table(
        self, config: MachineConfig, slots: dict[str, dict[int, bytes]], first: dict[str, int]
    ) -> bytes:
        """Boot metadata for the board's FSBL, from the board's image builder."""
        table: bytes = self._hook(config, "boot_table")(config, slots, first)
        return table

    # ------------------------------------------------------------- checks

    def check(self, board: Board) -> list[str]:
        """Clock properties against the XSA, firmware files, register maps."""
        config = MachineConfig(
            name="check",
            board=board,
            boot=board.default_boot,
            performance_mips=board.performance_mips,
            workdir=board.directory,
        )
        problems: list[str] = []
        try:
            xsa = self._xsa(config)
            self._straps(config)
        except ConfigError as exc:
            return [str(exc)]
        if xsa is not None:
            try:
                ps_version = int(str(self._setting(config, "ps_version")))
                ps_clk = float(str(self._setting(config, "ps_clk_hz")))
            except ConfigError as exc:
                return [str(exc)]
            problems += self._check_clocks(board, xsa, ps_version, ps_clk)
        elif board.clocks:
            problems.append(
                "clocks: zynq7000.xsa is not set, so there is no ps7_init to check them against"
            )
        for role, path in board.firmware.items():
            if not path.is_file():
                problems.append(f"firmware {role}: {path} not found (build the firmware)")
        sources = list(board.register_maps)
        for option in board.options.values():
            for choice in option.choices.values():
                sources.extend(choice.register_maps)
        for source in sources:
            try:
                airhdl.parse(source.package, source.module)
            except (ConfigError, OSError) as exc:
                problems.append(f"register map {source.target}: {exc}")
        return problems

    @staticmethod
    def _check_clocks(board: Board, xsa: Path, ps_version: int, ps_clk: float) -> list[str]:
        problems: list[str] = []
        ps7 = Ps7Init.from_xsa(xsa, ps_version)
        clocks = ZynqClocks.from_operations(ps7.init + ps7.post_config, ps_clk)
        values = repl.properties(board.platform)
        for target, clock in board.clocks.items():
            peripheral, _, prop = target.partition(".")
            expected = _clock_value(clocks, clock)
            if expected is None:
                problems.append(
                    f"{target}: clock {clock!r} is not configured by the XSA's ps7_init"
                )
                continue
            actual = values.get(peripheral, {}).get(prop)
            if not isinstance(actual, (int, float)):
                problems.append(
                    f"{target}: not set in {board.platform.name} (expected {expected:.0f})"
                )
            elif abs(actual - expected) > 1.0:
                problems.append(
                    f"{target}: {actual} in {board.platform.name}, {expected:.0f} from the XSA"
                )
        return problems

    # ----------------------------------------------------------- the driver

    def overlay(self, config: MachineConfig) -> str:
        lines = [
            "cpu0:",
            f"    PerformanceInMips: {config.performance_mips}",
            "",
        ]
        by_peripheral: dict[str, list[str]] = {}
        for target, path in self.register_maps(config).items():
            peripheral, _, parameter = target.partition(".")
            if not parameter:
                raise ConfigError(
                    f"board {config.board.name}: register map target {target!r} "
                    "is not <peripheral>.<parameter>"
                )
            by_peripheral.setdefault(peripheral, []).append(f'    {parameter}: "{path.as_posix()}"')
        for peripheral, entries in by_peripheral.items():
            lines += [f"{peripheral}:", *entries, ""]
        return "\n".join(lines)

    def configure(self, config: MachineConfig, session: RenodeSession) -> None:
        session.select_machine(config.name)
        straps = self._straps(config)
        if straps is not None:
            session.execute(f"slcr BootMode {straps}")
        images = dict(config.storage_images)
        if config.boot == "qspi":
            target = str((config.board.boot.get("qspi") or {}).get("storage", "flash0"))
            if target not in images:
                images[target] = self.qspi_image(config)
        for storage in config.board.storage:
            image = config.workdir / f"{storage.name}.img"
            source = images.get(storage.name)
            if source is not None:
                if not source.is_file():
                    raise ConfigError(f"{config.name}: image {source} for {storage.name} not found")
                if source.stat().st_size > storage.size:
                    raise ConfigError(
                        f"{config.name}: image {source} is larger than "
                        f"{storage.name} ({storage.size} bytes)"
                    )
                shutil.copyfile(source, image)
            session.execute(f"{storage.peripheral} AttachImage @{image.as_posix()}")

    def boot(self, config: MachineConfig, session: RenodeSession) -> None:
        session.select_machine(config.name)
        if config.boot == "jtag":
            self._boot_jtag(config, session)
        elif config.boot == "qspi":
            session.execute("bootrom PowerOn")
            state = session.execute("bootrom State").strip().splitlines()[0]
            if state != "Booted":
                boot_log = session.execute("bootrom BootLog").strip()
                raise ConfigError(f"{config.name}: the BootROM did not boot ({state}):\n{boot_log}")
        else:
            raise ConfigError(
                f"{config.name}: boot mode {config.boot!r} is not one of "
                f"{', '.join(self.boot_modes)}"
            )

    def reset(self, config: MachineConfig, session: RenodeSession) -> None:
        """PS_SRST_B: an external system reset (REBOOT_STATUS SRST)."""
        session.select_machine(config.name)
        session.execute("slcr OnGPIO 1 true")
        session.execute("slcr OnGPIO 1 false")

    def power_off(self, config: MachineConfig, session: RenodeSession) -> None:
        """The core stops; PS_POR_B resets the machine with the power-on cause, and
        the BootROM keeps the core halted until power returns. Chips on the rails
        go to their power-on state at once, so their timers stop counting."""
        session.select_machine(config.name)
        session.execute("bootrom PowerOff")
        for peripheral in config.board.power_restart:
            session.execute(f"{peripheral} PowerCycle")
        session.execute("slcr OnGPIO 0 true")
        session.execute("slcr OnGPIO 0 false")

    def power_on(self, config: MachineConfig, session: RenodeSession) -> None:
        session.select_machine(config.name)
        for peripheral in config.board.power_restart:
            session.execute(f"{peripheral} PowerCycle")
        session.execute("bootrom PowerOn")

    def _boot_jtag(self, config: MachineConfig, session: RenodeSession) -> None:
        app = config.board.firmware_file("app")
        if not app.is_file():
            raise ConfigError(
                f"{config.name}: application ELF {app} not found (build the firmware first)"
            )
        program = self.register_program(config)
        bitstream = self.bitstream(config)
        session.execute("bootrom HoldCoreForDebugger true")
        session.execute("bootrom PowerOn")
        if program is not None:
            session.execute(f"machine RunRegisterProgram @{program.as_posix()}")
        if bitstream is not None:
            session.execute(f"devc ConfigureFromJtag @{bitstream.as_posix()}")
        session.execute(f"sysbus LoadELF @{app.as_posix()}")
        session.execute("bootrom HoldCoreForDebugger false")
        session.execute("cpu0 IsHalted false")
