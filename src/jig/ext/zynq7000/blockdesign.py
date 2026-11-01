"""Zynq-7000 block designs: the programmable logic an XSA describes, written as
Renode platform text.

An XSA is a zip archive. For each block design Vivado puts a hardware handoff
file (``.hwh``) in it: the design's instances with their VLNVs and parameters,
the address map of every master, the ports of the design and the nets that
join instance ports. :func:`read` parses the handoff holding the
``processing_system7`` instance into a :class:`BlockDesign`, which derives
what a simulation needs: the slaves on the PS's map, the source of each
``IRQ_F2P`` line, reset domains, the drivers of AXI GPIO inputs, the AXI Quad
SPI's SCK and the AXI CDMA's reach. :func:`pl_section` writes the platform
text, with the device's ``ip`` lines (:class:`IpOverride`) applied.

The module knows nothing of Jig's board model: its inputs are a path, the
FCLK frequencies and the overrides, and its output is text.
"""

from __future__ import annotations

import functools
import itertools
import re
import textwrap
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jig.errors import ConfigError

PS7 = "processing_system7"

MODELS: dict[str, str] = {
    "xilinx.com:ip:axi_gpio": "Jig.Xilinx.XilinxAxiGpio",
    "xilinx.com:ip:axi_quad_spi": "Jig.Xilinx.XilinxAxiQuadSpi",
    "xilinx.com:ip:axi_cdma": "Jig.Xilinx.XilinxAxiCdma",
    "xilinx.com:ip:axi_bram_ctrl": "Memory.MappedMemory",
}
"""The model of each IP Jig simulates, by VLNV without the version."""

STRUCTURAL = frozenset(
    {
        "xilinx.com:ip:processing_system7",
        "xilinx.com:ip:xlconcat",
        "xilinx.com:ip:xlconstant",
        "xilinx.com:ip:xlslice",
        "xilinx.com:ip:proc_sys_reset",
        "xilinx.com:ip:axi_interconnect",
        "xilinx.com:ip:smartconnect",
        "xilinx.com:ip:blk_mem_gen",
    }
)
"""IP that joins other IP and gets no platform text of its own."""

COSIM = "CoSimulated.CoSimulatedPeripheral"

# The interrupt output of each modelled IP (PG144, PG153, PG034).
_INTERRUPT_PORTS = {
    "xilinx.com:ip:axi_gpio": "ip2intc_irpt",
    "xilinx.com:ip:axi_quad_spi": "ip2intc_irpt",
    "xilinx.com:ip:axi_cdma": "cdma_introut",
}

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_FCLK_CLK = re.compile(r"FCLK_CLK([0-3])")
_FCLK_RESET = re.compile(r"FCLK_RESET([0-3])_N")
_PS7_MARK = re.compile(rb'MODTYPE="processing_system7"|:processing_system7:')
_ACTIVE_LOW_RESETS = frozenset({"peripheral_aresetn", "interconnect_aresetn"})
_TRACE_LIMIT = 64

Pin = tuple[str | None, str]
"""One end of a net: (instance, port), with instance None for a port of the design."""


# ---------------------------------------------------------------- the design


@dataclass(frozen=True)
class Port:
    """A port of an instance, or of the design itself (``instance`` None)."""

    instance: str | None
    name: str
    direction: str
    """``I``, ``O`` or ``IO``."""
    left: int | None = None
    right: int | None = None
    signal: str = ""
    """What the port carries, as Vivado labels it (``SIGIS``): ``clk``, ``rst``,
    ``INTERRUPT``, ``undef``."""
    net: str | None = None
    clock_hz: float | None = None
    """The frequency Vivado recorded on a clock port (``CLKFREQUENCY``)."""
    connections: tuple[tuple[str, str], ...] = ()
    """The (instance, port) pairs the file lists at the other ends of the net."""

    @property
    def width(self) -> int:
        if self.left is None or self.right is None:
            return 1
        return abs(self.left - self.right) + 1


@dataclass(frozen=True)
class BusInterface:
    """A bus interface of an instance, or one the design exports."""

    name: str
    mode: str
    """``MASTER``, ``SLAVE``, ``INITIATOR``, ``TARGET`` or ``MONITOR``."""
    bus: str
    """The bus it joins (``BUSNAME``)."""


@dataclass(frozen=True)
class Segment:
    """One range of a master's address map (a ``MEMRANGE``)."""

    master: str
    interface: str
    """The master's interface (``M_AXI_GP0``)."""
    slave: str
    """The slave instance, or the name of an exported AXI port."""
    slave_interface: str
    block: str
    """The slave's address block (``Reg``, ``Mem0``)."""
    base: int
    high: int
    memory: bool
    """Vivado's memory type is ``MEMORY``; otherwise ``REGISTER``."""

    @property
    def size(self) -> int:
        return self.high - self.base + 1

    def __str__(self) -> str:
        return f"0x{self.base:08X}-0x{self.high:08X}"


@dataclass(frozen=True)
class Instance:
    """One instance of the block design."""

    name: str
    vlnv: str
    kind: str
    """The IP's name (``axi_gpio``), from the VLNV or, without one, ``MODTYPE``."""
    parameters: Mapping[str, str] = field(default_factory=dict)
    ports: Mapping[str, Port] = field(default_factory=dict)
    interfaces: Mapping[str, BusInterface] = field(default_factory=dict)
    memory_map: tuple[Segment, ...] = ()
    """What the instance reaches as a master."""

    @property
    def ip(self) -> str:
        """The VLNV without its version: ``xilinx.com:ip:axi_gpio``."""
        return ":".join(self.vlnv.split(":")[:3])

    def number(self, parameter: str) -> int | None:
        """A parameter as an integer; None when it is absent or not a number."""
        value = self.parameters.get(parameter)
        if value is None:
            return None
        try:
            return _number(value)
        except ValueError:
            return None


@dataclass(frozen=True)
class Signal:
    """What drives one bit: an output of an instance, an input port of the
    design (``instance`` None), or an ``xlconstant`` (``constant`` set)."""

    instance: str | None
    port: str
    bit: int = 0
    width: int = 1
    """The width of the driving port."""
    constant: int | None = None

    def __str__(self) -> str:
        if self.constant is not None:
            return f"constant {self.constant} ({self.instance})"
        name = self.port if self.instance is None else f"{self.instance}/{self.port}"
        return f"{name}[{self.bit}]" if self.width > 1 else name


@dataclass(frozen=True)
class Interrupt:
    """One line of the PS's ``IRQ_F2P`` port."""

    line: int
    id: int
    """The shared peripheral interrupt ID (:func:`spi_id`)."""
    source: Signal | None

    @property
    def gic(self) -> int:
        """The line of Renode's GIC: ID N is ``gic@(N-32)``."""
        return self.id - 32


@dataclass(frozen=True)
class ResetDomain:
    """The reset that holds an AXI slave."""

    block: str | None
    """The ``proc_sys_reset`` driving its reset input; None when a reset
    output of the PS drives it directly."""
    fclk: int | None
    """n of the ``FCLK_RESETn_N`` behind it; None when the reset comes from
    outside the PS."""


