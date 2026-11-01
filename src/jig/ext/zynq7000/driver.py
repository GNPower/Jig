"""Zynq-7000 family: device arguments, the platform description, boot flows.

``device zynq: zynq7000(...)`` takes, every argument optional:

``part``
    The part number, such as ``"XC7Z020-CLG484"``. Every Zynq-7000 part
    loads Jig's ``xilinx/zynq7000`` platform (CPU1 is not modelled).
``xsa``
    The hardware design (relative to the board's root). With it, the
    platform's PS clocks come from the design's ``ps7_init`` at ``ps_clk``, a
    JTAG boot replays the design's ``ps7_init`` and ``ps7_post_config``
    register writes and configures the PL with its bitstream.
``ps_clk``, ``ps_version``
    PS_CLK (``33.333333 MHz``) and the silicon version that selects
    ``ps7_init``'s tables; both needed with an ``xsa``.
``straps``
    The BOOT_MODE straps (``"jtag"``, ``"qspi"``, ``"nor"``, ``"nand"``,
    ``"sd"``); without it the SLCR keeps its power-on value.
``ddr``
    The size of the board's DDR, mapped from 0x00100000, the first address
    every bus master sees as DDR (UG585 table 4-1).
``clocks``
    For a board without an XSA: the PS clocks the firmware sets up, by
    name (``cpu_3x2x``, ``cpu_1x``, ``uart_ref``, ``spi_ref``, ``can_ref``,
    ``lqspi_ref``, ``pcap``; ``cpu_6x4x``, ``cpu_2x`` and ``fclk0`` to
    ``fclk3`` are accepted and clock no PS peripheral). ``CLOCK_TABLE`` lists
    the peripherals each one clocks.
``mips``
    The core's performance in the emulation (100 by default).

The family's interfaces are ``uart0``, ``uart1``, ``can0``, ``can1``,
``spi0``, ``spi1``, ``qspi``, ``i2c0`` and ``i2c1``; its pins ``mio[0]`` to
``mio[53]`` (GPIO pins 0-53), ``emio[0]`` to ``emio[63]`` (GPIO pins 54-117),
``ps_por_b`` and ``ps_srst_b`` (the SLCR's reset inputs 0 and 1). With an XSA,
the instances and exported ports of its block design are names of the device
too: an AXI Quad SPI that Jig models is an SPI controller
(``spi(controller: zynq.axi_quad_spi_0, ...)``), and ``ip`` lines name ports
and instances in ``on``, ``irq`` and property values.

The programmable logic, ``ip`` lines included, comes from
``jig.ext.zynq7000.blockdesign``: ``pl_section`` for a device with an XSA, and
``render`` of an empty design for one without, where ``ip`` lines give address
ranges and ``IRQ_F2P`` lines.

Boot modes:

``jtag``
    What a debugger does over JTAG: the core is held at reset while the
    BootROM's hardware set-up runs, the XSA's register writes are replayed and
    the PL is configured (when the board has them), and the application ELF
    (the ``app`` firmware) is loaded at its physical addresses with the PC at
    its entry. The boot-mode straps stay as they are, so the firmware sees the
    same BOOT_MODE it sees on the bench.

``qspi``
    Power-on through the BootROM model: it searches the flash on chip select 0
    for a boot header and starts the FSBL found there, which configures the PL
    and loads the application from its slot. The flash starts from a prebuilt
    image passed to the run, or from one the board's plugin builds
    (``api.zynq_hooks(qspi_image=...)``). ``boot qspi: {storage: flash0}``
    names the flash (``flash0`` by default).

After a reset the BootROM always follows the straps, as on the board. A power
cycle halts the core, resets the machine, restarts the chips on the board's
supply and runs the BootROM again with the power-on reset cause.
"""

from __future__ import annotations

