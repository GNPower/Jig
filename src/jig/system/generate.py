"""The Renode platform description Jig writes for a device, from its board.

A run writes it to ``<machine directory>/machine.repl`` and loads it;
``jig platform`` prints it. It is assembled in this order:

1. the family's platform (``using "xilinx/zynq7000.repl"``, a part's STM32
   platform);
2. what the device's arguments fix: clocks, boot straps, memories;
3. the programmable logic: a Zynq-7000 XSA's block design with the device's
   ``ip`` lines applied (``jig.ext.zynq7000.blockdesign``);
4. peripherals at their controllers (``@ qspi 0``, ``@ i2c4 0x40``), GPIO
   connections and pulls, and sensor inputs;
5. unmodelled ranges and property assignments from the device body;
6. the board's ``repl`` fragments, as written;
7. what Jig adds per run (CPU performance, register maps): the family
   driver's overlay.

Steps 1, 2 and 3 are the family's (``platform_lines``; a family that places
``ip`` lines says so with ``programmable_logic``). A family maps the device's
pins to Renode connections (``pin``), says what each name a link ends at is
(``interface_kind``) and how a converter's channel takes a voltage
(``analog``).
"""

from __future__ import annotations

import dataclasses
import re
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jig.errors import ConfigError
from jig.system.boards import Board
from jig.system.model import (
    AirhdlSource,
    ChannelRef,
    Device,
    InternalRef,
    Link,
    Peripheral,
    PeripheralInput,
    PeripheralOutput,
    PeripheralRef,
    PinRef,
)

if TYPE_CHECKING:
    from jig.system.socs import MachineConfig, SocDriver

ANALOG_INPUTS = "inputs"  # the converter implements IAnalogInputs: SetChannelVoltage
CHANNEL_SOURCE = "source"  # an Analog.ADCChannelSource on the channel

_ENUM = re.compile(r"\.[A-Za-z_][A-Za-z0-9_]*")
_SIZE_WORDS = ("size", "Size")


@dataclass(frozen=True)
class Pin:
    """A device pin as Renode connects it: ``input`` names it as a GPIO
    destination (``gpio@7``), ``output`` as a numbered source (``("gpio",
    7)``) when it can drive, and ``pull`` gives the init line of the entry
    that pulls it up or down (``("gpio", "SetPull 7 true")``)."""

    input: str | None
    output: tuple[str, int] | None
    pull_up: tuple[str, str] | None = None
    pull_down: tuple[str, str] | None = None


class Writer:
    """Platform-description entries in order. Every monitor command the
    writer gives one entry goes into a single ``init add:`` block, the one
    its first command opened."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._init_ends: dict[str, int] = {}  # entry -> where its init block's next command goes

    def comment(self, text: str) -> None:
        for line in text.splitlines():
            self.lines.append(f"// {line}" if line else "//")

    def blank(self) -> None:
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def entry(self, head: str, body: Sequence[str] = ()) -> None:
        self.lines.append(head)
        self.lines.extend(f"    {line}" for line in body)
        self.lines.append("")

    def init(self, entry: str, commands: Sequence[str]) -> None:
        """Monitor commands an entry runs once the platform is built,
        after any init the entry already has."""
        if not commands:
            return
        lines = [f"        {command}" for command in commands]
        end = self._init_ends.get(entry)
        if end is None:
            self.lines += [f"{entry}:", "    init add:", *lines]
            self._init_ends[entry] = len(self.lines)
            self.lines.append("")
            return
        self.lines[end:end] = lines
        for name, index in self._init_ends.items():
            if index >= end:
                self._init_ends[name] = index + len(lines)

    def text(self) -> str:
        while self.lines and self.lines[-1] == "":
            self.lines.pop()
        return "\n".join(self.lines) + "\n"


# --------------------------------------------------------------- values


def repl_value(value: Any, key: str = "") -> str:
    """A value as a platform description writes it: numbers (a size in
    hex), text in quotes (a leading dot keeps an enum value as it is:
    ``.GICv1``), true/false, lists, ranges ``<low, high>``, and the name of a
    peripheral or of a device's own peripheral for a reference."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and abs(value - round(value)) <= 1e-6 * max(1.0, abs(value)):
        value = round(value)
    if isinstance(value, int):
        if any(word in key for word in _SIZE_WORDS) and value >= 0:
            return f"0x{value:X}"
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        if _ENUM.fullmatch(value):
            return value
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, PeripheralRef):
        return value.peripheral
    if isinstance(value, InternalRef):
        return value.name
    if isinstance(value, (list, tuple)) and len(value) == 2 and key == "<range>":
        return f"<0x{value[0]:X}, 0x{value[1]:X}>"
    if isinstance(value, list):
        return "[" + ", ".join(repl_value(item) for item in value) + "]"
    low, high = getattr(value, "low", None), getattr(value, "high", None)
    if isinstance(low, int) and isinstance(high, int):
        return f"<{low:#x}, {high:#x}>"
    raise ConfigError(
        f"{key or 'a property'}: Jig cannot write {value!r} in a platform description"
    )