@dataclass
class BlockDesign:
    """A block design as its hardware handoff describes it."""

    source: str = ""
    """Where it came from: ``<xsa name>:<handoff>``, or the XSA's name alone
    when it holds no handoff."""
    name: str = ""
    instances: dict[str, Instance] = field(default_factory=dict)
    ports: dict[str, Port] = field(default_factory=dict)
    """The design's own ports."""
    interfaces: dict[str, BusInterface] = field(default_factory=dict)
    """The bus interfaces the design exports."""
    _drivers: dict[Pin, Pin] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._drivers = _driver_index(self.instances, self.ports)

    # -------------------------------------------------------------- maps

    @property
    def ps(self) -> Instance | None:
        """The ``processing_system7`` instance; None in a design without PL."""
        for instance in self.instances.values():
            if instance.kind == PS7:
                return instance
        return None

    def slaves(self) -> list[Segment]:
        """The PS's address map, by base address."""
        ps = self.ps
        return sorted(ps.memory_map, key=lambda s: s.base) if ps is not None else []

    def exported_axi(self) -> dict[str, list[Segment]]:
        """Each exported AXI port the PS reaches, with its address segments."""
        exported: dict[str, list[Segment]] = {}
        for segment in self.slaves():
            if segment.slave in self.interfaces and segment.slave not in self.instances:
                exported.setdefault(segment.slave, []).append(segment)
        return exported

    def reach(self, master: str) -> list[Segment]:
        """What a master reaches, from its own address map, by base address."""
        return sorted(self.instances[master].memory_map, key=lambda s: s.base)

    # -------------------------------------------------------------- nets

    def driver(self, instance: str | None, port: str, bit: int = 0) -> Signal | None:
        """What drives bit ``bit`` of a port, followed back through ``xlconcat``
        and ``xlslice`` blocks; None for a bit nothing drives."""
        for _ in range(_TRACE_LIMIT):
            source = self._drivers.get((instance, port))
            if source is None:
                return None
            owner, name = source
            if owner is None:
                width = self.ports[name].width if name in self.ports else 1
                return Signal(None, name, bit, width) if bit < width else None
            block = self.instances[owner]
            output = block.ports.get(name)
            width = output.width if output is not None else 1
            if bit >= width:
                return None
            if block.kind == "xlconcat" and name == "dout":
                found = _concat_input(block, bit)
                if found is None:
                    return None
                instance, (port, bit) = owner, found
                continue
            if block.kind == "xlslice" and name == "Dout":
                low = block.number("DIN_TO") or 0
                high = block.number("DIN_FROM")
                if high is not None and high < low:
                    return None
                instance, port, bit = owner, "Din", low + bit
                continue
            if block.kind == "xlconstant" and name == "dout":
                value = block.number("CONST_VAL") or 0
                return Signal(owner, name, bit, width, constant=(value >> bit) & 1)
            return Signal(owner, name, bit, width)
        raise ConfigError(
            f"{self.source}: the net driving {instance}/{port} loops through concat or slice blocks"
        )

    def interrupts(self) -> list[Interrupt]:
        """Every line of ``IRQ_F2P``, with its interrupt ID and what drives it."""
        ps = self.ps
        if ps is None or "IRQ_F2P" not in ps.ports:
            return []
        inputs = ps.ports["IRQ_F2P"].width
        mode = ps.parameters.get("C_IRQ_F2P_MODE", "DIRECT")
        return [
            Interrupt(line, spi_id(line, inputs, mode), self.driver(ps.name, "IRQ_F2P", line))
            for line in range(inputs)
        ]

    def exported_interrupts(self) -> dict[str, list[Interrupt]]:
        """The design's input ports that reach ``IRQ_F2P``, each with its lines
        in the order of the port's bits."""
        exported: dict[str, list[Interrupt]] = {}
        for interrupt in self.interrupts():
            source = interrupt.source
            if source is not None and source.instance is None:
                exported.setdefault(source.port, []).append(interrupt)
        for lines in exported.values():
            lines.sort(key=lambda i: i.source.bit if i.source is not None else 0)
        return exported

    # ------------------------------------------------------------ resets

    def fclk_reset(self, signal: Signal | None) -> int | None:
        """n when ``signal`` is high exactly while ``FCLK_RESETn_N`` is released:
        that output of the PS itself, or an active-low output of a
        ``proc_sys_reset`` whose ``ext_reset_in`` it drives."""
        for _ in range(_TRACE_LIMIT):
            if signal is None or signal.instance is None:
                return None
            block = self.instances[signal.instance]
            if block.kind == PS7:
                match = _FCLK_RESET.fullmatch(signal.port)
                return int(match[1]) if match else None
            if block.kind != "proc_sys_reset" or signal.port not in _ACTIVE_LOW_RESETS:
                return None
            signal = self.driver(block.name, "ext_reset_in")
        return None

    def reset_domain(self, instance: str) -> ResetDomain | None:
        """The reset holding ``instance``: the source of its first ``*aresetn``
        input that a reset block or the PS drives."""
        block = self.instances[instance]
        for port in block.ports.values():
            if port.direction == "I" and port.name.lower().endswith("aresetn"):
                domain = self._domain(self.driver(instance, port.name))
                if domain is not None:
                    return domain
        return None

    def interface_reset(self, name: str) -> ResetDomain | None:
        """The reset of an exported AXI port: that of the interconnect port it
        leaves through (``M02_AXI``: ``M02_ARESETN``, else the block's own
        ``ARESETN``)."""
        exported = self.interfaces.get(name)
        if exported is None:
            return None
        for block in self.instances.values():
            lowered = {port.lower(): port for port in block.ports}
            for interface in block.interfaces.values():
                if interface.bus != exported.bus:
                    continue
                stem = interface.name[:-4] if interface.name.upper().endswith("_AXI") else ""
                for candidate in (f"{stem}_ARESETN", f"{interface.name}_ARESETN", "ARESETN"):
                    port = lowered.get(candidate.lower())
                    if port is not None:
                        domain = self._domain(self.driver(block.name, port))
                        if domain is not None:
                            return domain
        return None

    def _domain(self, signal: Signal | None) -> ResetDomain | None:
        if signal is None or signal.instance is None:
            return None
        source = self.instances[signal.instance]
        if source.kind == "proc_sys_reset":
            return ResetDomain(source.name, self.fclk_reset(signal))
        if source.kind == PS7 and _FCLK_RESET.fullmatch(signal.port):
            return ResetDomain(None, self.fclk_reset(signal))
        return None

    # -------------------------------------------------- inputs and clocks

    def gpio_inputs(self, instance: str) -> dict[int, Signal | None]:
        """What drives each input of an AXI GPIO, numbered as
        ``Jig.Xilinx.XilinxAxiGpio`` numbers them: bit n of channel 1
        (``gpio_io_i``) is input n, bit n of channel 2 (``gpio2_io_i``) input
        32 + n."""
        block = self.instances[instance]
        inputs: dict[int, Signal | None] = {}
        for name, first in (("gpio_io_i", 0), ("gpio2_io_i", 32)):
            port = block.ports.get(name)
            if port is not None:
                for bit in range(port.width):
                    inputs[first + bit] = self.driver(instance, name, bit)
        return inputs

    def clock_hz(self, instance: str, port: str, fclk_hz: Mapping[int, float]) -> float | None:
        """The frequency on a clock input: ``fclk_hz[n]`` when the PS's
        ``FCLK_CLKn`` drives it, else what Vivado recorded on the port."""
        signal = self.driver(instance, port)
        if signal is not None and signal.instance in self.instances:
            match = _FCLK_CLK.fullmatch(signal.port)
            ps = self.instances[signal.instance].kind == PS7
            if ps and match and int(match[1]) in fclk_hz:
                return float(fclk_hz[int(match[1])])
        recorded = self.instances[instance].ports.get(port)
        return recorded.clock_hz if recorded is not None else None

    def spi_clock_hz(self, instance: str, fclk_hz: Mapping[int, float]) -> float:
        """SCK of an AXI Quad SPI: the clock on ``ext_spi_clk`` divided by
        ``C_SCK_RATIO``, the IP's frequency ratio (PG153)."""
        block = self.instances[instance]
        ratio = block.number("C_SCK_RATIO")
        clock = self.clock_hz(instance, "ext_spi_clk", fclk_hz)
        if ratio is None or ratio <= 0:
            raise ConfigError(f"{self.source}: {instance} has no C_SCK_RATIO")
        if clock is None:
            raise ConfigError(
                f"{self.source}: no frequency for {instance}/ext_spi_clk: no FCLK in "
                "ps7_init drives it and the handoff records none (an ip line can give "
                "spiClockFrequency)"
            )
        return clock / ratio


