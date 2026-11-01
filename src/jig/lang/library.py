"""What a plan can reach: built-in functions and handles on the system.

Plans never touch the simulation directly. A system declaration creates
handles (``BoardHandle`` for a board, ``BusHandle`` for a CAN link,
``ConsoleHandle`` for a UART link, and one handle per node from the node kind
that declared it) and each handle exposes a fixed set of members, listed in
its ``dsl_members``, plus the members extensions and board plugins register
(``jig.plugin``). Anything else is not reachable from a plan. Methods that
wait are coroutines on the simulation's scheduler, every duration is in
seconds of simulated time.
"""

from __future__ import annotations

import math
import random
import re
import zlib
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jig import project
from jig.errors import ConfigError, JigError
from jig.lang.values import Record, Regex, as_pattern, show, type_name
from jig.runtime import environment
from jig.runtime.environment import Background
from jig.runtime.scheduler import seconds_to_ns

if TYPE_CHECKING:
    from jig.runtime.scheduler import Scheduler
    from jig.system.boards import Board
    from jig.system.model import Link
    from jig.system.socs import MachineConfig
    from jig.system.system import Console, Machine, Simulation
    from jig.transports.can import CanBusPort
    from jig.transports.uart import UartPort


class DslError(JigError):
    """A library call used wrongly from a plan (bad argument, unknown name)."""


def as_seconds(value: Any, what: str) -> float:
    """A non-negative duration in seconds, or DslError naming ``what``."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise DslError(f"{what} must be a non-negative duration, not {show(value)}")
    return float(value)


def as_bytes(value: Any, what: str) -> bytes:
    """Bytes, or text as Latin-1 bytes, or DslError naming ``what``."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("latin-1")
    raise DslError(f"{what} must be bytes or text, not {type_name(value)}")


def _time_of(record: Any, what: str) -> float:
    time = record.get("time") if isinstance(record, Mapping) else None
    if not isinstance(time, (int, float)) or isinstance(time, bool):
        raise DslError(f"{what}: every record needs a time, not {show(record)[:60]}")
    return float(time)


def _field(record: Any, name: str) -> Any:
    """A field of a record or of one of its groups (reply.header.length)."""
    if isinstance(record, Mapping):
        if name in record:
            return record[name]
        for value in record.values():
            if isinstance(value, Mapping):
                found = _field(value, name)
                if found is not None:
                    return found
    return None


# ------------------------------------------------------------------- matches


class Match:
    """A regular-expression match: ``m.text``, ``m[1]``, ``m.number(1)``."""

    dsl_members = frozenset({"text", "group", "groups", "number", "start", "end"})

    def __init__(self, match: re.Match[str]) -> None:
        self._match = match

    @property
    def text(self) -> str:
        return self._match.group(0)

    def group(self, index: int | str = 0) -> str | None:
        return self._match.group(index)

    @property
    def groups(self) -> list[str | None]:
        return list(self._match.groups())

    def number(self, index: int | str = 1) -> int | float:
        text = self._match.group(index)
        if text is None:
            raise DslError(f"group {index} of {self._match.re.pattern!r} matched nothing")
        try:
            return int(text, 0)
        except ValueError:
            return float(text)

    @property
    def start(self) -> int:
        return self._match.start()

    @property
    def end(self) -> int:
        return self._match.end()

    def __getitem__(self, index: int | str) -> str | None:
        return self._match.group(index)

    def __repr__(self) -> str:
        return f"match {self.text!r}"


# -------------------------------------------------------------------- handles