import functools
import logging
import re
import shutil
import zipfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jig import artifacts
from jig.errors import ConfigError
from jig.ext import airhdl
from jig.ext.zynq7000 import blockdesign
from jig.ext.zynq7000.blockdesign import (
    AddressRange,
    BlockDesign,
    Cosim,
    IpOverride,
    Literal,
    Memory,
    PropertyValue,
    Reference,
)
from jig.ext.zynq7000.clocks import ZynqClocks
from jig.ext.zynq7000.ps7init import Ps7Init, write_program
from jig.renode.session import RenodeSession
from jig.system.boards import Board, RegisterMapSource
from jig.system.generate import Pin, repl_value
from jig.system.model import (
    AirhdlSource,
    DesignName,
    Device,
    InternalRef,
    IpBlock,
    PeripheralRef,
    PinRef,
)
from jig.system.socs import MachineConfig, power_cycled

log = logging.getLogger(__name__)

FAMILY = "zynq7000"
PLATFORM = "xilinx/zynq7000.repl"

# BOOT_MODE[2:0] straps (UG585 table 6-4).
STRAPS = {"jtag": 0, "qspi": 1, "nor": 2, "nand": 4, "sd": 5}

ARGUMENTS = ("part", "xsa", "ps_clk", "ps_version", "straps", "ddr", "clocks", "mips")

# The PS peripherals each clock drives, as (peripheral, property) of Jig's
# xilinx/zynq7000 platform (UG585 chapter 25).
CLOCK_TABLE: dict[str, tuple[tuple[str, str], ...]] = {
    "cpu_3x2x": (("globalTimer", "frequency"), ("privateTimer", "frequency")),
    "cpu_1x": (("swdt", "frequency"), ("ttc0", "frequency"), ("ttc1", "frequency")),
    "uart_ref": (("uart0", "clockFrequency"), ("uart1", "clockFrequency")),
    "spi_ref": (("spi0", "referenceClockFrequency"), ("spi1", "referenceClockFrequency")),
    "can_ref": (("can0", "referenceClockFrequency"), ("can1", "referenceClockFrequency")),
    "lqspi_ref": (("qspi", "referenceClockFrequency"),),
    "pcap": (("devc", "pcapClockFrequency"),),
}
CLOCKS = (*CLOCK_TABLE, "cpu_6x4x", "cpu_2x", "fclk0", "fclk1", "fclk2", "fclk3")
REQUIRED_CLOCKS = ("cpu_3x2x", "cpu_1x")  # the timers and the watchdog take no default

INTERFACES = {
    "uart0": "uart",
    "uart1": "uart",
    "can0": "can",
    "can1": "can",
    "spi0": "spi",
    "spi1": "spi",
    "qspi": "qspi",
    "i2c0": "i2c",
    "i2c1": "i2c",
}
MIO_PINS = 54
EMIO_PINS = 64
RESET_PINS = {"ps_por_b": 0, "ps_srst_b": 1}  # the SLCR's GPIO inputs

DDR_BASE = 0x00100000

# The link kind of each PL IP Jig models that a link can name as its
# controller, by VLNV without the version (PG153: the AXI Quad SPI's standard
# mode is an SPI controller).
PL_INTERFACES = {"xilinx.com:ip:axi_quad_spi": "spi"}

_PART = re.compile(r"XC7Z\d{3}S?(-\S+)?", re.IGNORECASE)
_ROLES = ("app", "fsbl", "bitstream")

QspiImageHook = Callable[[MachineConfig], Path]
ImagesHook = Callable[[MachineConfig], dict[str, Path]]
BootTableHook = Callable[[MachineConfig, dict[str, dict[int, bytes]], dict[str, int]], bytes]


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


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@functools.lru_cache(maxsize=8)
def _read_design(path: str, size: int, modified_ns: int) -> BlockDesign:
    """An XSA's block design, read once per version of the file."""
    return blockdesign.read(Path(path))