def spi_id(line: int, inputs: int = 16, mode: str = "DIRECT") -> int:
    """The shared peripheral interrupt ID that ``IRQ_F2P[line]`` raises.

    The PS takes 16 interrupts from the PL, IRQF2P[15:0]: lines 0 to 7 are IDs
    61 to 68 and lines 8 to 15 are IDs 84 to 91 (UG585 v1.7 table 7-3). The
    processing_system7 IP joins its ``IRQ_F2P`` port of ``inputs`` bits to the
    bottom of those lines with ``C_IRQ_F2P_MODE`` DIRECT, and to the top with
    REVERSE, where ``IRQ_F2P[k]`` reaches IRQF2P[16 - inputs + k] (the IP's HDL,
    processing_system7 v5.5)."""
    if not 1 <= inputs <= 16 or not 0 <= line < inputs:
        raise ConfigError(
            f"IRQ_F2P[{line}] does not exist: the port has {inputs} lines, and the PS "
            "takes at most 16 from the PL"
        )
    position = line if mode.upper() == "DIRECT" else 16 - inputs + line
    return 61 + position if position < 8 else 76 + position


def _concat_input(concat: Instance, bit: int) -> tuple[str, int] | None:
    """The input of an ``xlconcat`` that bit ``bit`` of ``dout`` comes from, and
    the bit there. ``In0`` holds the low bits."""
    count = concat.number("NUM_PORTS")
    if count is None:
        count = sum(1 for name in concat.ports if re.fullmatch(r"In\d+", name))
    offset = 0
    for index in range(count):
        name = f"In{index}"
        port = concat.ports.get(name)
        width = port.width if port is not None else concat.number(f"IN{index}_WIDTH") or 1
        if offset <= bit < offset + width:
            return name, bit - offset
        offset += width
    return None


def _driver_index(instances: Mapping[str, Instance], ports: Mapping[str, Port]) -> dict[Pin, Pin]:
    """Every pin of a net, mapped to the pin that drives the net: an output of
    an instance, or an input port of the design. The nets come from the
    ports' ``SIGNAME`` and from their connection lists."""
    parent: dict[Pin, Pin] = {}

    def find(pin: Pin) -> Pin:
        root = pin
        while parent.setdefault(root, root) != root:
            root = parent[root]
        while parent[pin] != root:
            parent[pin], pin = root, parent[pin]
        return root

    def join(a: Pin, b: Pin) -> None:
        first, second = find(a), find(b)
        if first != second:
            parent[second] = first

    named: dict[str, Pin] = {}
    every = [port for instance in instances.values() for port in instance.ports.values()]
    for port in [*every, *ports.values()]:
        pin: Pin = (port.instance, port.name)
        find(pin)
        if port.net and not port.net.startswith("__"):
            join(named.setdefault(port.net, pin), pin)
        for owner, name in port.connections:
            join(pin, (owner if owner in instances else None, name))

    def drives(pin: Pin) -> int:
        owner, name = pin
        if owner is None:
            found = ports.get(name)
            return {"I": 2, "IO": 1}.get(found.direction, 0) if found is not None else 0
        found = instances[owner].ports.get(name)
        return {"O": 2, "IO": 1}.get(found.direction, 0) if found is not None else 0

    groups: dict[Pin, list[Pin]] = {}
    for pin in list(parent):
        groups.setdefault(find(pin), []).append(pin)
    index: dict[Pin, Pin] = {}
    for members in groups.values():
        best = max(members, key=drives)
        if drives(best):
            for pin in members:
                index[pin] = best
    return index


# ---------------------------------------------------------------- reading


def read(xsa: Path) -> BlockDesign:
    """The block design in ``xsa``: the hardware handoff holding the
    ``processing_system7`` instance. An XSA without a ``.hwh`` gives a design
    with no PL."""
    xsa = Path(xsa)
    if not xsa.is_file():
        raise ConfigError(f"XSA {xsa} not found")
    try:
        with zipfile.ZipFile(xsa) as archive:
            names = archive.namelist()
            handoffs = [name for name in names if name.lower().endswith(".hwh")]
            top = _top_handoff(archive, names)
            if top is not None and top not in names:
                raise ConfigError(
                    f"XSA {xsa}: sysdef.xml names the hardware handoff {top}, "
                    "which the archive does not hold"
                )
            holding = {}
            for name in handoffs:
                data = archive.read(name)
                if _PS7_MARK.search(data):
                    holding[name] = data
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        raise ConfigError(f"XSA {xsa} is not a readable zip archive: {exc}") from None
    if not handoffs:
        return BlockDesign(source=xsa.name)
    if not holding:
        raise ConfigError(
            f"XSA {xsa}: no hardware handoff in it holds a processing_system7 instance "
            f"(it has {', '.join(handoffs)})"
        )
    if len(holding) > 1:
        if top not in holding:
            raise ConfigError(
                f"XSA {xsa}: {', '.join(holding)} each hold a processing_system7 instance, "
                "and sysdef.xml names none of them as the top block design"
            )
        holding = {str(top): holding[str(top)]}
    ((name, data),) = holding.items()
    design = parse(data, f"{xsa.name}:{name}")
    if design.ps is None:
        raise ConfigError(f"XSA {xsa}: {name} holds no processing_system7 instance")
    return design


def _top_handoff(archive: zipfile.ZipFile, names: list[str]) -> str | None:
    """The handoff ``sysdef.xml`` names for the top block design (``BD_TYPE``
    DEFAULT_BD), when the XSA has a readable ``sysdef.xml``."""
    if "sysdef.xml" not in names:
        return None
    try:
        root = ET.fromstring(archive.read("sysdef.xml"))
    except ET.ParseError:
        return None
    for entry in root.iter("File"):
        if entry.get("Type") == "HW_HANDOFF" and entry.get("BD_TYPE") == "DEFAULT_BD":
            return entry.get("Name") or None
    return None