@dataclass(frozen=True)
class BoardLinkRef:
    """A board's link named in a system declaration (``dut.kiss``): the
    machine, the link's name on the board, its kind (``uart``, ``can``, ...),
    whether it leaves the board, and the device interfaces a UART or CAN
    link joins, as the board file names them (``("zynq.uart0",)``)."""

    machine: str
    name: str
    kind: str
    external: bool = True
    interfaces: tuple[str, ...] = ()

    dsl_members = frozenset({"machine", "name", "kind", "external"})

    def __repr__(self) -> str:
        return f"{self.machine}.{self.name}"

    @property
    def ref(self) -> BoardLinkRef:
        return self

    def dsl_attribute(self, name: str) -> Any:
        """A plan reaches a board's link only through the system link on it."""
        if not self.external:
            raise DslError(
                f"{self!r} is a link inside board {self.machine}: a plan reaches it through "
                "a system link on it, and those probes come in a later release"
            )
        raise DslError(
            f"{self!r} joins no system link, so the harness is not on it: declare one, "
            f"such as link {self.name}0: {self.kind}(members: [{self!r}])"
        )


def _link_ref(machine: str, link: Link) -> BoardLinkRef:
    """The reference to ``link``, a link of the board ``machine`` runs."""
    ends = tuple(f"{end.device}.{end.name}" for end in link.members)
    return BoardLinkRef(machine, link.name, link.kind, link.external, ends)


class ConsoleHandle:
    """The harness's end of a UART: what the board sent as text, and lines
    sent to it."""

    dsl_members = frozenset(
        {"label", "text", "buffer", "send", "write", "cli", "expect", "seen", "clear", "drain"}
    )

    def __init__(self, console: Console) -> None:
        self._source: Callable[[], Console] = lambda: console

    def __repr__(self) -> str:
        return f"<console {self._console.label}>"

    @property
    def _console(self) -> Console:
        return self._source()

    @property
    def label(self) -> str:
        return self._console.label

    @property
    def text(self) -> str:
        """Everything the console has received."""
        return self._console.text

    @property
    def buffer(self) -> str:
        """What arrived since the last clear (or cli)."""
        return self._console.buffer

    def send(self, line: str) -> None:
        """Send one command line, ended the way the interface's ``eol`` says."""
        self._console.send_line(line)

    def write(self, text: str) -> None:
        """Send characters as they are."""
        self._console.write(text)

    async def cli(self, command: str, settle: float = 0.3) -> str:
        """Send a command and return what the board printed until it went quiet."""
        return await self._console.run(command, settle=as_seconds(settle, "settle"))

    async def expect(self, pattern: Any, within: float = 10.0) -> Match:
        """Wait for ``pattern`` (a /regex/ or text) in what arrived since the last clear."""
        found = await self._console.expect(as_pattern(pattern), as_seconds(within, "within"))
        return Match(found)

    def seen(self, pattern: Any) -> Match | None:
        found = self._console.search(as_pattern(pattern))
        return Match(found) if found else None

    def clear(self) -> None:
        self._console.clear()

    async def drain(self, settle: float = 0.5) -> str:
        return await self._console.drain(as_seconds(settle, "settle"))


class UartLinkHandle(ConsoleHandle):
    """A board's external UART link (``a.console``, ``a.kiss``): the console
    members on the harness's end of it, which records what the board sends
    from the start of the run."""

    def __init__(self, machine: Machine, link: str) -> None:
        self._machine = machine
        self._link = link
        self._source = lambda: machine.console_on(link)
        self.ref = _link_ref(machine.name, machine.board.link(link))

    def __repr__(self) -> str:
        return f"<link {self._machine.name}.{self._link}>"


class TerminalLinkHandle(UartLinkHandle):
    """An external UART link whose interface's stack has ``vt100``: the
    console members work on the text as a terminal shows it, and three more
    reach past the rendering, ``press``, ``raw`` and ``raw_buffer``."""

    dsl_members = UartLinkHandle.dsl_members | {"press", "raw", "raw_buffer"}

    def press(self, key: Any) -> None:
        """Send the bytes a terminal sends for a named key: ``up``, ``down``,
        ``right``, ``left``, ``home``, ``end``, ``insert``, ``delete``,
        ``page_up``, ``page_down``, ``tab``, ``enter``, ``backspace``,
        ``escape``, ``ctrl-a`` to ``ctrl-z``."""
        if not isinstance(key, str):
            raise DslError(f'press takes the name of a key, such as "up", not {type_name(key)}')
        try:
            self._console.press(key)
        except ValueError as exc:
            raise DslError(f"press: {exc}") from None

    @property
    def raw(self) -> bytes:
        """The bytes the board sent since the run started, as it sent them."""
        return bytes(self._console.raw)

    @property
    def raw_buffer(self) -> bytes:
        """The bytes the board sent since the last ``clear()`` (or ``cli``),
        as it sent them."""
        return bytes(self._console.raw_buffer)


