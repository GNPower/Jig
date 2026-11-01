"""Test doubles for code built on Jig: no Renode, no firmware.

* ``FakeClock``: a clock a ``Scheduler`` advances; it records each step.
* ``FakeUart`` and ``FakeBoard``: a CSP node behind a KISS link, in process.
  It answers pings, plain requests on the ports given to ``FakeBoard.serve``
  and RDP connections on the ports given to ``FakeBoard.serve_rdp``, and
  sends packets of its own with ``FakeBoard.send_to``.
* ``FakeSimulation``, ``FakeMachine`` and ``run_source``: run a plan on
  simulated time alone. A system declaration builds fake machines from real
  board definitions (their plugins load, their members and codecs work); a
  board's UART reaches a test's ``FakeBoard`` through ``uarts``, a CAN link
  is a ``FakeCanPort`` that keeps and acknowledges the frames harness nodes
  write and takes the frames a test feeds it, and ``on_board`` scripts a
  machine's ``FakeConsole`` once the plan declares it.

For example::

    scheduler = Scheduler(FakeClock(), step_ns=10_000_000)
    board = FakeBoard(scheduler)
    node = CspNode("gs", 8, scheduler, kiss=board.uart)
    scheduler.run(node.ping(3, b"hello"))
"""

from __future__ import annotations

import contextlib
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from jig import project
from jig.errors import ConfigError, ExpectTimeoutError
from jig.ext.csp import protocol as csp
from jig.ext.csp.rdp import RdpConnection
from jig.lang.interpreter import Interpreter, RunOptions
from jig.lang.parser import load
from jig.report.report import SuiteReport
from jig.runtime.environment import BackgroundRegistry
from jig.runtime.scheduler import Scheduler, WaitTimeoutError, seconds_to_ns
from jig.system import boards, registry
from jig.system.boards import Board
from jig.system.socs import MachineConfig, SocDriver, driver_for
from jig.system.system import BusContext, Event
from jig.transports.can import CanBusPort, CanFrame
from jig.transports.terminal import Terminal, encode_key

T = TypeVar("T")

PayloadHandler = Callable[[bytes], bytes | None]
"""Turns one received payload into a reply, or None for no reply."""


class FakeClock:
    """A clock in nanoseconds that a scheduler advances; ``steps`` lists each step."""

    def __init__(self) -> None:
        self.now_ns = 0
        self.steps: list[int] = []

    def advance(self, nanoseconds: int) -> None:
        self.steps.append(nanoseconds)
        self.now_ns += nanoseconds


class FakeUart:
    """The harness side of a KISS link whose far end is a FakeBoard.

    Bytes travel in both directions at the next scheduler service, as they would
    across a step on an emulated board, never inside the sender's call."""

    def __init__(self, board: FakeBoard, label: str = "fake.kiss") -> None:
        self.label = label
        self._board = board
        self._listeners: list[Callable[[bytes, list[int]], None]] = []
        self._to_board: list[bytes] = []
        self._to_node: list[tuple[bytes, int]] = []

    def subscribe(self, listener: Callable[[bytes, list[int]], None]) -> None:
        self._listeners.append(listener)

    def write(self, data: bytes) -> None:
        self._to_board.append(data)

    def deliver(self, data: bytes, stamp_us: int) -> None:
        self._to_node.append((data, stamp_us))

    def pump(self) -> None:
        while self._to_board or self._to_node:
            to_board, self._to_board = self._to_board, []
            for data in to_board:
                self._board.from_node(data)
            to_node, self._to_node = self._to_node, []
            for data, stamp_us in to_node:
                for listener in self._listeners:
                    listener(data, [stamp_us] * len(data))