class Zynq7000Driver:
    name: str = FAMILY
    boot_modes: tuple[str, ...] = ("jtag", "qspi")
    programmable_logic = True  # platform_lines places the device's ip lines

    def __init__(self, image_builder: ImageBuilder | None = None) -> None:
        self.image_builder = image_builder

    # ------------------------------------------------------------ arguments

    def arguments(self, values: Mapping[str, Any], where: str) -> dict[str, Any]:
        """The device's arguments, checked."""
        unknown = sorted(set(values) - set(ARGUMENTS))
        if unknown:
            raise ConfigError(
                f"{where}: zynq7000() takes {', '.join(ARGUMENTS)}, not {', '.join(unknown)}"
            )
        checked = dict(values)
        part = checked.get("part")
        if part is not None and (not isinstance(part, str) or not _PART.fullmatch(part)):
            raise ConfigError(f"{where}: zynq7000 part {part!r} is not a Zynq-7000 (XC7Z...)")
        xsa = checked.get("xsa")
        if xsa is not None:
            if not isinstance(xsa, str) or not xsa:
                raise ConfigError(f"{where}: xsa is a path relative to the board's root")
            for key in ("ps_clk", "ps_version"):
                if key not in checked:
                    raise ConfigError(f"{where}: zynq7000 with an xsa needs {key}")
            if "clocks" in checked:
                raise ConfigError(
                    f"{where}: clocks are for a board without an xsa; with one they come "
                    "from its ps7_init"
                )
        ps_clk = checked.get("ps_clk")
        if ps_clk is not None and (not _number(ps_clk) or ps_clk <= 0):
            raise ConfigError(f"{where}: ps_clk is a frequency, such as 33.333333 MHz")
        version = checked.get("ps_version")
        if version is not None and (
            not isinstance(version, int) or isinstance(version, bool) or version < 0
        ):
            raise ConfigError(f"{where}: ps_version is a silicon version number, such as 3")
        straps = checked.get("straps")
        if straps is not None and straps not in STRAPS:
            raise ConfigError(
                f"{where}: straps is one of {', '.join(map(repr, STRAPS))}, not {straps!r}"
            )
        ddr = checked.get("ddr")
        if ddr is not None and (
            not isinstance(ddr, int) or isinstance(ddr, bool) or ddr <= DDR_BASE
        ):
            raise ConfigError(f"{where}: ddr is the DDR's size, such as 512 MiB")
        clocks = checked.get("clocks")
        if clocks is not None:
            if not isinstance(clocks, dict):
                raise ConfigError(f"{where}: clocks is a map of clock frequencies")
            for name, value in clocks.items():
                if name not in CLOCKS:
                    raise ConfigError(
                        f"{where}: clocks: {name} is not a Zynq-7000 PS clock "
                        f"(known: {', '.join(CLOCKS)})"
                    )
                if not _number(value) or value <= 0:
                    raise ConfigError(f"{where}: clocks: {name} is a frequency, not {value!r}")
        mips = checked.get("mips")
        if mips is not None and (not isinstance(mips, int) or isinstance(mips, bool) or mips <= 0):
            raise ConfigError(f"{where}: mips is a positive whole number")
        return checked

    @staticmethod
    def _setting(config: MachineConfig, key: str) -> object:
        settings = config.board.soc_settings
        if key not in settings:
            raise ConfigError(f"board {config.board.name}: zynq7000 {key} is not set")
        return settings[key]

    def required_files(self, config: MachineConfig) -> dict[str, Path]:
        """Files a boot needs that the device's ``firmware`` does not list, so
        ``Simulation.start`` can name them before Renode starts: the ``xsa``,
        when the device has one."""
        value = config.board.soc_settings.get("xsa")
        if value is None:
            return {}
        return {"zynq7000 xsa": (config.board.root / str(value)).resolve()}

    @staticmethod
    def xsa_path(board: Board) -> Path | None:
        value = board.soc_settings.get("xsa")
        if value is None:
            return None
        xsa = (board.root / str(value)).resolve()
        if not xsa.is_file():
            raise ConfigError(f"board {board.name}: XSA {xsa} not found")
        return xsa

    def _xsa(self, config: MachineConfig) -> Path | None:
        return self.xsa_path(config.board)

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

    # ----------------------------------------------------------- the platform

    def clocks(self, board: Board) -> tuple[dict[str, float], str]:
        """The PS clock frequencies in Hz by name, and where they came from."""
        xsa = self.xsa_path(board)
        if xsa is None:
            given = board.soc_settings.get("clocks") or {}
            found = {str(name): float(value) for name, value in given.items()}
            return found, "the device's clocks argument"
        ps_version = int(str(board.soc_settings["ps_version"]))
        ps_clk = float(board.soc_settings["ps_clk"])
        ps7 = Ps7Init.from_xsa(xsa, ps_version)
        tree = ZynqClocks.from_operations(ps7.init + ps7.post_config, ps_clk)
        found = {}
        for name in CLOCKS:
            if name.startswith("fclk"):
                value: float | None = tree.fclk[int(name[4:])]
            else:
                value = getattr(tree, name)
            if value is not None:
                found[name] = float(value)
        return found, f"the XSA's ps7_init at PS_CLK {ps_clk / 1e6:.6g} MHz"

    def fclk_hz(self, board: Board) -> dict[int, float]:
        """The PL clocks FCLK0 to FCLK3 the design sets up, by index."""
        clocks, _source = self.clocks(board)
        return _fclk(clocks)

    def design(self, board: Board) -> BlockDesign:
        """The block design in the board's XSA: an empty design for a board
        without an XSA, or with one that holds no ``.hwh``."""
        xsa = self.xsa_path(board)
        if xsa is None:
            return BlockDesign()
        stat = xsa.stat()
        return _read_design(str(xsa), stat.st_size, stat.st_mtime_ns)

    def platform_lines(self, board: Board, device: Device) -> list[str]:
        """Steps 1 to 3 of the description: the platform, the PS clocks, the
        straps, the DDR, the programmable logic."""
        arguments = board.soc_settings
        lines = [f'using "{PLATFORM}"', ""]
        clocks, source = self.clocks(board)
        missing = [name for name in REQUIRED_CLOCKS if name not in clocks]
        if missing:
            raise ConfigError(
                f"{device.where}: the zynq7000 device needs the {' and '.join(missing)} clock "
                "(the timers and the watchdog take no default): give it in clocks, or an xsa"
            )
        lines.append(f"// PS clocks, from {source} (UG585 chapter 25)")
        for clock, targets in CLOCK_TABLE.items():
            if clock not in clocks:
                continue
            hz = round(clocks[clock])
            for peripheral, prop in targets:
                lines += [f"{peripheral}:", f"    {prop}: {hz}", ""]
        straps = arguments.get("straps")
        if straps is not None:
            lines += [
                f"// Boot-mode straps: {straps} (UG585 table 6-4)",
                "slcr:",
                f"    bootMode: {STRAPS[str(straps)]}",
                "",
            ]
        ddr = arguments.get("ddr")
        if ddr is not None:
            lines += [
                f"// DDR: {ddr // (1024 * 1024)} MiB, mapped from 0x{DDR_BASE:08X}, the first "
                "address every bus",
                "// master sees as DDR (UG585 table 4-1)",
                f"ddr: Memory.MappedMemory @ sysbus 0x{DDR_BASE:08X}",
                f"    size: 0x{ddr - DDR_BASE:X}",
                "",
            ]
        lines += self._pl_lines(board, device, _fclk(clocks))
        return lines

    def _pl_lines(self, board: Board, device: Device, fclk_hz: Mapping[int, float]) -> list[str]:
        """Step 3: the programmable logic, the device's ``ip`` lines
        included: the XSA's block design with the lines applied, or the lines
        alone for a device without an XSA."""
        xsa = self.xsa_path(board)
        if xsa is None and not device.ips:
            return []
        overrides = self.overrides(board, device)
        try:
            if xsa is None:
                section = blockdesign.render(BlockDesign(), {}, overrides)
            else:
                section = blockdesign.pl_section(xsa, fclk_hz, overrides)
        except ConfigError as exc:
            raise ConfigError(f"{_ip_where(device, str(exc))}{exc}") from None
        return [*section.splitlines(), ""]

    def overrides(self, board: Board, device: Device) -> list[IpOverride]:
        """The device's ``ip`` lines as ``blockdesign.IpOverride``s."""
        names: Collection[str] = ()
        if any(_names_instance(ip) for ip in device.ips):
            names = self._referable(board, device)
        return [self.override(ip, names) for ip in device.ips]

    @staticmethod
    def override(ip: IpBlock, names: Collection[str] = ()) -> IpOverride:
        """An ``ip`` line as the block-design writer takes it: the model (a
        class, ``Memory`` or ``Cosim``), its properties in Renode's units
        without the co-simulation's build settings, ``on`` and ``irq``.
        ``names`` are the names a property value may give (``_referable``):
        peripherals of the board and the platform, the ip lines, and the
        instances of the design that have a model."""
        where = f"{ip.where}: ip {ip.name}"
        properties = {
            key: _property(value, key, where, names)
            for key, value in ip.properties.items()
            if not isinstance(value, AirhdlSource)  # the overlay fills these in per run
        }
        model: str | Memory | Cosim = ip.model
        if ip.memory:
            size = properties.pop("size", None)
            if size is None and isinstance(ip.on, tuple):
                size = ip.on[1] - ip.on[0] + 1
            if not isinstance(size, int) or isinstance(size, bool):
                raise ConfigError(f"{where}: memory() needs a size, such as memory(size: 64 KiB)")
            model = Memory(size)
        elif ip.cosim is not None:
            model = Cosim()
        on: Any = ip.on
        if isinstance(on, tuple):
            on = AddressRange(*on)
        return IpOverride(ip.name, model, properties, on, ip.irq)

    def _referable(self, board: Board, device: Device) -> set[str]:
        """What an ip line's property can name: the board's peripherals, the
        ip lines, the platform's own peripherals (``gic``), and the instances
        of the block design that get a model."""
        design = self.design(board)
        placed = {segment.slave for segment in design.slaves()}
        names = {part.name for part in board.peripherals} | {ip.name for ip in device.ips}
        names |= blockdesign.platform_names()
        names |= {
            name
            for name, instance in design.instances.items()
            if name in placed and instance.ip in blockdesign.MODELS
        }
        return names

    def pin(self, device: Device, ref: Any) -> Pin:
        """A pin of the device: ``mio[n]`` and ``emio[n]`` are GPIO pins n and
        54 + n, ``ps_por_b`` and ``ps_srst_b`` the SLCR's reset inputs."""
        if isinstance(ref, InternalRef) and ref.name in RESET_PINS:
            return Pin(f"slcr@{RESET_PINS[ref.name]}", None)
        if isinstance(ref, PinRef) and ref.port in ("mio", "emio") and ref.index is not None:
            count = MIO_PINS if ref.port == "mio" else EMIO_PINS
            if not 0 <= ref.index < count:
                raise ConfigError(f"{ref!r}: {ref.port} has pins 0-{count - 1}")
            number = ref.index if ref.port == "mio" else MIO_PINS + ref.index
            return Pin(
                f"gpio@{number}",
                ("gpio", number),
                ("gpio", f"SetPull {number} true"),
                ("gpio", f"SetPull {number} false"),
            )
        raise ConfigError(
            f"{ref!r} is not a Zynq-7000 pin (mio[0-53], emio[0-63], ps_por_b, ps_srst_b)"
        )

    def interface_kind(self, board: Board, device: Device, name: str) -> str | None:
        """What one of the device's names is as the end of a link (``uart``,
        ``can``, ``spi``...): a PS interface, or an instance of the block
        design that Jig models as a controller (an AXI Quad SPI is ``spi``).
        None for another name of the design: an exported port, or an instance
        whose model is no controller. A name the device doesn't have, and an
        instance Jig has no model for, are errors that name it."""
        if name in INTERFACES:
            return INTERFACES[name]
        design = self.design(board)
        instance = design.instances.get(name)
        if instance is not None:
            replaced = any(ip.name == name for ip in device.ips)
            placed = any(segment.slave == name for segment in design.slaves())
            if replaced or (placed and instance.ip in blockdesign.MODELS):
                return PL_INTERFACES.get(instance.ip)
            raise ConfigError(
                f"{device.name}.{name} is an instance of {design.source} ({instance.vlnv}) that "
                "Jig has no model for: give it one with an ip line, or leave it out"
            )
        if name in design.interfaces or name in design.ports:
            return None
        known = ", ".join(INTERFACES)
        modelled = sorted(
            {s.slave for s in design.slaves() if s.slave in design.instances}
            & {n for n, i in design.instances.items() if i.ip in blockdesign.MODELS}
        )
        if modelled:
            known += f"; the block design's {', '.join(modelled)}"
        raise ConfigError(f"{device.name} has no interface {name} (has: {known})")

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
        """Firmware roles and files, the XSA, register maps."""
        problems: list[str] = []
        try:
            self.xsa_path(board)
        except ConfigError as exc:
            problems.append(str(exc))
        roles = sorted(set(board.firmware) - set(_ROLES))
        if roles:
            problems.append(
                f"firmware: a Zynq-7000 takes the roles {', '.join(_ROLES)}, not {', '.join(roles)}"
            )
        for role, path in board.firmware.items():
            if not path.is_file():
                problems.append(f"firmware {role}: {path} not found (build the firmware)")
        for source in board.register_maps:
            try:
                airhdl.parse(source.package, source.module)
            except (ConfigError, OSError) as exc:
                problems.append(f"register map {source.target}: {exc}")
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
            peripheral, _, parameter = target.rpartition(".")
            if not peripheral:
                raise ConfigError(
                    f"board {config.board.name}: register map target {target!r} "
                    "is not <peripheral>.<parameter>"
                )
            by_peripheral.setdefault(peripheral, []).append(f'    {parameter}: "{path.as_posix()}"')
        for peripheral, entries in by_peripheral.items():
            lines += [f"{peripheral.rpartition('.')[2]}:", *entries, ""]
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
                if storage.size is not None and source.stat().st_size > storage.size:
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
        the BootROM keeps the core halted until power returns. Chips on the
        board's supply go to their power-on state at once, so their timers
        stop counting."""
        session.select_machine(config.name)
        session.execute("bootrom PowerOff")
        for peripheral in power_cycled(config, session):
            session.execute(f"{peripheral} PowerCycle")
        session.execute("slcr OnGPIO 0 true")
        session.execute("slcr OnGPIO 0 false")

    def power_on(self, config: MachineConfig, session: RenodeSession) -> None:
        session.select_machine(config.name)
        for peripheral in power_cycled(config, session):
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


_ENUM = re.compile(r"\.[A-Za-z_][A-Za-z0-9_]*")


def _fclk(clocks: Mapping[str, float]) -> dict[int, float]:
    """FCLK0 to FCLK3 by index, from the PS clocks by name."""
    return {int(name[4:]): value for name, value in clocks.items() if name.startswith("fclk")}


def _names_instance(ip: IpBlock) -> bool:
    return any(isinstance(value, DesignName) for value in ip.properties.values())


def _property(value: Any, key: str, where: str, names: Collection[str]) -> PropertyValue:
    """A property value of an ip line in Renode's units: whole numbers that
    a quantity left as a float (``100 MHz``) become integers, ``".Enum"``
    stays a bare word, a peripheral or an instance of the design becomes a
    reference, and lists and ranges take the platform description's form."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and abs(value - round(value)) <= 1e-6 * max(1.0, abs(value)):
        return round(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return Literal(value) if _ENUM.fullmatch(value) else value
    if isinstance(value, PeripheralRef):
        return Reference(value.peripheral)
    if isinstance(value, DesignName):
        if value.name not in names:
            listed = ", ".join(sorted(set(names) - blockdesign.platform_names())) or "none"
            raise ConfigError(
                f"{where}: {key}: {value.name} is no peripheral of the board or the platform, "
                f"ip line or modelled instance of the block design (the board's: {listed})"
            )
        return Reference(value.name)
    if isinstance(value, InternalRef):
        return Reference(value.name)
    return Literal(repl_value(value, key))


def _ip_where(device: Device, message: str) -> str:
    """The location an error about the PL belongs to: the ip line it names,
    else the device."""
    match = re.match(r"ip (\w+):", message)
    if match is not None:
        for ip in device.ips:
            if ip.name == match.group(1):
                return f"{ip.where}: "
    return f"{device.where}: "