class BackgroundHandle:
    """Work a plan started that runs alongside it (``board.drive``)."""

    dsl_members = frozenset({"name", "running", "updates", "stop"})

    def __init__(self, entry: Background) -> None:
        self._entry = entry

    def __repr__(self) -> str:
        state = "running" if self._entry.running else "stopped"
        return f"<background {self._entry.name} {state}>"

    @property
    def name(self) -> str:
        return self._entry.name

    @property
    def running(self) -> bool:
        return self._entry.running

    @property
    def updates(self) -> int:
        return self._entry.updates

    def stop(self) -> None:
        self._entry.stop()


BackgroundBody = Callable[[Background], Coroutine[Any, Any, None]]


def start_background(simulation: Any, name: str, body: BackgroundBody) -> BackgroundHandle:
    """Run ``body`` beside the plan, it belongs to the block that started it."""
    entry = simulation.background.start(simulation.scheduler, name, body)
    return BackgroundHandle(entry)


def periodic(
    scheduler: Scheduler,
    action: Callable[[], Any],
    every: float,
    start_ns: int | None = None,
) -> BackgroundBody:
    """A background body that calls ``action`` at ``start_ns`` (default: when
    it starts) and every ``every`` seconds after, on a fixed grid, awaiting it
    when it is a coroutine."""
    period_ns = seconds_to_ns(every)
    if period_ns <= 0:
        raise DslError("a periodic service needs a positive period")
    first_ns = start_ns

    async def body(entry: Background) -> None:
        start_ns = scheduler.now_ns if first_ns is None else first_ns
        slot = 0
        while True:
            result = action()
            if isinstance(result, Coroutine):
                await result
            entry.updates += 1
            slot += 1
            await scheduler.sleep_until_ns(start_ns + slot * period_ns)

    return body


class DeviceHandle:
    """A device of a board (``a.zynq``): its name, its family and its
    machine's monitor."""

    dsl_members = frozenset({"name", "family", "monitor"})

    def __init__(self, board: BoardHandle, name: str, family: str) -> None:
        self.board = board
        self._name = name
        self._family = family

    def __repr__(self) -> str:
        return f"<device {self.board.name}.{self._name} ({self._family})>"

    @property
    def name(self) -> str:
        return self._name

    @property
    def family(self) -> str:
        return self._family

    def monitor(self, command: str) -> str:
        """A Renode monitor command in this device's machine."""
        return self.board.monitor(command)


class PeripheralHandle:
    """A peripheral of a board (``a.flash0``): its name and model."""

    dsl_members = frozenset({"name", "model"})

    def __init__(self, board: BoardHandle, name: str, model: str) -> None:
        self.board = board
        self._name = name
        self._model = model

    def __repr__(self) -> str:
        return f"<peripheral {self.board.name}.{self._name} ({self._model})>"

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model


def name_collisions(board: Board, members: Iterable[str]) -> list[str]:
    """The board's devices, peripherals and links whose names a board
    handle already uses for a member (``reset``, a plugin's ``uptime``)."""
    taken = set(BoardHandle.dsl_members) | set(members)
    found = []
    for kind, names in (
        ("device", [device.name for device in board.devices]),
        ("peripheral", [part.name for part in board.peripherals]),
        ("link", [link.name for link in board.links]),
    ):
        for name in names:
            if name in taken:
                found.append(
                    f"board {board.name}: {kind} {name} has the name of the board member "
                    f"{name}; rename the {kind}"
                )
    return found