def parse(data: bytes | str, source: str = "") -> BlockDesign:
    """A block design from the text of a hardware handoff (``.hwh``).
    ``source`` names it in errors."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ConfigError(f"{source}: the hardware handoff is not readable XML: {exc}") from None
    if root.tag != "EDKSYSTEM":
        raise ConfigError(
            f"{source}: not a hardware handoff (its root element is {root.tag}, not EDKSYSTEM)"
        )
    instances: dict[str, Instance] = {}
    for module in root.iterfind("MODULES/MODULE"):
        if module.get("IS_ENABLE", "1") == "0":
            continue
        name = _required(module, "INSTANCE", source)
        vlnv = module.get("VLNV", "")
        fields = vlnv.split(":")
        instances[name] = Instance(
            name=name,
            vlnv=vlnv,
            kind=fields[2] if len(fields) >= 3 else module.get("MODTYPE", ""),
            parameters={
                _required(p, "NAME", source): p.get("VALUE", "")
                for p in module.iterfind("PARAMETERS/PARAMETER")
            },
            ports={
                p.name: p for p in (_port(name, e, source) for e in module.iterfind("PORTS/PORT"))
            },
            interfaces={
                b.name: b
                for b in (
                    _interface(e, source) for e in module.iterfind("BUSINTERFACES/BUSINTERFACE")
                )
            },
            memory_map=tuple(
                _segment(name, e, source) for e in module.iterfind("MEMORYMAP/MEMRANGE")
            ),
        )
    info = root.find("SYSTEMINFO")
    return BlockDesign(
        source=source,
        name=info.get("NAME", "") if info is not None else "",
        instances=instances,
        ports={
            p.name: p for p in (_port(None, e, source) for e in root.iterfind("EXTERNALPORTS/PORT"))
        },
        interfaces={
            b.name: b
            for b in (
                _interface(e, source) for e in root.iterfind("EXTERNALINTERFACES/BUSINTERFACE")
            )
        },
    )


def _required(element: ET.Element, attribute: str, source: str) -> str:
    value = element.get(attribute)
    if not value:
        raise ConfigError(f"{source}: a {element.tag} element has no {attribute}")
    return value


def _number(text: str) -> int:
    """An integer as a handoff writes one: ``0x41200000``, ``0b101`` or ``64``."""
    value = text.strip().replace("_", "")
    if value[:2].lower() == "0x":
        return int(value[2:], 16)
    if value[:2].lower() == "0b":
        return int(value[2:], 2)
    return int(value, 10)


def _attribute_number(element: ET.Element, attribute: str, source: str) -> int | None:
    value = element.get(attribute)
    if value is None or not value.strip():
        return None
    try:
        return _number(value)
    except ValueError:
        owner = element.get("NAME") or element.get("INSTANCE") or ""
        raise ConfigError(
            f"{source}: {element.tag} {owner} has {attribute}={value!r}, not a number"
        ) from None


def _port(instance: str | None, element: ET.Element, source: str) -> Port:
    try:
        clock_hz = float(element.get("CLKFREQUENCY", ""))
    except ValueError:
        clock_hz = None
    return Port(
        instance=instance,
        name=_required(element, "NAME", source),
        direction=element.get("DIR", "").upper(),
        left=_attribute_number(element, "LEFT", source),
        right=_attribute_number(element, "RIGHT", source),
        signal=element.get("SIGIS", ""),
        net=element.get("SIGNAME"),
        clock_hz=clock_hz,
        connections=tuple(
            (c.get("INSTANCE", ""), c.get("PORT", ""))
            for c in element.iterfind("CONNECTIONS/CONNECTION")
        ),
    )


def _interface(element: ET.Element, source: str) -> BusInterface:
    return BusInterface(
        name=_required(element, "NAME", source),
        mode=element.get("TYPE", "").upper(),
        bus=element.get("BUSNAME", ""),
    )


def _segment(master: str, element: ET.Element, source: str) -> Segment:
    slave = _required(element, "INSTANCE", source)
    base = _attribute_number(element, "BASEVALUE", source)
    high = _attribute_number(element, "HIGHVALUE", source)
    if base is None or high is None or high < base:
        raise ConfigError(
            f"{source}: {master}'s address map gives {slave} no valid range "
            f"({element.get('BASEVALUE')}-{element.get('HIGHVALUE')})"
        )
    return Segment(
        master=master,
        interface=element.get("MASTERBUSINTERFACE", ""),
        slave=slave,
        slave_interface=element.get("SLAVEBUSINTERFACE", ""),
        block=element.get("ADDRESSBLOCK", ""),
        base=base,
        high=high,
        memory=element.get("MEMTYPE", "").upper() == "MEMORY",
    )


# -------------------------------------------------------------- ip lines


@dataclass(frozen=True)
class Memory:
    """``memory(size: ...)``: a ``Memory.MappedMemory`` of ``size`` bytes."""

    size: int


@dataclass(frozen=True)
class Cosim:
    """``cosim(...)``: Renode's ``CoSimulated.CoSimulatedPeripheral``. The
    library it runs is attached when the run starts, so the platform text holds
    only the peripheral's own properties (``frequency``, ``limitBuffer``...)."""


@dataclass(frozen=True)
class Reference:
    """A property value naming another peripheral: ``bram: axi_bram_ctrl_0``."""

    name: str


@dataclass(frozen=True)
class Literal:
    """A property value already in platform-description syntax: ``.GICv1``."""

    text: str


@dataclass(frozen=True)
class AddressRange:
    """``low .. high``, both ends included."""

    low: int
    high: int


class Span(Protocol):
    """An address range by its ends, both included: an :class:`AddressRange`,
    or a plan's ``low .. high``."""

    @property
    def low(self) -> int: ...

    @property
    def high(self) -> int: ...


PropertyValue = bool | int | float | str | Reference | Literal


@dataclass(frozen=True)
class IpOverride:
    """One ``ip`` line of a device body.

    A ``name`` that names an instance of the block design replaces that
    instance's model, at the instance's address and with its interrupts and
    reset domain; ``on`` and ``irq`` stay None. Any other name is a new block,
    which ``on`` places:

    - an exported AXI port (or an instance) by name: the model sits at the
      port's address segment;
    - a map from such names to window sizes: one window per entry, from the
      segment's base;
    - an address range (:class:`Span`): the window itself.

    An instance whose segment a window covers gets no model of its own. ``irq``
    names an exported interrupt port, or gives an ``IRQ_F2P`` line; the model's
    ``IRQ`` output (a co-simulated block's signals 0, 1, ...) drives it.
    """

    name: str
    model: str | Memory | Cosim
    """A Renode class name (``"Example.Dsp.Filter"``), :class:`Memory` or :class:`Cosim`."""
    properties: Mapping[str, PropertyValue] = field(default_factory=dict)
    """Properties and constructor arguments of the model, in Renode's units."""
    on: str | Reference | Mapping[str, int] | Span | None = None
    irq: str | Reference | int | None = None