class FakeBoard:
    """CSP node ``address`` behind a KISS link: pings on port 1, plain
    request and reply on every port registered with ``serve``, and RDP
    connections on every port registered with ``serve_rdp``.

    ``drop_segment(raw)`` returning True loses that RDP segment on its way in,
    which exercises the harness's retransmissions."""

    def __init__(self, scheduler: Scheduler, address: int = 3) -> None:
        self.scheduler = scheduler
        self.address = address
        self.uart = FakeUart(self)
        self.drop_segment: Callable[[bytes], bool] = lambda _raw: False
        self.received: list[csp.Packet] = []  # packets that no service took
        self._decoder = csp.KissDecoder()
        self._plain: dict[int, PayloadHandler] = {}
        self._services: dict[int, Callable[[], PayloadHandler]] = {}
        self._rdp: dict[tuple[int, int, int], tuple[RdpConnection, PayloadHandler, csp.CspId]] = {}
        scheduler.add_service(self.service)

    @property
    def now_ms(self) -> int:
        return self.scheduler.now_ns // 1_000_000

    def serve(self, port: int, handler: PayloadHandler) -> None:
        """Answer packets without RDP on ``port``: ``handler(payload)`` gives
        the reply, sent back to the requester's port (None: no reply)."""
        self._plain[port] = handler

    def serve_rdp(self, port: int, handler: Callable[[], PayloadHandler]) -> None:
        """Accept RDP connections on ``port``. ``handler()`` makes one payload
        handler per connection; its replies go back on that connection."""
        self._services[port] = handler

    def send_to(
        self,
        destination: int,
        dport: int,
        data: bytes,
        *,
        sport: int = 0,
        priority: int = csp.PRIORITY_NORMAL,
    ) -> None:
        """One packet from this board, outside any connection."""
        self._send(csp.CspId(self.address, destination, dport, sport, priority=priority), data)

    def from_node(self, data: bytes) -> None:
        for packet in self._decoder.feed(data):
            self._on_packet(packet)

    def _send(self, ident: csp.CspId, data: bytes) -> None:
        payload = csp.crc32_append(data) if ident.flags & csp.FLAG_CRC32 else data
        self.uart.deliver(
            csp.kiss_encode(csp.Packet(ident, payload)), self.scheduler.now_ns // 1000
        )

    def _on_packet(self, packet: csp.Packet) -> None:
        if packet.id.dst not in (self.address, csp.BROADCAST):
            return
        data = packet.data
        if packet.id.flags & csp.FLAG_CRC32:
            stripped = csp.crc32_strip(data)
            if stripped is None:
                return
            data = stripped
        rdp = bool(packet.id.flags & csp.FLAG_RDP)
        if packet.id.dport == csp.PORT_PING and not rdp and packet.id.dst == self.address:
            self._send(packet.id.reply(), data)
            return
        if not rdp and packet.id.dport in self._plain:
            answer = self._plain[packet.id.dport](data)
            if answer is not None:
                self._send(packet.id.reply(), answer)
            return
        if rdp and packet.id.dport in self._services:
            if self.drop_segment(data):
                return
            key = (packet.id.src, packet.id.sport, packet.id.dport)
            if key not in self._rdp:
                self._rdp[key] = (
                    RdpConnection(iss=4321),
                    self._services[packet.id.dport](),
                    packet.id.reply(),
                )
            connection, handler, reply_id = self._rdp[key]
            connection.on_segment(data, self.now_ms)
            while connection.received:
                answer = handler(connection.received.popleft())
                if answer is not None and connection.can_write:
                    connection.write(answer, self.now_ms)
            self._flush(connection, reply_id)
            return
        self.received.append(csp.Packet(packet.id, data))

    def _flush(self, connection: RdpConnection, reply_id: csp.CspId) -> None:
        while connection.outgoing:
            self._send(reply_id, connection.outgoing.pop(0))

    def service(self) -> None:
        self.uart.pump()
        for connection, _handler, reply_id in list(self._rdp.values()):
            connection.poll(self.now_ms)
            self._flush(connection, reply_id)