def properties(values: Mapping[str, Any]) -> list[str]:
    """Property lines, leaving out what the per-run overlay fills in
    (``airhdl(...)`` register maps)."""
    return [
        f"{name}: {repl_value(value, name)}"
        for name, value in values.items()
        if not isinstance(value, AirhdlSource)
    ]


# ----------------------------------------------------------- the description


def description(board: Board, driver: SocDriver, config: MachineConfig | None = None) -> str:
    """The platform description of the board's device, steps 1 to 7 (the
    overlay, step 7, when a machine's settings are given)."""
    device = board.device
    writer = Writer()
    options = ", ".join(f"{name} {value}" for name, value in board.option_values.items())
    header = (
        f"Board {board.name}{f' ({options})' if options else ''}, device {device.name} "
        f"({device.family}): the platform description Jig writes from "
        f"{board.directory.name}/{board.source.name}."
    )
    writer.comment("\n".join(textwrap.wrap(header, 76)))
    writer.blank()
    family_lines = getattr(driver, "platform_lines", None)
    if not callable(family_lines):
        raise ConfigError(
            f"board {board.name}: device family {device.family} writes no platform description"
        )
    if device.ips and not getattr(driver, "programmable_logic", False):
        raise ConfigError(
            f"{device.ips[0].where}: device family {device.family} has no programmable logic "
            "for ip lines"
        )
    _check_link_ends(board, device, driver)
    usings = [line for line in _fragment_lines(board) if line.startswith("using ")]
    body: list[str] = family_lines(board, device)
    head = [line for line in body if line.startswith("using ")]
    writer.lines.extend([*head, *usings])
    writer.blank()
    rest = [line for line in body if not line.startswith("using ")]
    while rest and not rest[0]:
        rest.pop(0)
    writer.lines.extend(rest)
    writer.blank()
    _peripherals(writer, board, device, driver)
    _links(writer, board, device, driver)
    _sensors(writer, board, device, driver)
    _device_body(writer, board, device, driver)
    _fragments(writer, board)
    if config is not None:
        overlay = driver.overlay(config).strip("\n")
        if overlay:
            writer.comment("Per run: CPU performance and register maps (the family's overlay)")
            writer.lines.extend(overlay.splitlines())
    return writer.text()


def _peripherals(writer: Writer, board: Board, device: Device, driver: SocDriver) -> None:
    if not board.peripherals:
        return
    writer.comment("Peripherals at their controllers")
    for part in board.peripherals:
        if part.bus is not None:
            _device_name, controller, address = part.bus
            link = next(link for link in board.links if part.name in link.targets.values())
            where = f"{controller} {address if link.kind != 'i2c' else f'0x{address:X}'}"
        else:
            where = "none"
        lines = properties(part.properties)
        outputs = _outputs(board, device, driver, part)
        writer.entry(f"{part.name}: {part.model} @ {where}", [*lines, *outputs])


def _outputs(board: Board, device: Device, driver: SocDriver, part: Peripheral) -> list[str]:
    """A peripheral's GPIO connections: ``IRQ -> gpio@7``."""
    lines = []
    for link in board.links:
        source = link.source
        if isinstance(source, PeripheralOutput) and source.peripheral == part.name:
            targets = " | ".join(_sink(board, device, driver, sink, link) for sink in link.sinks)
            lines.append(f"{source.name} -> {targets}")
    return lines


def _sink(board: Board, device: Device, driver: SocDriver, sink: Any, link: Link) -> str:
    if isinstance(sink, PeripheralInput):
        return f"{sink.peripheral}@{sink.number}"
    pin = _pin(driver, device, sink, link)
    if pin.input is None:
        raise ConfigError(f"{link.where}: link {link.name}: {sink!r} is not an input")
    return pin.input


def _pin(driver: SocDriver, device: Device, ref: Any, link: Link) -> Pin:
    resolve = getattr(driver, "pin", None)
    if not callable(resolve):
        raise ConfigError(f"{link.where}: device family {device.family} has no pins")
    try:
        found: Pin = resolve(device, ref)
    except ConfigError as exc:
        raise ConfigError(f"{link.where}: link {link.name}: {exc}") from None
    return found


def _links(writer: Writer, board: Board, device: Device, driver: SocDriver) -> None:
    gpio = [link for link in board.links if link.kind == "gpio"]
    if not gpio:
        return
    writer.comment("GPIO links")
    for link in gpio:
        source = link.source
        if isinstance(source, (PinRef, InternalRef)):
            pin = _pin(driver, device, source, link)
            if pin.output is None:
                raise ConfigError(f"{link.where}: link {link.name}: {source!r} is not an output")
            entry, number = pin.output
            targets = " | ".join(_sink(board, device, driver, sink, link) for sink in link.sinks)
            writer.entry(f"{entry}:", [f"{number} -> {targets}"])
        pull = link.settings.get("pull")
        if pull is None:
            continue
        pulled = []
        for end in (source, *link.sinks):
            if isinstance(end, (PinRef, InternalRef)):
                pin = _pin(driver, device, end, link)
                found = pin.pull_up if pull == "up" else pin.pull_down
                if found is not None:
                    pulled.append(found)
        if not pulled:
            writer.comment(f"{link.name}: pulled {pull}; no device pin on it takes a pull")
        for entry, command in pulled:
            writer.init(entry, [command])