class BoardHandle:
    """One emulated board.

    Besides the plan members in ``dsl_members``, a plan reaches the board's
    devices, peripherals and links by name (``a.zynq``, ``a.flash0``,
    ``a.console``). Plugins read ``definition`` (the board), ``config`` (the
    machine's settings), ``root``, ``workdir`` and ``console``, and call
    ``emit(kind, detail)``."""

    dsl_kind = "board"
    dsl_members = frozenset(
        {
            "name",
            "board",
            "boot_mode",
            "boot",
            "cli",
            "send",
            "expect",
            "set_sensor",
            "sensor_code",
            "sensors",
            "drive",
            "set_time",
            "time",
            "reset",
            "power_on",
            "power_off",
            "power_cycle",
            "powered",
            "image",
            "monitor",
        }
    )

    def __init__(self, machine: Machine) -> None:
        self._machine = machine
        self._links: dict[str, Any] = {}  # link name -> its handle, once made

    def __repr__(self) -> str:
        return f"<board {self._machine.name} ({self._machine.board.name})>"

    # ------------------------------------------------- what plugins read

    @property
    def machine(self) -> Machine:
        return self._machine

    @property
    def definition(self) -> Board:
        """The board this machine was built from, for its option values."""
        return self._machine.board

    @property
    def config(self) -> MachineConfig:
        """The machine's settings: boot mode, options, storage images, work directory."""
        return self._machine.config

    @property
    def root(self) -> Path:
        """The board's root directory (its ``root`` setting)."""
        return self._machine.board.root

    @property
    def workdir(self) -> Path:
        """This machine's directory in the run directory."""
        return self._machine.config.workdir

    def emit(self, kind: str, detail: str = "") -> None:
        """Record an event of this board in the run's event stream."""
        self._machine.simulation.emit(self._machine.name, str(kind), str(detail))

    @property
    def scheduler(self) -> Scheduler:
        return self._machine.simulation.scheduler

    def start_background(self, name: str, body: BackgroundBody) -> BackgroundHandle:
        return start_background(self._machine.simulation, name, body)

    # ------------------------------------------------------- plan members

    @property
    def name(self) -> str:
        return self._machine.name

    @property
    def board(self) -> str:
        return self._machine.board.name

    @property
    def boot_mode(self) -> str:
        """The boot mode the board runs (``jtag``, ``qspi``, ``flash``, ...)."""
        return self._machine.config.boot

    @property
    def console(self) -> ConsoleHandle:
        """The board's external link named ``console``."""
        found: ConsoleHandle = self.link("console")
        return found

    @property
    def sensors(self) -> list[str]:
        return [sensor.name for sensor in self._machine.board.sensors]

    def link(self, name: str) -> Any:
        """The board's link ``name``: an external UART link's console members
        (``UartLinkHandle``); for a CAN link, the handle of the system link
        it joined; else a ``BoardLinkRef`` (a CAN link that joins no system
        link, a link inside the board)."""
        found = self._links.get(name)
        if found is not None:
            return found
        spec = self._machine.board.link(name)
        if spec.external and spec.kind == "uart":
            terminal = "vt100" in self._machine.board.interface(spec.name).stack
            kind = TerminalLinkHandle if terminal else UartLinkHandle
            found = kind(self._machine, spec.name)
        else:
            found = _link_ref(self._machine.name, spec)
        self._links[name] = found
        return found

    def attach_link(self, name: str, handle: Any) -> None:
        """What the board's link ``name`` stands for in plans from now on: the
        system link that joined it."""
        self._links[name] = handle

    def dsl_attribute(self, name: str) -> Any:
        """``dut.kiss``, ``dut.zynq``, ``dut.flash0``: a link, a device or a
        peripheral of the board by name."""
        board = self._machine.board
        if any(link.name == name for link in board.links):
            return self.link(name)
        for device in board.devices:
            if device.name == name:
                return DeviceHandle(self, device.name, device.family)
        for part in board.peripherals:
            if part.name == name:
                return PeripheralHandle(self, part.name, part.model)
        return None

    @property
    def cli_console(self) -> ConsoleHandle:
        """The console of the board's one interface whose stack has ``cli``,
        which ``cli``, ``send`` and ``expect`` use."""
        definition = self._machine.board
        links = definition.cli_links()
        if len(links) == 1:
            found: ConsoleHandle = self.link(links[0])
            return found
        if not links:
            raise DslError(
                f"board {self.name} ({definition.name}) runs cli on none of its external UART "
                "links: give the console's interface a cli layer, such as interface uart1: cli"
            )
        listed = ", ".join(links)
        raise DslError(
            f"board {self.name} runs cli on {listed}: name the link, such as "
            f"{self.name}.{links[0]}.cli(...)"
        )

    async def cli(self, command: str, settle: float = 0.3) -> str:
        return await self.cli_console.cli(command, settle)

    def send(self, line: str) -> None:
        self.cli_console.send(line)

    async def expect(self, pattern: Any, within: float = 10.0) -> Match:
        return await self.cli_console.expect(pattern, within)

    def set_sensor(self, name: str, volts: float) -> int | None:
        """Drive a sensor input (volts at the converter pin), returns the code it
        reads (none for a converter that reports no codes to the harness)."""
        if not isinstance(volts, (int, float)):
            raise DslError(f"set_sensor: volts must be a number, not {type_name(volts)}")
        return self._machine.set_sensor(name, float(volts))

    def sensor_code(self, name: str, volts: Any = None) -> int:
        """The code the sensor reports now, or would report for ``volts``."""
        if volts is None:
            return self._machine.sensor_code(name)
        if not isinstance(volts, (int, float)) or isinstance(volts, bool):
            raise DslError(f"sensor_code: volts is a number, not {type_name(volts)}")
        return self._machine.sensor_code(name, float(volts))

    def drive(self, sensor: str, profile: Any, every: float = 1.0) -> BackgroundHandle:
        """Set a sensor input from ``profile`` (volts against the seconds since
        this call) every ``every`` seconds, in the background, until stopped or
        until the block that called it ends."""
        name = str(sensor)
        self._machine.board.sensor(name)  # an unknown sensor fails here, not later
        try:
            shape = environment.as_profile(profile)
        except ConfigError as exc:
            raise DslError(f"drive: {exc}") from None
        period = as_seconds(every, "every")
        if period <= 0:
            raise DslError("drive: every must be a positive duration")
        machine = self._machine
        scheduler = machine.simulation.scheduler
        start_ns = scheduler.now_ns
        body = periodic(
            scheduler,
            lambda: machine.set_sensor(name, shape.at((scheduler.now_ns - start_ns) / 1e9)),
            period,
            start_ns,
        )
        return self.start_background(f"{self.name}.drive({name})", body)

    def set_time(self, iso: str) -> None:
        """Load the board's real-time clock with a calendar time such as
        2027-03-21T06:00:00. The clock has no time zone: it counts on from
        what it is given."""
        self._machine.set_time(str(iso))

    def time(self) -> str:
        """The board's real-time clock as its counters read, 2027-03-21T06:00:02.00."""
        return self._machine.rtc_time()

    def reset(self) -> None:
        """Pulse the board's system-reset input."""
        self._machine.reset()

    def power_on(self) -> None:
        """Apply power: a board that never booted runs its boot flow, one that
        was powered off restarts through reset. A board whose boot mode needs
        a debugger then waits for ``boot()``."""
        self._machine.power_on()

    def power_off(self) -> None:
        """Remove power: the cores stop until ``power_on``."""
        self._machine.power_off()

    @property
    def powered(self) -> bool:
        return self._machine.powered

    async def power_cycle(self, off: float = 1.0) -> None:
        """Remove power for ``off`` seconds, then restore it, as ``power_off``
        then ``power_on`` do."""
        await self._machine.power_cycle(as_seconds(off, "off"))

    def boot(self) -> None:
        """Run the board's boot flow again, the one its boot mode names and
        the run started it with.

        For a Zynq-7000 in ``jtag`` mode that is what the debugger does: the
        hardware design's register writes and bitstream when the board has
        them, then the application ELF loaded and core 0 started at its entry.
        In ``qspi`` mode it is the BootROM's boot from flash; for an STM32 in
        ``flash`` mode, the ELF loaded into flash and the core started from
        the vector table.

        A board in JTAG boot mode needs it after ``power_cycle()``, or after
        ``power_on()`` that follows ``power_off()``: like the hardware, it
        comes back in its BootROM, waiting for a debugger, and prints nothing
        until ``boot()``. A board that boots from its own storage is running
        again after power-on; ``boot()`` then runs the flow once more from
        that state. So ``power_cycle()`` then ``boot()`` restarts a board the
        way the run started it, whatever its boot mode.

        Nothing resets the board first, and a powered-off board is an error
        (``power_on()`` first)."""
        self._machine.boot()

    def image(self, role: str) -> bytes:
        """A firmware image as an over-the-air update sends it (app, bitstream, fsbl)."""
        images = self._machine.driver.images(self._machine.config)
        if role not in images:
            raise DslError(f"{self.name} has no {role!r} image (has: {', '.join(images)})")
        return images[role].read_bytes()

    def monitor(self, command: str) -> str:
        """A Renode monitor command in this board's context (for diagnostics)."""
        return self._machine.execute(command)