class FakeConsole:
    """A board's console as a test scripts it: ``feed`` makes text arrive now,
    ``replies`` maps a command line to what the board prints when it gets it,
    ``sent`` lists every line the plan sent and ``pressed`` every key it
    pressed. Waits run on the simulation's scheduler, as on a real console.

    With ``terminal`` (a link whose stack has ``vt100``), what a test feeds
    is rendered as a terminal shows it, as in a run: ``text`` and ``buffer``
    are the rendered text, ``raw`` the bytes fed and ``raw_buffer`` those
    fed since the last ``clear()``."""

    def __init__(self, simulation: FakeSimulation, label: str, *, terminal: bool = False) -> None:
        self._simulation = simulation
        self.label = label
        self.eol = "\r"
        self.sent: list[str] = []
        self.pressed: list[str] = []
        self.replies: dict[str, str] = {}
        self.raw = b""  # everything fed, as Latin-1 bytes
        self._raw_start = 0  # raw[:_raw_start] came before the last clear()
        self._buffer = ""
        self._text = ""
        self._terminal = Terminal() if terminal else None
        self._last_data_ns = 0

    @property
    def text(self) -> str:
        return self._text if self._terminal is None else self._terminal.text

    @property
    def buffer(self) -> str:
        return self._buffer if self._terminal is None else self._terminal.since_mark

    @property
    def raw_buffer(self) -> bytes:
        return self.raw[self._raw_start :]

    def feed(self, text: str) -> None:
        data = text.encode("latin-1", errors="replace")
        self.raw += data
        if self._terminal is not None:
            self._terminal.feed(data)
        self._buffer += text
        self._text += text
        self._last_data_ns = self._simulation.now_ns

    def clear(self) -> None:
        self._buffer = ""
        self._raw_start = len(self.raw)
        if self._terminal is not None:
            self._terminal.mark()

    def write(self, text: str) -> None:
        self._last_data_ns = self._simulation.now_ns

    def press(self, key: str) -> None:
        encode_key(key)  # an unknown name raises ValueError, as on a real console
        self.pressed.append(key)
        self._last_data_ns = self._simulation.now_ns

    def send_line(self, line: str, terminator: str | None = None) -> None:
        self.sent.append(line)
        self.write(line + (self.eol if terminator is None else terminator))
        if line in self.replies:
            self.feed(self.replies[line])

    def search(self, pattern: str) -> re.Match[str] | None:
        return re.search(pattern, self.buffer)

    async def expect(self, pattern: str, timeout: float) -> re.Match[str]:
        rx = re.compile(pattern)
        match: re.Match[str] = await self._simulation.scheduler.wait_for(
            lambda: rx.search(self.buffer),
            timeout,
            error=lambda: ExpectTimeoutError(self.label, pattern, timeout, self.buffer),
        )
        return match

    async def drain(self, settle: float, limit: float = 30.0) -> str:
        settle_ns = seconds_to_ns(settle)
        scheduler = self._simulation.scheduler
        with contextlib.suppress(WaitTimeoutError):  # still talking after `limit`
            await scheduler.wait_for(
                lambda: True if scheduler.now_ns - self._last_data_ns >= settle_ns else None, limit
            )
        return self.buffer

    async def run(self, command: str, settle: float = 0.5) -> str:
        self.clear()
        self.send_line(command)
        return await self.drain(settle)


class FakeMachine:
    """The parts of a machine plans and plugins use, without Renode: its
    definition and settings, power and reset (as events), consoles a test
    scripts (``FakeConsole``) and the UARTs it connects through
    ``FakeSimulation.uarts``. Monitor commands are recorded in ``commands``
    and answer nothing."""

    def __init__(
        self, simulation: FakeSimulation, config: MachineConfig, driver: SocDriver, power: str
    ) -> None:
        self.simulation = simulation
        self.config = config
        self.driver = driver
        self.initial_power = power
        self.powered = False
        self.booted = False
        self.commands: list[str] = []
        self.consoles: dict[str, FakeConsole] = {}

    def __repr__(self) -> str:
        return f"<FakeMachine {self.name} ({self.board.name})>"

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def board(self) -> Board:
        return self.config.board

    @property
    def console(self) -> FakeConsole:
        return self.console_on("console")

    def console_on(self, link: str) -> FakeConsole:
        if link not in self.consoles:
            interface = self.board.interface(link)
            if interface.kind != "uart":
                raise ConfigError(f"{self.name}: no external UART link {link!r}")
            console = FakeConsole(
                self.simulation, f"{self.name}.{link}", terminal="vt100" in interface.stack
            )
            console.eol = interface.eol
            self.consoles[link] = console
        return self.consoles[link]

    def port(self, interface: str) -> Any:
        found = self.simulation.uarts.get((self.name, interface))
        if found is None:
            raise ConfigError(f"{self.name}: no fake UART for {interface!r} (FakeSimulation.uarts)")
        return found

    def execute(self, command: str) -> str:
        self.commands.append(command)
        return ""

    def reset(self) -> None:
        self.simulation.emit(self.name, "reset", "system reset input")

    def power_on(self) -> None:
        if self.powered:
            return
        self.booted = self.powered = True
        self.simulation.emit(self.name, "power", "on")

    def power_off(self) -> None:
        if not self.powered:
            return
        self.simulation.emit(self.name, "power", "off")
        self.powered = False

    async def power_cycle(self, off: float = 1.0) -> None:
        self.simulation.emit(self.name, "power", f"off for {off:g} s")
        self.powered = False
        await self.simulation.scheduler.sleep(off)
        self.booted = self.powered = True
        self.simulation.emit(self.name, "power", "on")

    def boot(self) -> None:
        """The boot flow again, as an event; a powered-off board is an error."""
        if not self.powered:
            raise ConfigError(f"{self.name} is powered off: power it on before booting it")
        self.booted = True
        self.simulation.emit(self.name, "boot", self.config.boot)

    def _start(self) -> None:
        if self.initial_power == "off":
            self.simulation.emit(self.name, "power", "off")
            return
        self.booted = self.powered = True
        self.simulation.emit(self.name, "boot", self.config.boot)