def pl_section(
    xsa: Path, fclk_hz: Mapping[int, float], overrides: Sequence[IpOverride] = ()
) -> str:
    """Renode platform text for the programmable logic of ``xsa``.

    ``fclk_hz`` maps n to the frequency ``ps7_init`` gives ``FCLK_CLKn``, and
    ``overrides`` are the device's ``ip`` lines. The text names peripherals of
    Jig's ``xilinx/zynq7000`` platform (``gic``, ``slcr``, ``pl``, ``sysbus``),
    so it loads after that platform."""
    return render(read(Path(xsa)), fclk_hz, overrides)


def render(
    design: BlockDesign, fclk_hz: Mapping[int, float], overrides: Sequence[IpOverride] = ()
) -> str:
    """Renode platform text for ``design``, with ``overrides`` applied (see
    :func:`pl_section`)."""
    return _Writer(design, fclk_hz, overrides).text()


# --------------------------------------------------------------- writing


@dataclass
class _Entry:
    """One peripheral of the generated text."""

    name: str
    model: str
    windows: list[tuple[int, int]]
    """(base, size) of each registration."""
    point: bool = False
    """Registered at its base, the model giving its size."""
    about: list[str] = field(default_factory=list)
    """Comment lines above the entry."""
    properties: list[tuple[str, str]] = field(default_factory=list)
    outputs: dict[str, list[int]] = field(default_factory=dict)
    """GIC lines by the model's output (``IRQ``, or a co-simulated signal)."""
    init: list[str] = field(default_factory=list)
    memory: bool = False
    domain: ResetDomain | None = None
    cosim: bool = False
    generated: bool = False
    """The model comes from ``MODELS``, not from an ip line."""


@dataclass
class _Placement:
    """Where a new block sits: its windows and the slaves they cover."""

    windows: list[tuple[int, int]]
    covers: list[str]
    """Instances and exported ports under the windows, in ``on``'s order."""
    point: bool