@dataclass
class BusSpec:
    """A system CAN link: its bit rate and the boards' links it joins."""

    bitrate: float
    members: list[BoardLinkRef] = field(default_factory=list)


@dataclass
class LinkSpec:
    """``uart(...)``, ``can(...)`` and the other link kinds in a system: the
    settings it states (``baud``, ``bitrate``) and its members, boards'
    links and nodes."""

    kind: str
    settings: dict[str, Any] = field(default_factory=dict)
    members: list[Any] = field(default_factory=list)


class BusHandle:
    """A CAN link the plan declared (``link can0: can(...)``), and every
    board's CAN link it joins. Plugins reach what each bus observer made of
    its traffic through ``observer(name)``."""

    dsl_kind = "bus"
    dsl_members = frozenset({"name", "bitrate", "frames", "acknowledge"})

    def __init__(self, name: str, spec: BusSpec, simulation: Simulation) -> None:
        self._name = name
        self._spec = spec
        self._simulation = simulation

    def __repr__(self) -> str:
        return f"<bus {self._name}>"

    @property
    def name(self) -> str:
        return self._name

    @property
    def bitrate(self) -> float:
        return self._spec.bitrate

    @property
    def frames(self) -> int:
        """Frames seen on the bus so far."""
        return self._simulation.can_buses[self._name].frame_count

    def acknowledge(self, enabled: bool) -> None:
        """Whether the harness's adapter acknowledges frames (it does by default)."""
        self._simulation.can_buses[self._name].set_acknowledging(bool(enabled))

    def observer(self, name: str) -> Any:
        """The bus observer ``name`` (``api.bus_observer``) made for this bus."""
        observers = self._simulation.bus_observers.get(self._name, {})
        if name not in observers:
            raise DslError(
                f"bus {self._name} has no {name!r} observer (has: {', '.join(observers)})"
            )
        return observers[name]

    @property
    def port(self) -> CanBusPort:
        return self._simulation.can_buses[self._name]

    @property
    def scheduler(self) -> Scheduler:
        return self._simulation.scheduler

    def start_background(self, name: str, body: BackgroundBody) -> BackgroundHandle:
        return start_background(self._simulation, name, body)


