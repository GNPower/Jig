"""The board model a board file evaluates to: options, devices, peripherals,
links and sensors, with what each declaration said and where.

These are plain data. ``jig.system.boardfile`` builds them from a board file,
``jig.system.generate`` writes a Renode platform description from them and
``jig.system.boards.Board`` exposes them, with the fields board.yaml boards
had, to drivers and plugins.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from jig.errors import ConfigError
from jig.system.protocols import Layer

RESERVED_OPTIONS = ("boot", "mips", "storage", "power")
"""Settings every board takes from a system's board line; no option uses them."""


# --------------------------------------------------------------- references


class _Ref:
    """A name a board file declared, as an expression sees it. References
    have no plan members of their own; attributes and indexes reach what the
    name holds (``zynq.uart1``, ``zynq.mio[7]``, ``adc.channel[3]``)."""

    dsl_members: frozenset[str] = frozenset()


@dataclass(frozen=True, repr=False)
class DeviceRef(_Ref):
    """A device: ``zynq``. Its attributes are its interfaces, pins and
    internal peripherals (``zynq.uart1``, ``zynq.mio``, ``mcu.adc4``)."""

    device: str

    def __repr__(self) -> str:
        return self.device

    def dsl_attribute(self, name: str) -> InternalRef:
        return InternalRef(self.device, name)


@dataclass(frozen=True, repr=False)
class InternalRef(_Ref):
    """Something inside a device, by the name its family gives it: an
    interface (``zynq.can0``), a pin without a number (``zynq.ps_srst_b``), an
    internal peripheral (``mcu.adc4``), or a pin port (``zynq.mio``, indexed
    to a pin)."""

    device: str
    name: str

    def __repr__(self) -> str:
        return f"{self.device}.{self.name}"

    def dsl_attribute(self, name: str) -> Any:
        if name == "channel":
            return Channels(self)
        return None

    def dsl_index(self, index: Any) -> PinRef:
        return PinRef(self.device, self.name, _number(index, f"{self!r}[...]"))


@dataclass(frozen=True, repr=False)
class PinRef(_Ref):
    """A device pin: ``zynq.mio[7]``, ``mcu.pa[9]``; ``index`` is None for a
    pin the family names alone (``zynq.ps_por_b``)."""

    device: str
    port: str
    index: int | None = None

    def __repr__(self) -> str:
        suffix = "" if self.index is None else f"[{self.index}]"
        return f"{self.device}.{self.port}{suffix}"


@dataclass(frozen=True, repr=False)
class PeripheralRef(_Ref):
    """A peripheral: ``flash0``. ``chip.IRQ`` names one of its outputs,
    ``chip.gpio[0]`` one of its inputs and ``adc.channel[2]`` an analog input."""

    peripheral: str

    def __repr__(self) -> str:
        return self.peripheral

    def dsl_attribute(self, name: str) -> Any:
        if name == "channel":
            return Channels(self)
        if name == "gpio":
            return Inputs(self)
        return PeripheralOutput(self.peripheral, name)


@dataclass(frozen=True, repr=False)
class PeripheralOutput(_Ref):
    """A peripheral's output, by its GPIO property name: ``chip.IRQ``."""

    peripheral: str
    name: str

    def __repr__(self) -> str:
        return f"{self.peripheral}.{self.name}"


@dataclass(frozen=True, repr=False)
class Inputs(_Ref):
    """``chip.gpio``, indexed to an input: ``chip.gpio[0]``."""

    owner: PeripheralRef

    def __repr__(self) -> str:
        return f"{self.owner!r}.gpio"

    def dsl_index(self, index: Any) -> PeripheralInput:
        return PeripheralInput(self.owner.peripheral, _number(index, f"{self!r}[...]"))


@dataclass(frozen=True, repr=False)
class PeripheralInput(_Ref):
    """A peripheral's numbered input: ``chip.gpio[0]``."""

    peripheral: str
    number: int

    def __repr__(self) -> str:
        return f"{self.peripheral}.gpio[{self.number}]"


@dataclass(frozen=True, repr=False)
class Channels(_Ref):
    """``adc.channel`` or ``mcu.adc4.channel``, indexed to an analog input."""

    owner: PeripheralRef | InternalRef

    def __repr__(self) -> str:
        return f"{self.owner!r}.channel"

    def dsl_index(self, index: Any) -> ChannelRef:
        return ChannelRef(self.owner, _number(index, f"{self!r}[...]"))