def _sensors(writer: Writer, board: Board, device: Device, driver: SocDriver) -> None:
    """Sensor inputs that need text: a channel source on a Renode converter,
    and a starting voltage on a converter that implements IAnalogInputs."""
    section = Writer()
    for spec in board.sensor_specs:
        kind = _analog(driver, device, spec.channel)
        owner = spec.channel.owner
        entry = owner.peripheral if isinstance(owner, PeripheralRef) else owner.name
        if kind == CHANNEL_SOURCE:
            micro = round((spec.initial or 0.0) * 1_000_000)
            section.entry(
                f"{spec.name}_input: Analog.ADCChannelSource @ {entry} {spec.channel.channel}",
                [f"defaultValueMicroVolts: {micro}"],
            )
        elif spec.initial is not None:
            section.init(entry, [f"SetChannelVoltage {spec.channel.channel} {spec.initial!r}"])
    if section.lines:
        writer.comment("Sensor inputs")
        writer.lines.extend(section.lines)


def with_channel_sources(board: Board, driver: SocDriver) -> Board:
    """The board with each sensor's ``source`` filled in where the
    description puts an ``Analog.ADCChannelSource`` on its channel: the
    source's name in the machine's monitor (``adc1.rail_input``), which
    ``set_sensor`` drives instead of ``SetChannelVoltage``."""
    if not board.devices or not board.sensor_specs:
        return board
    sources = {
        spec.name: f"{spec.channel.owner.name}.{spec.name}_input"
        for spec in board.sensor_specs
        if isinstance(spec.channel.owner, InternalRef)
        and _analog(driver, board.device, spec.channel) == CHANNEL_SOURCE
    }
    if not sources:
        return board
    sensors = tuple(
        dataclasses.replace(sensor, source=sources.get(sensor.name, sensor.source))
        for sensor in board.sensors
    )
    return dataclasses.replace(board, sensors=sensors)


def _analog(driver: SocDriver, device: Device, channel: ChannelRef) -> str:
    if isinstance(channel.owner, PeripheralRef):
        return ANALOG_INPUTS
    decide = getattr(driver, "analog", None)
    if not callable(decide):
        return ANALOG_INPUTS
    kind: str = decide(device, channel.owner.name)
    return kind


def interface_kind(board: Board, device: Device, driver: SocDriver, name: str) -> str | None:
    """What the family says a name of the device is as a link's end
    (``uart``, ``spi``...), or None when the family doesn't say; a name the
    family rejects raises its ConfigError."""
    kind_of = getattr(driver, "interface_kind", None)
    if not callable(kind_of):
        return None
    kind: str | None = kind_of(board, device, name)
    return kind


def _check_link_ends(board: Board, device: Device, driver: SocDriver) -> None:
    """Every device interface a link ends at is one the family has: the
    members of UART and CAN links and the controllers of buses."""
    for link in board.links:
        ends = [*link.members, *([link.controller] if link.controller is not None else [])]
        for end in ends:
            try:
                interface_kind(board, device, driver, end.name)
            except ConfigError as exc:
                raise ConfigError(f"{link.where}: link {link.name}: {exc}") from None


def _device_body(writer: Writer, board: Board, device: Device, driver: SocDriver) -> None:
    if device.unmodelled:
        writer.comment("Unmodelled ranges: they read 0, without a warning per access")
        writer.init(
            "sysbus",
            [
                f'Tag <0x{u.low:X}, 0x{u.high:X}> "{u.name} (unmodelled)" 0 false true'
                for u in device.unmodelled
            ],
        )
    if device.assignments:
        writer.comment("Properties the device body sets")
        by_target: dict[str, list[str]] = {}
        for assignment in device.assignments:
            if isinstance(assignment.value, AirhdlSource):
                continue
            by_target.setdefault(assignment.target, []).append(
                f"{assignment.name}: {repl_value(assignment.value, assignment.name)}"
            )
        for target, lines in by_target.items():
            writer.entry(f"{target}:", lines)


def _fragment_lines(board: Board) -> list[str]:
    lines: list[str] = []
    for path in board.repl:
        lines.extend(line.rstrip() for line in path.read_text(encoding="utf-8").splitlines())
    return lines


def _fragments(writer: Writer, board: Board) -> None:
    for path in board.repl:
        writer.comment(f"From {_label(board, path)}")
        text = path.read_text(encoding="utf-8").splitlines()
        writer.lines.extend(line.rstrip() for line in text if not line.startswith("using "))
        writer.blank()


def _label(board: Board, path: Path) -> str:
    try:
        return path.relative_to(board.directory).as_posix()
    except ValueError:
        return path.as_posix()