class _Writer:
    def __init__(
        self, design: BlockDesign, fclk_hz: Mapping[int, float], overrides: Sequence[IpOverride]
    ) -> None:
        self.design = design
        self.fclk_hz = fclk_hz
        self.overrides = list(overrides)
        self.entries: list[_Entry] = []
        self.by_instance: dict[str, _Entry] = {}
        self.tags: list[tuple[int, int, str]] = []
        self.notes: list[str] = []
        # IRQ_F2P lines with the entry that drives each, and how the header
        # describes every line.
        self.taken: dict[int, str] = {}
        self.irq_text: dict[int, str] = {}
        # Window sizes ip lines give instances and exported ports.
        self.sizes: dict[str, int] = {}

    def text(self) -> str:
        replaced, placed = self._overrides()
        claimed = {name: o.name for o, placement in placed for name in placement.covers}
        self._instances(replaced, claimed)
        for override, placement in placed:
            self._place(override, placement)
        self._interrupts(replaced, placed)
        slcr = self._gpio_wiring()
        self._check_overlaps()

        entries = sorted(self.entries, key=lambda e: (e.windows[0][0], e.name))
        out = [self._header(), *(_render_entry(entry) for entry in entries)]
        if slcr:
            out.append(
                "// FCLK_RESETn_N (slcr GPIO outputs 0-3) into the AXI GPIO inputs they drive.\n"
                "slcr:\n"
                + "".join(f"    {n} -> {' | '.join(pins)}\n" for n, pins in sorted(slcr.items()))
            )
        domain = []
        for entry in entries:
            if entry.memory:
                domain.append(f"AddMemory {entry.name}")
            elif entry.domain is not None and entry.domain.fclk is not None:
                domain.append(f"AddPeripheral {entry.name} {entry.domain.fclk}")
        if domain:
            out.append(
                "// The PL reset domain: block RAM cleared with the PL; AXI slaves that\n"
                "// stall the CPU while their FCLK reset is asserted.\n"
                "pl:\n    init add:\n" + "".join(f"        {line}\n" for line in domain)
            )
        if self.tags:
            out.append(
                "// Instances without a model: their ranges read 0 and ignore writes.\n"
                "sysbus:\n    init add:\n"
                + "".join(
                    f'        Tag <0x{base:08X}, 0x{base + size - 1:08X}> "{name}" 0 false true\n'
                    for base, size, name in sorted(self.tags)
                )
            )
        return "\n".join(out)

    def _header(self) -> str:
        design = self.design
        ps = design.ps
        if ps is None and design.source:
            lines = _comment(f"{design.source} holds no block design: no PL instances.")
        elif ps is None:
            lines = _comment("No XSA: the ip lines below are the programmable logic.")
        else:
            what = design.source.replace(":", ", ", 1)
            lines = _comment(f"Programmable logic of block design {design.name} ({what}).")
        interrupts = design.interrupts()
        if interrupts and ps is not None:
            mode = ps.parameters.get("C_IRQ_F2P_MODE", "DIRECT")
            lines += _comment(
                f"IRQ_F2P ({mode}). Lines 0-7 of the PS are interrupt IDs 61-68 and lines "
                "8-15 IDs 84-91 (UG585 v1.7 table 7-3); ID N is gic@(N-32)."
            )
            for interrupt in interrupts:
                described = self.irq_text.get(interrupt.line, _describe(interrupt.source))
                text = f"[{interrupt.line}] ID {interrupt.id}: {described}"
                lines += _comment(text, indent=2, hang=4)
        known = {interrupt.line for interrupt in interrupts}
        for line in sorted(set(self.taken) - known):
            lines += _comment(f"IRQ_F2P[{line}] ID {spi_id(line)}: {self.irq_text[line]}")
        for note in self.notes:
            lines += _comment(note)
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------- overrides

    def _overrides(self) -> tuple[dict[str, IpOverride], list[tuple[IpOverride, _Placement]]]:
        design = self.design
        replaced: dict[str, IpOverride] = {}
        placed: list[tuple[IpOverride, _Placement]] = []
        for override in self.overrides:
            name = override.name
            _check_name(name, f"ip {name}")
            if name in replaced or any(o.name == name for o, _ in placed):
                raise ConfigError(f"ip {name}: two ip lines share this name")
            _check_model(override)
            if name in design.instances:
                if override.on is not None or override.irq is not None:
                    raise ConfigError(
                        f"ip {name}: {name} is an instance of {design.source}, which gives its "
                        "address and interrupts; drop on and irq, or give the block another name"
                    )
                if not self._segments(name):
                    raise ConfigError(f"ip {name}: {name} has no address on the PS's map")
                replaced[name] = override
                if isinstance(override.model, Memory):
                    self.sizes[name] = override.model.size
                continue
            if override.on is None:
                if design.ps is None:
                    holder = design.source or "a device without an xsa"
                    raise ConfigError(
                        f"ip {name}: {holder} has no block design, so the block needs on, "
                        "an address range"
                    )
                raise ConfigError(
                    f"ip {name}: {name} is not an instance of {design.source}; place it with on"
                )
            placed.append((override, self._placement(override)))
        covered: dict[str, str] = {}
        for override, placement in placed:
            for target in placement.covers:
                if target in replaced:
                    raise ConfigError(
                        f"ip {override.name}: its window covers {target}, which "
                        f"ip {target} also models"
                    )
                if target in covered:
                    raise ConfigError(
                        f"ip {override.name} and ip {covered[target]} both cover {target}"
                    )
                covered[target] = override.name
        return replaced, placed

    def _segments(self, name: str) -> list[Segment]:
        return [s for s in self.design.slaves() if s.slave == name]

    def _placement(self, override: IpOverride) -> _Placement:
        on = override.on
        model = override.model
        where = f"ip {override.name}"
        window = _range_of(on)
        if window is not None:
            low, high = window
            if high < low:
                raise ConfigError(f"{where}: the range 0x{low:X} .. 0x{high:X} is empty")
            return _Placement([(low, high - low + 1)], [], point=False)
        if isinstance(on, (str, Reference)):
            target = on.name if isinstance(on, Reference) else on
            segments = self._target(target, where)
            if isinstance(model, Memory):
                if len(segments) != 1:
                    raise ConfigError(f"{where}: {target} has {len(segments)} address segments")
                self.sizes[target] = model.size
                return _Placement([(segments[0].base, model.size)], [target], point=True)
            point = len(segments) == 1 and not isinstance(model, Cosim)
            return _Placement([(s.base, s.size) for s in segments], [target], point)
        if isinstance(on, Mapping):
            windows: list[tuple[int, int]] = []
            covers: list[str] = []
            for key, size in on.items():
                target = key.name if isinstance(key, Reference) else str(key)
                segments = self._target(target, where)
                if len(segments) != 1:
                    raise ConfigError(
                        f"{where}: {target} has {len(segments)} address segments; "
                        "place the block with an address range"
                    )
                if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                    raise ConfigError(f"{where}: the window on {target} needs a size in bytes")
                windows.append((segments[0].base, size))
                covers.append(target)
                self.sizes[target] = size
            if not windows:
                raise ConfigError(f"{where}: on names no port")
            return _Placement(windows, covers, point=False)
        raise ConfigError(
            f"{where}: on takes an exported AXI port, a map from ports to window sizes "
            f"or an address range, not {on!r}"
        )

    def _target(self, target: str, where: str) -> list[Segment]:
        design = self.design
        segments = self._segments(target)
        if segments:
            return segments
        if design.ps is None:
            holder = design.source or "a device without an xsa"
            raise ConfigError(
                f"{where}: {target} names no port or instance: {holder} has no block design, "
                "so on takes an address range and irq an IRQ_F2P line"
            )
        exported = ", ".join(design.exported_axi()) or "none"
        raise ConfigError(
            f"{where}: {target} is neither an exported AXI port nor an instance on the PS's "
            f"address map (exported AXI ports: {exported})"
        )

    # -------------------------------------------------------- instances

    def _instances(self, replaced: Mapping[str, IpOverride], claimed: Mapping[str, str]) -> None:
        design = self.design
        groups: dict[str, list[Segment]] = {}
        for segment in design.slaves():
            groups.setdefault(segment.slave, []).append(segment)
        exported = design.exported_axi()
        for name, segments in groups.items():
            ranges = ", ".join(str(s) for s in segments)
            if name in claimed:
                if name not in exported:
                    self.notes.append(f"{name} ({ranges}) is under ip {claimed[name]}.")
                continue
            if name in exported:
                self.notes.append(
                    f"{name}: exported AXI port at {ranges}; no ip line places a model there."
                )
                continue
            instance = design.instances.get(name)
            if instance is None:
                self.notes.append(f"{name} ({ranges}) is not an instance of the design.")
                continue
            override = replaced.get(name)
            if override is not None:
                self._replace(instance, segments, override)
            elif instance.ip in MODELS:
                self._model(instance, segments)
            elif instance.ip not in STRUCTURAL:
                _check_name(name, f"{design.source}: instance {name}")
                self.tags += [(s.base, s.size, name) for s in segments]
        for instance in design.instances.values():
            if instance.ip in MODELS and instance.name not in groups:
                self.notes.append(
                    f"{instance.name} ({instance.vlnv}) is not on the PS's address map: no model."
                )

    def _primary(self, name: str, segments: list[Segment], memory: bool) -> Segment:
        """The segment a model sits at; the instance's other segments become tags."""
        primary = next((s for s in segments if s.memory == memory), segments[0])
        self.tags += [(s.base, s.size, name) for s in segments if s is not primary]
        return primary

    def _model(self, instance: Instance, segments: list[Segment]) -> None:
        design = self.design
        name = instance.name
        _check_name(name, f"{design.source}: instance {name}")
        model = MODELS[instance.ip]
        memory = model == "Memory.MappedMemory"
        primary = self._primary(name, segments, memory)
        domain = design.reset_domain(name)
        entry = _Entry(
            name=name,
            model=model,
            windows=[(primary.base, primary.size)],
            point=True,
            about=[f"{name} ({instance.vlnv}) at {primary}{_domain_text(domain)}."],
            memory=memory,
            domain=domain,
            generated=True,
        )
        if instance.ip == "xilinx.com:ip:axi_gpio":
            entry.properties = _gpio_properties(instance)
        elif instance.ip == "xilinx.com:ip:axi_quad_spi":
            entry.properties = self._quad_spi_properties(instance, entry.about)
        elif instance.ip == "xilinx.com:ip:axi_cdma":
            entry.properties = [("reachableRanges", self._reach(instance))]
            if instance.number("C_INCLUDE_SG"):
                entry.about.append("Built with scatter-gather, which the model lacks.")
        else:
            entry.properties = [("size", f"0x{primary.size:X}")]
        self.entries.append(entry)
        self.by_instance[name] = entry

    def _replace(self, instance: Instance, segments: list[Segment], override: IpOverride) -> None:
        name = instance.name
        primary = self._primary(name, segments, isinstance(override.model, Memory))
        domain = self.design.reset_domain(name)
        entry = self._override_entry(override, [(primary.base, primary.size)], point=True)
        entry.about = [
            f"{name} ({instance.vlnv}) at {primary}{_domain_text(domain)}; "
            "its model comes from an ip line."
        ]
        entry.domain = domain
        self.entries.append(entry)
        self.by_instance[name] = entry

    def _place(self, override: IpOverride, placement: _Placement) -> None:
        design = self.design
        domain = None
        if placement.covers:
            first = placement.covers[0]
            domain = (
                design.reset_domain(first)
                if first in design.instances
                else design.interface_reset(first)
            )
        entry = self._override_entry(override, placement.windows, placement.point)
        where = ", ".join(placement.covers) if placement.covers else "an address range"
        entry.about = [f"{override.name}: an ip line's model on {where}{_domain_text(domain)}."]
        entry.domain = domain
        self.entries.append(entry)

    def _override_entry(
        self, override: IpOverride, windows: list[tuple[int, int]], point: bool
    ) -> _Entry:
        model = override.model
        where = f"ip {override.name}"
        properties = [
            (_property_name(key, where), _value(value, f"{where}: {key}"))
            for key, value in override.properties.items()
        ]
        if isinstance(model, Memory):
            if model.size <= 0:
                raise ConfigError(f"{where}: a memory needs a size in bytes")
            return _Entry(
                name=override.name,
                model="Memory.MappedMemory",
                windows=[(windows[0][0], model.size)],
                point=True,
                properties=[("size", f"0x{model.size:X}"), *properties],
                memory=True,
            )
        if isinstance(model, Cosim):
            return _Entry(
                name=override.name, model=COSIM, windows=windows, properties=properties, cosim=True
            )
        return _Entry(
            name=override.name, model=model, windows=windows, point=point, properties=properties
        )

    def _quad_spi_properties(self, instance: Instance, about: list[str]) -> list[tuple[str, str]]:
        design = self.design
        name = instance.name
        hz = design.spi_clock_hz(name, self.fclk_hz)
        clock = design.clock_hz(name, "ext_spi_clk", self.fclk_hz) or 0.0
        source = design.driver(name, "ext_spi_clk")
        about.append(
            f"SCK: ext_spi_clk ({_describe(source)}, {_hz(clock)}) / "
            f"C_SCK_RATIO {instance.number('C_SCK_RATIO')} = {_hz(hz)}."
        )
        if (instance.number("C_SPI_MODE") or 0) != 0 or instance.number("C_XIP_MODE"):
            about.append("Dual, quad and XIP modes are beyond the model, which runs standard SPI.")
        properties = [("spiClockFrequency", str(round(hz)))]
        depth = instance.number("C_FIFO_DEPTH")
        if depth is not None:
            # Without FIFOs (C_FIFO_DEPTH 0, PG153) one data register holds a byte.
            properties.append(("fifoDepth", str(max(depth, 1))))
        selects = instance.number("C_NUM_SS_BITS")
        if selects is not None:
            properties.append(("slaveSelectBits", str(selects)))
        return properties

    def _reach(self, instance: Instance) -> str:
        segments = self.design.reach(instance.name)
        if not segments:
            self.notes.append(f"{instance.name}: its address map is empty, so it reaches all.")
        ranges = [f"0x{s.base:08X}:0x{self.sizes.get(s.slave, s.size):X}" for s in segments]
        return '"' + ",".join(ranges) + '"'

    # ------------------------------------------------------- interrupts

    def _take(self, interrupt: Interrupt, entry: _Entry, output: str) -> None:
        line = interrupt.line
        if line in self.taken:
            raise ConfigError(
                f"IRQ_F2P[{line}] would be driven by both {self.taken[line]} and {entry.name}"
            )
        self.taken[line] = entry.name
        entry.outputs.setdefault(output, []).append(interrupt.gic)
        target = f"{entry.name} signal {output}" if entry.cosim else f"{entry.name} {output}"
        source = interrupt.source
        if source is not None and source.instance != entry.name:
            target = f"{source} -> {target}"
        self.irq_text[line] = target

    def _interrupts(
        self, replaced: Mapping[str, IpOverride], placed: list[tuple[IpOverride, _Placement]]
    ) -> None:
        design = self.design
        interrupts = design.interrupts()
        by_instance: dict[str, list[Interrupt]] = {}
        for interrupt in interrupts:
            source = interrupt.source
            if source is not None and source.instance is not None and source.constant is None:
                by_instance.setdefault(source.instance, []).append(interrupt)
        for name, lines in by_instance.items():
            entry = self.by_instance.get(name)
            if entry is None or entry.memory:
                for interrupt in lines:
                    self.irq_text[interrupt.line] = f"{_describe(interrupt.source)}, no model"
                continue
            if entry.cosim:
                for index, interrupt in enumerate(lines):
                    self._take(interrupt, entry, str(index))
                continue
            instance = design.instances[name]
            expected = _INTERRUPT_PORTS.get(instance.ip) if name not in replaced else None
            ports = {i.source.port for i in lines if i.source is not None}
            for interrupt in lines:
                port = interrupt.source.port if interrupt.source is not None else ""
                if port == expected or (expected is None and len(ports) == 1):
                    self._take(interrupt, entry, "IRQ")
                else:
                    self.irq_text[interrupt.line] = (
                        f"{_describe(interrupt.source)}, not connected: the model of {name} "
                        "has one IRQ output"
                    )
        exported = design.exported_interrupts()
        for override, _ in placed:
            if override.irq is None:
                continue
            entry = next(e for e in self.entries if e.name == override.name)
            if entry.memory:
                raise ConfigError(f"ip {override.name}: a memory has no interrupt output")
            for index, interrupt in enumerate(self._irq_lines(override, exported)):
                self._take(interrupt, entry, str(index) if entry.cosim else "IRQ")
        for interrupt in interrupts:
            source = interrupt.source
            if interrupt.line not in self.irq_text and source is not None and not source.instance:
                self.irq_text[interrupt.line] = f"{source}, a port of the design; no ip line"
        for entry in self.entries:
            if entry.cosim and entry.outputs:
                signals = len(entry.outputs)
                entry.properties.append(("cosimToRenodeSignalRange", f"<0, +{signals}>"))

    def _irq_lines(
        self, override: IpOverride, exported: Mapping[str, list[Interrupt]]
    ) -> list[Interrupt]:
        design = self.design
        irq = override.irq
        where = f"ip {override.name}"
        if isinstance(irq, int) and not isinstance(irq, bool):
            interrupts = design.interrupts()
            if not interrupts:
                return [Interrupt(irq, spi_id(irq), None)]
            if not 0 <= irq < len(interrupts):
                raise ConfigError(
                    f"{where}: IRQ_F2P[{irq}] does not exist (the PS's IRQ_F2P has "
                    f"{len(interrupts)} lines)"
                )
            return [interrupts[irq]]
        name = irq.name if isinstance(irq, Reference) else irq
        if not isinstance(name, str):
            raise ConfigError(f"{where}: irq takes an interrupt port or an IRQ_F2P line")
        lines = exported.get(name)
        if lines is None:
            known = ", ".join(exported) or "none"
            raise ConfigError(
                f"{where}: {name} is not a port of the design that reaches IRQ_F2P "
                f"(such ports: {known})"
            )
        if len(lines) > 1 and not isinstance(override.model, Cosim):
            raise ConfigError(
                f"{where}: {name} reaches {len(lines)} IRQ_F2P lines and the model has one "
                "IRQ output"
            )
        return lines

    # ------------------------------------------------------------- GPIO

    def _gpio_wiring(self) -> dict[int, list[str]]:
        """Constant AXI GPIO inputs set in the model, and the SLCR outputs that
        drive the others; returns the pins each FCLK reset drives."""
        design = self.design
        slcr: dict[int, list[str]] = {}
        for name, entry in self.by_instance.items():
            if design.instances[name].ip != "xilinx.com:ip:axi_gpio":
                continue
            inputs = design.gpio_inputs(name)
            if entry.generated:
                for number, signal in inputs.items():
                    fclk = design.fclk_reset(signal)
                    if signal is not None and signal.constant == 1:
                        entry.init.append(f"SetInput {1 + number // 32} {number % 32} true")
                    elif fclk is not None:
                        slcr.setdefault(fclk, []).append(f"{name}@{number}")
            runs = _input_runs(inputs, design)
            if runs:
                entry.about += ["Inputs:", *(f"  {run}" for run in runs)]
        return slcr

    def _check_overlaps(self) -> None:
        spans = [(base, size, e.name) for e in self.entries for base, size in e.windows]
        spans = sorted([*spans, *self.tags])
        for (base, size, name), (following, _, other) in itertools.pairwise(spans):
            if following < base + size:
                raise ConfigError(
                    f"{name} (0x{base:08X}-0x{base + size - 1:08X}) overlaps {other} "
                    f"(from 0x{following:08X})"
                )