@dataclass(frozen=True, repr=False)
class ChannelRef(_Ref):
    """An analog input: ``adc.channel[2]``, ``mcu.adc4.channel[13]``."""

    owner: PeripheralRef | InternalRef
    channel: int

    def __repr__(self) -> str:
        return f"{self.owner!r}.channel[{self.channel}]"


@dataclass(frozen=True, repr=False)
class LinkRef(_Ref):
    """A link of the board, by name."""

    link: str

    def __repr__(self) -> str:
        return self.link


@dataclass(frozen=True, repr=False)
class DesignName(_Ref):
    """A name in an ``ip`` line that no declaration of the board gives: a
    port the block design exports (``on: M00_AXI``, ``irq: filter_intr``) or
    one of its instances (``bram: axi_bram_ctrl_0``). The device's family
    checks it against the design when it writes the platform description."""

    name: str

    def __repr__(self) -> str:
        return self.name


def _number(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{what}: an index is a whole number, not {value!r}")
    return value


# ------------------------------------------------------------- what values are


class _Value:
    """A value a board file computes. It has no plan members, and calls
    that return it keep it as it is."""

    dsl_members: ClassVar[frozenset[str]] = frozenset()


@dataclass(frozen=True)
class ModelSpec(_Value):
    """A model and its properties: ``"SPI.Micron_MT25Q" (size: 32 MiB)``."""

    model: str
    properties: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChoiceSpec(_Value):
    """``choice(1, 2)`` or ``choice({model: "...", rtl: "..."})``: the values
    an option takes (with descriptions) and its default."""

    values: tuple[Any, ...]
    descriptions: Mapping[Any, str]
    default: Any


@dataclass(frozen=True)
class AirhdlSource(_Value):
    """``airhdl(package, module)``: a property whose value is the JSON
    register map Jig generates from the airhdl sources."""

    package: Path
    module: Path | None = None


@dataclass(frozen=True)
class MemorySpec(_Value):
    """``memory(size: 64 KiB)``: a block of memory in the programmable logic."""

    properties: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CosimSource(_Value):
    """``cosim(build:, sources:, library:, ...)``: RTL under Verilator in place
    of a model; the other arguments are the co-simulated peripheral's
    properties (``frequency``) and its placement (``on``, ``irq``)."""

    build: Path
    sources: Path
    inputs: tuple[Path, ...]
    library: str
    context: str
    properties: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FirmwareBuild(_Value):
    """``build(script, sources:, output:)``: an ELF built from source, paths
    relative to the board file."""

    script: Path
    sources: Path
    output: str


@dataclass(frozen=True)
class FirmwareCommand(_Value):
    """``command([...], output:)``: an ELF a command makes in the board's root."""

    arguments: tuple[str, ...]
    output: str


@dataclass(frozen=True)
class LinkSpec(_Value):
    """``uart(...)``, ``can(...)``, ``spi(...)`` and the other link kinds in a
    board file: their settings and ends, as given."""

    kind: str
    settings: Mapping[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------- the model


@dataclass(frozen=True)
class OptionSpec:
    """A choice a board offers the systems that use it."""

    name: str
    default: Any
    choices: Mapping[Any, str] | None = None  # value -> description; None: any value
    where: str = ""

    def accept(self, value: Any, where: str) -> Any:
        """``value`` for this option: a choice, or a value of the default's
        type. Text from jig.toml or ``--option`` is read as that value."""
        if isinstance(value, str) and not isinstance(self.default, str):
            value = self._from_text(value, where)
        if self.choices is not None:
            for choice in self.choices:
                if choice == value and type(choice) is type(value):
                    return choice
            listed = ", ".join(map(_show, self.choices))
            raise ConfigError(f"{where}: option {self.name} is one of {listed}, not {_show(value)}")
        if type(value) is not type(self.default) and not (
            isinstance(self.default, float) and isinstance(value, int)
        ):
            raise ConfigError(
                f"{where}: option {self.name} takes a {_kind(self.default)}, not {_show(value)}"
            )
        return value

    def describe(self) -> str:
        """The option as ``jig boards`` prints it: its values, the default
        marked, with their descriptions; or the kind of value it takes."""
        if self.choices is None:
            return f"any {_kind(self.default)}, {_show(self.default)} by default"
        parts = []
        for value, text in self.choices.items():
            default = value == self.default and type(value) is type(self.default)
            part = _show(value) + (" (default)" if default else "")
            parts.append(f"{part}: {text}" if text else part)
        return ("; " if any(self.choices.values()) else ", ").join(parts)

    def _from_text(self, text: str, where: str) -> Any:
        candidates = list(self.choices) if self.choices is not None else [self.default]
        for candidate in candidates:
            if _show(candidate) == text or str(candidate) == text:
                return candidate
        sample = candidates[0]
        try:
            if isinstance(sample, bool):
                return {"true": True, "false": False}[text.lower()]
            if isinstance(sample, int):
                return int(text, 0)
            if isinstance(sample, float):
                return float(text)
        except (KeyError, ValueError):
            pass
        return text


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return repr(value)


def _kind(value: Any) -> str:
    return {bool: "true or false", int: "whole number", float: "number", str: "text"}.get(
        type(value), type(value).__name__
    )


@dataclass(frozen=True)
class Peripheral:
    """A chip wired to a device: its model, its properties and the
    controller its bus link registers it at (``qspi 0``)."""

    name: str
    model: str
    properties: Mapping[str, Any]
    where: str
    bus: tuple[str, str, int] | None = None  # (device, controller, address) once wired

    @property
    def path(self) -> str:
        """Its name in the machine's monitor: ``qspi.flash0``, or ``flash0``
        for a peripheral on no bus."""
        return self.name if self.bus is None else f"{self.bus[1]}.{self.name}"


@dataclass(frozen=True)
class BootMode:
    name: str
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtocolInstance:
    """``protocol csp(...)`` or ``protocol NAME: LAYER(...)`` in a device."""

    name: str
    layer: Layer
    where: str


@dataclass(frozen=True)
class InterfaceStack:
    """``interface NAME: STACK``: the layers a device runs on an interface,
    top first."""

    interface: str
    layers: tuple[Layer, ...]
    where: str

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(layer.name for layer in self.layers)


@dataclass(frozen=True)
class IpBlock:
    """``ip NAME: MODEL``: a block of programmable logic, its model and
    where it sits (``on``: a port or instance name, a map of such names to
    window sizes, or an address range (low, high)); ``irq``: an exported
    interrupt port or an IRQ_F2P index. Property values that name an
    instance of the block design are ``DesignName``s."""

    name: str
    model: str
    properties: Mapping[str, Any]
    on: Any
    irq: Any
    where: str
    memory: bool = False
    cosim: CosimSource | None = None


@dataclass(frozen=True)
class Unmodelled:
    name: str
    low: int
    high: int
    where: str


@dataclass(frozen=True)
class Assignment:
    """``fdcan1.kernelFrequency = 16 MHz``: a property of one of the device's
    own peripherals."""

    target: str
    name: str
    value: Any
    where: str


@dataclass(frozen=True)
class Device:
    """A part that runs code and that Renode simulates: one machine."""

    name: str
    family: str
    arguments: Mapping[str, Any]
    where: str
    firmware: Any = None
    boot: tuple[BootMode, ...] = ()
    protocols: tuple[ProtocolInstance, ...] = ()
    interfaces: tuple[InterfaceStack, ...] = ()
    ips: tuple[IpBlock, ...] = ()
    unmodelled: tuple[Unmodelled, ...] = ()
    assignments: tuple[Assignment, ...] = ()
    commands: tuple[str, ...] = ()
    driver: str | None = None

    def stack(self, interface: str) -> InterfaceStack | None:
        return next((s for s in self.interfaces if s.interface == interface), None)

    def instances(self, layer: str) -> list[ProtocolInstance]:
        """The device's protocol instances of one layer kind (``csp``)."""
        return [p for p in self.protocols if p.layer.name == layer]


@dataclass(frozen=True)
class Link:
    """A connection on the board: a UART line, a CAN bus, an SPI, QSPI or
    I2C bus, a GPIO net."""

    name: str
    kind: str
    external: bool
    where: str
    settings: Mapping[str, Any] = field(default_factory=dict)
    members: tuple[InternalRef, ...] = ()  # uart, can: device interfaces
    controller: InternalRef | None = None  # spi, qspi, i2c
    targets: Mapping[int, str] = field(default_factory=dict)  # address -> peripheral
    source: Any = None  # gpio: one output
    sinks: tuple[Any, ...] = ()  # gpio: inputs


@dataclass(frozen=True)
class SensorSpec:
    """``sensor NAME: adc.channel[3] = 1.2 V``."""

    name: str
    channel: ChannelRef
    initial: float | None
    where: str