class SystemContext:
    """The system a plan declared, as node kinds and plugins see it: the
    scheduler, the seed, the boards and buses by name, the ports a node can
    attach to, and the event stream."""

    def __init__(self, simulation: Simulation) -> None:
        self.simulation = simulation
        self.boards: dict[str, BoardHandle] = {}
        self.buses: dict[str, BusHandle] = {}
        self.nodes: dict[str, Any] = {}

    @property
    def scheduler(self) -> Scheduler:
        return self.simulation.scheduler

    @property
    def seed(self) -> int:
        return int(getattr(self.simulation, "seed", 0))

    @property
    def now(self) -> float:
        return float(self.simulation.now)

    def emit(self, source: str, kind: str, detail: str = "") -> None:
        emit = getattr(self.simulation, "emit", None)
        if emit is not None:
            emit(source, kind, detail)

    def uart(self, link: BoardLinkRef) -> UartPort:
        """The harness's end of a board's UART link (once the simulation started)."""
        return self.simulation.machines[link.machine].port(link.name)

    def can_bus(self, name: str) -> CanBusPort:
        """The harness's adapter on a system CAN link (once the simulation started)."""
        if name not in self.simulation.can_buses:
            raise ConfigError(f"no CAN link {name!r}")
        return self.simulation.can_buses[name]

    def start_background(self, name: str, body: BackgroundBody) -> BackgroundHandle:
        return start_background(self.simulation, name, body)