# ----------------------------------------------------------------- helpers


def _range_of(value: object) -> tuple[int, int] | None:
    """(low, high) of an address range: an :class:`AddressRange`, or any object
    with integer ``low`` and ``high`` (a plan's ``a .. b``)."""
    if isinstance(value, (str, Mapping)):
        return None
    low = getattr(value, "low", None)
    high = getattr(value, "high", None)
    if isinstance(low, int) and isinstance(high, int):
        return low, high
    return None


def _check_name(name: str, where: str) -> None:
    if not _IDENTIFIER.fullmatch(name):
        raise ConfigError(f"{where}: {name!r} is not a name Renode accepts (letters, digits, _)")
    if name in platform_names():
        raise ConfigError(f"{where}: {name} is a peripheral of the Zynq-7000 platform")


@functools.cache
def platform_names() -> frozenset[str]:
    """The peripherals Jig's ``xilinx/zynq7000`` platform declares."""
    from jig import project

    text = project.platform_file("xilinx/zynq7000").read_text(encoding="utf-8")
    return frozenset(re.findall(r"^([A-Za-z_]\w*):", text, flags=re.MULTILINE)) | {"sysbus"}


def _check_model(override: IpOverride) -> None:
    model = override.model
    if isinstance(model, (Memory, Cosim)):
        return
    if not isinstance(model, str) or not _TYPE_NAME.fullmatch(model):
        raise ConfigError(
            f"ip {override.name}: the model {model!r} is not a Renode class name, "
            "memory(...) or cosim(...)"
        )