class FakeCanPort(CanBusPort):
    """The harness's adapter on a fake CAN link (``FakeSimulation.can_buses``).

    A stand-in for Renode's CAN bus answers the port's monitor commands: it
    acknowledges every frame a harness node writes and keeps it in
    ``written``, stamped with the simulated time of the write and sent by
    ``harness``. At the next scheduler service the port hands those frames
    to the link's listeners, its bus observers and the nodes on it, as a
    run's bus hands back the harness's own frames. ``acknowledging`` is what
    the plan last set with ``acknowledge(...)``. ``feed`` makes frames from
    a board arrive."""

    def __init__(self, scheduler: Scheduler, name: str) -> None:
        super().__init__(_FakeCanBus(self), name)  # type: ignore[arg-type]
        self.written: list[CanFrame] = []
        self.acknowledging = True
        self._scheduler = scheduler
        self._unheard: list[CanFrame] = []  # written, not yet handed to the listeners
        scheduler.add_service(self._hand_back)

    def _wrote(self, frame_id: int, data: bytes, *, extended: bool, remote: bool) -> None:
        stamp_us = self._scheduler.now_ns // 1000
        frame = CanFrame(frame_id, data, extended, remote, stamp_us, "harness")
        self.written.append(frame)
        self._unheard.append(frame)

    def _hand_back(self) -> None:
        if self._unheard:
            frames, self._unheard = self._unheard, []
            self.feed(frames)


class _FakeCanBus:
    """Renode's side of a fake CAN link: the monitor commands a
    ``CanBusPort`` sends (``Write``, ``WriteFrames``, ``HarnessAcknowledges``),
    answered as the bus answers them when a node acknowledges."""

    def __init__(self, port: FakeCanPort) -> None:
        self._port = port

    def execute(self, command: str) -> str:
        _bus, verb, arguments = command.split(" ", 2)
        if verb == "HarnessAcknowledges":
            self._port.acknowledging = arguments == "true"
            return ""
        if verb == "Write":
            frame_id, data, extended, remote = arguments.split(" ")
            self._port._wrote(
                int(frame_id, 0),
                bytes.fromhex(data.strip('"')),
                extended=extended == "true",
                remote=remote == "true",
            )
            return "ack"
        if verb == "WriteFrames":
            for entry in arguments.strip('"').split():
                frame_id, flags, data = entry.split(":")
                self._port._wrote(
                    int(frame_id, 16),
                    bytes.fromhex(data),
                    extended="E" in flags,
                    remote="R" in flags,
                )
            return "ack"
        raise ConfigError(f"a fake CAN link answers no monitor command {command!r}")