@dataclass(frozen=True)
class NodeLink:
    """What Jig hands a node when the system starts: the system link it
    joined (its name and kind, ``uart`` or ``can``), the harness's port on
    that link (a ``UartPort`` or a ``CanBusPort``), the layers of its stack
    below the node, top first (``kiss`` in ``csp(...) over kiss``), and the
    boards' links the system link joins, in the order its members name them
    (``BoardLinkRef``: the machine, the link's name on the board and the
    device interfaces it joins)."""

    name: str
    kind: str
    port: Any
    layers: tuple[Any, ...] = ()
    board_links: tuple[BoardLinkRef, ...] = ()


class NodeDeclaration:
    """What a node kind's constructor returns (``node gs: csp(address: 10)``).

    ``runs_on`` names what the node needs from what is below it: ``packets``
    for a node that needs ``kiss`` or ``cfp`` under it, ``CAN frames`` for one
    that sits on a CAN link directly, ``byte stream`` for one that sits on a
    UART. A node declares the layers under it with ``over``
    (``csp(address: 10) over kiss``) and joins a system link as one of its
    members.

    ``describe`` gives the system line of the run report. ``create`` makes the
    handle the plan sees under the node's name, before the simulation starts;
    the handle's ``start(system, link)`` runs once it has started, with the
    ``NodeLink`` it joined."""

    runs_on: tuple[str, ...] = ("packets",)

    def describe(self) -> str:
        raise NotImplementedError

    def create(self, name: str, system: SystemContext) -> Any:
        raise NotImplementedError


# ------------------------------------------------------------------- builtins