def _property_name(name: object, where: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise ConfigError(f"{where}: {name!r} is not a property name")
    return name


def _value(value: object, where: str) -> str:
    """A property value in platform-description syntax."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        if '"' in value or "\n" in value:
            raise ConfigError(f"{where}: a platform description cannot hold {value!r}")
        return f'"{value}"'
    if isinstance(value, Reference):
        if not _IDENTIFIER.fullmatch(value.name):
            raise ConfigError(f"{where}: {value.name!r} is not a peripheral name")
        return value.name
    if isinstance(value, Literal):
        return value.text
    raise ConfigError(f"{where}: {value!r} has no platform-description form")


def _gpio_properties(instance: Instance) -> list[tuple[str, str]]:
    """The power-on state of an AXI GPIO (PG144: C_DOUT_DEFAULT and C_TRI_DEFAULT
    per channel, C_IS_DUAL), as the model's constructor takes it."""
    properties = []
    for parameter, name in (
        ("C_DOUT_DEFAULT", "dataDefault1"),
        ("C_TRI_DEFAULT", "triDefault1"),
        ("C_DOUT_DEFAULT_2", "dataDefault2"),
        ("C_TRI_DEFAULT_2", "triDefault2"),
    ):
        value = instance.number(parameter)
        if value is not None:
            properties.append((name, f"0x{value & 0xFFFFFFFF:08X}"))
    dual = instance.number("C_IS_DUAL")
    if dual is not None:
        properties.append(("dualChannel", "true" if dual else "false"))
    return properties


def _domain_text(domain: ResetDomain | None) -> str:
    if domain is None:
        return ""
    if domain.fclk is None:
        return f", reset by {domain.block or 'a source'} from outside the PS"
    through = f" through {domain.block}" if domain.block else ""
    return f", reset by FCLK_RESET{domain.fclk}_N{through}"


def _describe(signal: Signal | None) -> str:
    return "not connected" if signal is None else str(signal)


def _hz(value: float) -> str:
    for unit, scale in (("MHz", 1e6), ("kHz", 1e3)):
        if value >= scale:
            return f"{value / scale:g} {unit}"
    return f"{value:g} Hz"


def _input_runs(inputs: Mapping[int, Signal | None], design: BlockDesign) -> list[str]:
    """The inputs of an AXI GPIO described in runs that share a source."""
    runs: list[list[int]] = []
    for number in sorted(inputs):
        if runs and number == runs[-1][-1] + 1 and _follows(inputs[runs[-1][-1]], inputs[number]):
            runs[-1].append(number)
        else:
            runs.append([number])
    texts = []
    for run in runs:
        first, last = run[0], run[-1]
        span = f"[{first}]" if first == last else f"[{last}:{first}]"
        texts.append(f"{span} {_input_text(inputs[first], inputs[last], design)}")
    return texts


def _follows(previous: Signal | None, signal: Signal | None) -> bool:
    if previous is None or signal is None:
        return previous is None and signal is None
    same = (previous.instance, previous.port, previous.constant)
    return same == (signal.instance, signal.port, signal.constant) and (
        signal.bit == previous.bit + 1
    )


def _input_text(low: Signal | None, high: Signal | None, design: BlockDesign) -> str:
    if low is None or high is None:
        return "not connected"
    if low.constant is not None:
        return f"constant {low.constant}"
    fclk = design.fclk_reset(low)
    if fclk is not None:
        through = (
            ""
            if low.instance is None or design.instances[low.instance].kind == PS7
            else (f" through {low.instance}")
        )
        return f"FCLK_RESET{fclk}_N{through}"
    bits = ""
    if low.width > 1:
        bits = f"[{low.bit}]" if low.bit == high.bit else f"[{high.bit}:{low.bit}]"
    if low.instance is None:
        return f"{low.port}{bits}, a port of the design"
    return f"{low.instance}/{low.port}{bits}, not wired"


def _comment(text: str, indent: int = 0, hang: int = 0) -> list[str]:
    """``text`` as ``//`` lines of 79 columns at most, the first indented by
    ``indent`` spaces and the rest by ``indent + hang``."""
    lines = textwrap.wrap(
        text,
        width=76,
        initial_indent=" " * indent,
        subsequent_indent=" " * (indent + hang),
        break_long_words=False,
        break_on_hyphens=False,
    )
    return [f"// {line}" for line in lines]


def _render_entry(entry: _Entry) -> str:
    lines = []
    for line in entry.about:
        indent = len(line) - len(line.lstrip(" "))
        lines += _comment(line.lstrip(" "), indent=indent, hang=4 if indent else 0)
    if entry.point:
        where = f"sysbus 0x{entry.windows[0][0]:08X}"
    elif len(entry.windows) == 1:
        base, size = entry.windows[0]
        where = f"sysbus <0x{base:08X}, +0x{size:X}>"
    else:
        registrations = ";\n".join(
            f"        sysbus <0x{base:08X}, +0x{size:X}>" for base, size in entry.windows
        )
        where = "{\n" + registrations + "\n    }"
    lines.append(f"{entry.name}: {entry.model} @ {where}")
    lines += [f"    {key}: {value}" for key, value in entry.properties]
    lines += [
        f"    {output} -> {' | '.join(f'gic@{line}' for line in gic)}"
        for output, gic in entry.outputs.items()
    ]
    if entry.init:
        lines += ["    init:", *(f"        {command}" for command in entry.init)]
    return "\n".join(lines) + "\n"