class FakeSimulation:
    """The parts of a Simulation the interpreter uses, on simulated time alone."""

    def __init__(self, step_ns: int = 10_000_000, *, workdir: Path | None = None) -> None:
        self.now_ns = 0
        self.seed = 0
        self.scheduler = Scheduler(self, step_ns=step_ns)
        self._workdir = workdir
        self.events: list[Event] = []
        self.background = BackgroundRegistry()
        self.machines: dict[str, FakeMachine] = {}
        # Machine name -> what to do with the machine once a plan declares it
        # (script its console, say).
        self.on_board: dict[str, Callable[[FakeMachine], None]] = {}
        self.uarts: dict[tuple[str, str], Any] = {}  # (machine, interface) -> port
        self.can_buses: dict[str, FakeCanPort] = {}
        self.bus_observers: dict[str, dict[str, Any]] = {}
        self.recorded: dict[str, list[list[Any]]] = {}  # tables bus observers wrote
        self._buses: dict[str, list[tuple[str, str]]] = {}
        self._started = False

    def advance(self, nanoseconds: int) -> None:
        self.now_ns += nanoseconds

    @property
    def workdir(self) -> Path:
        """Where fake machines keep their files (a new temporary directory
        unless one was given)."""
        if self._workdir is None:
            self._workdir = Path(tempfile.mkdtemp(prefix="jig-fake-"))
        return self._workdir

    @property
    def now(self) -> float:
        return self.now_ns / 1e9

    def run(self, awaitable: Awaitable[T]) -> T:
        return self.scheduler.run(awaitable)

    def emit(self, source: str, kind: str, detail: str = "") -> Event:
        event = Event(self.now_ns, source, kind, detail)
        self.events.append(event)
        return event

    def add_board(
        self,
        name: str,
        board: str | Board,
        *,
        boot: str | None = None,
        performance_mips: int | None = None,
        storage: dict[str, Path] | None = None,
        options: Mapping[str, Any] | None = None,
        power: str | None = None,
    ) -> FakeMachine:
        if name in self.machines:
            raise ConfigError(f"a machine named {name!r} already exists")
        workdir = (self.workdir / name).as_posix()
        if isinstance(board, Board):
            chosen = board.option_values if options is None else options
            definition = board.evaluate(chosen, workdir) if board.file is not None else board
        else:
            definition = boards.find(board, options=options, workdir=workdir)
        plugins = registry.default()
        plugins.load_board(definition)
        driver = driver_for(definition)
        config = MachineConfig(
            name=name,
            board=definition,
            boot=boot or definition.default_boot,
            performance_mips=performance_mips or definition.performance_mips,
            workdir=self.workdir / name,
            storage_images=dict(storage or {}),
            options=dict(definition.option_values),
            hooks=plugins.hooks_for(definition),
        )
        machine = FakeMachine(self, config, driver, power or definition.power_initial)
        self.machines[name] = machine
        prepare = self.on_board.get(name)
        if prepare is not None:
            prepare(machine)
        return machine

    def add_can_bus(
        self, name: str, *, bitrate: float = 1_000_000, members: list[tuple[str, str]] | None = None
    ) -> None:
        self._buses[name] = list(members or [])

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        sources = [machine.board.source for machine in self.machines.values()]
        observers = registry.default().bus_observers_for(sources)
        for name in self._buses:
            port = FakeCanPort(self.scheduler, name)
            self.can_buses[name] = port
            context = BusContext(name, port, _TableSink(self))  # type: ignore[arg-type]
            self.bus_observers[name] = {
                observer: entry.value(context) for observer, entry in observers.items()
            }
        for machine in self.machines.values():
            machine._start()


class _TableSink:
    """Stands in for the recorder: keeps the rows bus observers write."""

    def __init__(self, simulation: FakeSimulation) -> None:
        self._simulation = simulation

    def table(self, name: str, header: list[str], row: list[Any]) -> None:
        rows = self._simulation.recorded.setdefault(name, [header])
        rows.append(row)


def run_source(
    directory: Path,
    source: str,
    *,
    name: str = "plan",
    files: Mapping[str, str] | None = None,
    options: RunOptions | None = None,
    simulation: FakeSimulation | None = None,
) -> SuiteReport:
    """Write ``source`` to ``<directory>/<name>.jig`` (``files`` beside it), run
    it on a FakeSimulation (its system declaration, setup, tests and
    teardown) and return the report."""
    for file_name, text in (files or {}).items():
        target = directory / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    plan = directory / f"{name}.jig"
    plan.write_text(source, encoding="utf-8", newline="\n")
    report = SuiteReport(name=name, source=str(plan))
    fake = simulation or FakeSimulation(workdir=directory / "out" / name)
    chosen = options or RunOptions()
    program = load(plan)
    interpreter = Interpreter(program, fake, chosen)  # type: ignore[arg-type]
    default = chosen.default_board or project.current().default_board
    if program.main.items("System") or default is not None:
        report.system = interpreter.build_system()
        interpreter.start_system()
    interpreter.run(report)
    report.simulated_seconds = fake.now
    return report