def builtin_functions(plan_dir: Path, simulation_now: Callable[[], float]) -> dict[str, Any]:
    """Functions every plan sees."""

    def read_file(path: str) -> bytes:
        """A file's bytes, relative to the plan's directory (or the project root)."""
        candidate = (plan_dir / path).resolve()
        if not candidate.is_file():
            candidate = (project.root() / path).resolve()
        if not candidate.is_file():
            raise DslError(f"read_file: {path} not found next to the plan or in the project")
        return candidate.read_bytes()

    def random_bytes(count: int, seed: int = 0) -> bytes:
        return random.Random(int(seed)).randbytes(int(count))

    def text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("latin-1")
        return value if isinstance(value, str) else show(value)

    def number(value: Any) -> int | float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        try:
            return int(str(value).strip(), 0)
        except ValueError:
            try:
                return float(str(value).strip())
            except ValueError:
                raise DslError(f"number: {show(value)} is not a number") from None

    def match(pattern: Any, subject: str) -> Match | None:
        found = re.search(as_pattern(pattern), str(subject))
        return Match(found) if found else None

    def hexstr(value: Any, width: int = 0) -> str:
        if isinstance(value, bytes):
            return value.hex()
        return f"0x{int(value):0{int(width)}x}"

    def from_hex(text_value: str) -> bytes:
        return bytes.fromhex("".join(str(text_value).split()))

    def during(records: Any, start: Any, end: Any) -> list[Any]:
        """The records (each with a ``time``) from ``start`` up to, not
        including, ``end``, in simulated seconds."""
        if not isinstance(records, list):
            raise DslError(f"during: records is a list, not {type_name(records)}")
        low = as_seconds(start, "during: start")
        high = as_seconds(end, "during: end")
        return [record for record in records if low <= _time_of(record, "during") < high]

    def off_profile(
        board: Any,
        sensor: str,
        profile: Any,
        records: Any,
        since: Any,
        lag: Any = 3.0,
        every: Any = 1.0,
    ) -> list[Record]:
        """The housekeeping records whose code for ``sensor`` lies outside the
        codes the profile gave the input in the ``lag`` seconds before each
        record. The profile started at ``since`` and was applied every ``every``
        seconds, as ``board.drive`` applies it. A sensor task that averages
        samples across an update reports a code between two updates' codes,
        so the test is a range."""
        if not isinstance(board, BoardHandle):
            raise DslError("off_profile: the first argument is a board")
        if not isinstance(profile, environment.Profile):
            raise DslError(f"off_profile: profile is a profile, not {type_name(profile)}")
        if not isinstance(records, list):
            raise DslError(f"off_profile: records is a list, not {type_name(records)}")
        origin = as_seconds(since, "off_profile: since")
        window = as_seconds(lag, "off_profile: lag")
        period = as_seconds(every, "off_profile: every")
        if period <= 0:
            raise DslError("off_profile: every must be positive")
        codes: dict[int, int] = {}

        def code_at(tick: int) -> int:
            if tick not in codes:
                codes[tick] = board.sensor_code(str(sensor), volts=profile.at(tick * period))
            return codes[tick]

        off = []
        for record in records:
            time = _time_of(record, "off_profile")
            reported = _field(record, str(sensor))
            if not isinstance(reported, (int, float)) or isinstance(reported, bool):
                raise DslError(f"off_profile: a record at {time:g} s has no number {sensor}")
            latest = math.floor((time - origin) / period + 1e-9)
            earliest = max(0, math.floor((time - window - origin) / period + 1e-9))
            if latest < 0:
                continue  # before the profile started
            accepted = [code_at(tick) for tick in range(earliest, latest + 1)]
            if not min(accepted) <= reported <= max(accepted):
                off.append(Record(time=time, code=reported, expected=sorted(set(accepted))))
        return off

    return {
        "now": simulation_now,
        "len": len,
        "abs": abs,
        "min": min,
        "max": max,
        "round": round,
        "floor": math.floor,
        "ceil": math.ceil,
        "sum": sum,
        "range": lambda *bounds: list(range(*(int(b) for b in bounds))),
        "text": text,
        "number": number,
        "match": match,
        "hex": hexstr,
        "from_hex": from_hex,
        "read_file": read_file,
        "random_bytes": random_bytes,
        "during": during,
        "off_profile": off_profile,
        "crc32": lambda data: zlib.crc32(as_bytes(data, "data")) & 0xFFFFFFFF,
        "regex": lambda source: Regex("/" + str(source).replace("/", "\\/") + "/"),
        **environment.constructors(),
        **environment.time_functions(),
    }


LINK_KINDS = ("uart", "can", "spi", "qspi", "i2c", "gpio")
_LINK_SETTINGS = {"uart": ("baud", "frame"), "can": ("bitrate",)}


def system_constructors() -> dict[str, Any]:
    """Functions only a system declaration uses, besides the node kinds and
    protocol layers: one per link kind (``link can0: can(members: [...])``)."""

    def constructor(kind: str) -> Callable[..., LinkSpec]:
        allowed = (*_LINK_SETTINGS.get(kind, ()), "members")

        def make(**arguments: Any) -> LinkSpec:
            unknown = sorted(set(arguments) - set(allowed))
            if unknown:
                raise DslError(
                    f"{kind}(): unknown argument {', '.join(unknown)} (takes {', '.join(allowed)})"
                )
            members = arguments.pop("members", None)
            if not isinstance(members, list) or not members:
                raise DslError(f"{kind}(): members is a list of boards' links and nodes")
            return LinkSpec(kind, dict(arguments), list(members))

        make.__name__ = kind
        return make

    return {kind: constructor(kind) for kind in LINK_KINDS}
