"""Simulations: boards in one Renode emulation, advanced in simulated time.

``Simulation`` creates a Renode session, builds each board into a machine
(the SoC platform, the board's platform description, the SoC driver's
overlay, storage images, interface ports), boots it, and advances time on its
``Scheduler``. Each step is one monitor round trip that also returns what
every UART and CAN bus carried, time-stamped, so consoles and protocol
endpoints always see the board's output up to the current simulated instant.

Waiting is asynchronous: consoles, protocol nodes and the plan interpreter are
coroutines on the scheduler, so several exchanges proceed at once. From plain
code, ``Simulation.run`` drives a coroutine to completion::

    with Simulation(name="smoke") as sim:
        dut = sim.add_board("dut", "example-board", boot="jtag")
        sim.start()
        sim.run(dut.console.expect(r"ready", timeout=20))

Every UART interface of a board gets its console when the machine is built,
so a console holds what the board sent from the start of the run, however
late the plan first reads it.

Before Renode starts, ``start`` checks that the files each board needs exist
(``Machine.required_files``: firmware, storage images, register-map sources)
and stops with a ConfigError naming each missing one.

Power: a board starts powered on and booted, or powered off (board.yaml
``power.initial``, or the plan's choice). A powered-off board's cores are
halted; powering it on runs its boot flow the first time and the SoC's
power-on sequence, through reset, after a power-off. ``Machine.boot`` runs the
boot flow again, which a board whose boot mode needs a debugger (a Zynq-7000
booted over JTAG) waits for after a power cycle.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

from jig import project
from jig.errors import ConfigError, ExpectTimeoutError, SimulationError
from jig.ext import cosim
from jig.renode import platforms
from jig.renode.session import RenodeSession
from jig.runtime.environment import BackgroundRegistry
from jig.runtime.recorder import Recorder
from jig.runtime.scheduler import (
    NANOSECONDS_PER_SECOND,
    Scheduler,
    Wait,
    WaitTimeoutError,
    seconds_to_ns,
)
from jig.system import boards, registry
from jig.system.boards import Board
from jig.system.renode_shim import RenodeConfig, find_renode, open_session
from jig.system.socs import MachineConfig, SocDriver, driver_for
from jig.transports import can as can_transport
from jig.transports import uart as uart_transport
from jig.transports.can import CanBusPort
from jig.transports.uart import UartPort

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_STEP = 0.010  # simulated seconds between port collections
RECORDER_FLUSH_NS = 1_000_000_000  # the recorder's files reach the disk every simulated second
ENV_KEEP_RUNS = "JIG_KEEP_RUNS"
DEFAULT_KEEP_RUNS = 20
_RUN_NAME = re.compile(r"\d{8}-\d{6}-.+")


# A run directory something wrote to more recently than this is in use: a run
# on another host (a container and the host can share out/runs) or one that
# just started.
ACTIVE_RUN_SECONDS = 600.0


def prune_runs(root: Path, keep: int, now: float | None = None) -> list[Path]:
    """Delete all but the newest ``keep`` run directories under ``root``,
    sparing any that is still in use."""
    if keep < 0 or not root.is_dir():
        return []
    runs = sorted(
        (entry for entry in root.iterdir() if entry.is_dir() and _RUN_NAME.fullmatch(entry.name)),
        key=lambda entry: entry.name,
    )
    since = (time.time() if now is None else now) - ACTIVE_RUN_SECONDS
    removed = [entry for entry in runs[: max(0, len(runs) - keep)] if not _in_use(entry, since)]
    for entry in removed:
        shutil.rmtree(entry, ignore_errors=True)
    return removed


def _in_use(directory: Path, since: float) -> bool:
    """Whether the directory or anything in it changed after ``since`` (epoch seconds)."""
    try:
        if directory.stat().st_mtime > since:
            return True
        return any(path.stat().st_mtime > since for path in directory.rglob("*"))
    except OSError:
        return True  # unreadable: leave it alone


def _new_run_directory(name: str) -> Path:
    """``out/runs/<time>-<name>``, claimed atomically so that runs started in the
    same second (parallel jobs) never share a directory."""
    root = project.output_dir() / "runs"
    root.mkdir(parents=True, exist_ok=True)
    # UTC, so that runs from the container and from the host sort by when they ran.
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
    for attempt in range(1, 1000):
        candidate = root / (f"{stamp}-{name}" if attempt == 1 else f"{stamp}-{name}-{attempt}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise ConfigError(f"cannot create a run directory for {name} in {root}")


def _keep_runs() -> int:
    value = os.environ.get(ENV_KEEP_RUNS)
    if value is None:
        return DEFAULT_KEEP_RUNS
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{ENV_KEEP_RUNS} must be an integer, not {value!r}") from None


def renode_config(chosen: project.Project) -> RenodeConfig:
    """The project's ``[renode]`` table as the Renode layer takes it."""
    return RenodeConfig(path=chosen.renode_path, version=chosen.renode_version)


@dataclass(frozen=True)
class Event:
    """Something that happened to the system, for reports and observers."""

    time_ns: int
    source: str
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class BusContext:
    """What a bus observer factory (``api.bus_observer``) gets for one CAN bus:
    its name, the harness's port on it (``port.subscribe(listener)`` sees every
    frame), and ``record``, which streams rows to a CSV file in the run
    directory."""

    name: str
    port: CanBusPort
    recorder: Recorder

    def record(self, file: str, header: Sequence[str], row: Sequence[Any]) -> None:
        self.recorder.table(file, list(header), list(row))


class Console:
    """A CLI on a UART: the text the board sent and pattern waits, in simulated time.

    The text comes from the port's record of everything the board sent
    (``UartPort.received``), so a console made after the board started
    printing still holds that output, and so does its transcript. ``text``
    and ``buffer`` decode only when read, which keeps a busy binary link
    that no plan reads as text cheap."""

    def __init__(
        self, simulation: Simulation, port: UartPort, transcript: Path, eol: str = "\r"
    ) -> None:
        self._simulation = simulation
        self.port = port
        self.eol = eol  # what ends a line the harness sends
        self._text = ""  # port.received[:self._text_end], decoded
        self._text_end = 0
        self._buffer = ""  # since the last clear(), decoded up to port.received[:_buffer_end]
        self._buffer_end = 0
        self._last_data_ns = 0
        self._transcript = transcript.open("a", encoding="utf-8", newline="")
        if port.received:  # what the board sent before this console existed
            self._transcript.write(port.received.decode("latin-1"))
            self._transcript.flush()
        port.subscribe(self._on_data)

    @property
    def label(self) -> str:
        return self.port.label

    @property
    def text(self) -> str:
        """Everything the board sent on this UART since the run started."""
        received = self.port.received
        if self._text_end < len(received):
            self._text += received[self._text_end :].decode("latin-1")
            self._text_end = len(received)
        return self._text

    @property
    def buffer(self) -> str:
        """What the board sent since the last ``clear()`` (or the run's start)."""
        received = self.port.received
        if self._buffer_end < len(received):
            self._buffer += received[self._buffer_end :].decode("latin-1")
            self._buffer_end = len(received)
        return self._buffer

    def _on_data(self, data: bytes, _stamps: list[int]) -> None:
        self._last_data_ns = self._simulation.now_ns
        self._transcript.write(data.decode("latin-1"))
        self._transcript.flush()

    def close(self) -> None:
        self._transcript.close()

    def clear(self) -> None:
        """Start the buffer afresh: only what arrives from now on."""
        self._buffer = ""
        self._buffer_end = len(self.port.received)

    def write(self, text: str) -> None:
        self.port.write(text.encode("latin-1"))
        # Quiet periods count from the last traffic in either direction.
        self._last_data_ns = self._simulation.now_ns

    def send_line(self, line: str, terminator: str | None = None) -> None:
        """Send a command line ended the way the firmware's CLI expects it."""
        self.write(line + (self.eol if terminator is None else terminator))

    def search(self, pattern: str) -> re.Match[str] | None:
        """The pattern's first match in the buffer, without waiting."""
        return re.search(pattern, self.buffer)

    def tail(self, characters: int = 400) -> str:
        return self.buffer[-characters:]

    async def expect(self, pattern: str, timeout: float) -> re.Match[str]:
        """Wait up to ``timeout`` simulated seconds for ``pattern`` (a regex) in the buffer."""
        rx = re.compile(pattern)
        match: re.Match[str] = await self._simulation.scheduler.wait_for(
            lambda: rx.search(self.buffer),
            timeout,
            error=lambda: ExpectTimeoutError(self.label, pattern, timeout, self.buffer),
        )
        return match

    async def drain(self, settle: float, limit: float = 30.0) -> str:
        """Advance until the board has been quiet for ``settle`` simulated seconds."""
        settle_ns = seconds_to_ns(settle)
        quiet: Callable[[], bool | None] = lambda: (  # noqa: E731
            True if self._simulation.now_ns - self._last_data_ns >= settle_ns else None
        )
        try:
            await self._simulation.scheduler.wait_for(quiet, limit)
        except WaitTimeoutError:
            log.warning("%s: output did not settle within %g s", self.label, limit)
        return self.buffer

    async def run(self, command: str, settle: float = 0.5) -> str:
        """Send a command and return everything the board printed until it went quiet."""
        self.clear()
        self.send_line(command)
        return await self.drain(settle)


class Machine:
    """A board built into the emulation."""

    def __init__(
        self,
        simulation: Simulation,
        config: MachineConfig,
        driver: SocDriver,
        power: str = "on",
    ) -> None:
        self.simulation = simulation
        self.config = config
        self.driver = driver
        self.initial_power = power
        self.powered = False  # the supply is on
        self.booted = False  # the boot flow ran since the simulation started
        self.ports: dict[str, UartPort] = {}
        self.consoles: dict[str, Console] = {}

    def __repr__(self) -> str:
        return f"<Machine {self.name} ({self.board.name}, {self.config.boot})>"

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def board(self) -> Board:
        return self.config.board

    @property
    def console(self) -> Console:
        return self.console_on("console")

    def console_on(self, interface: str) -> Console:
        """The console on a UART interface. Every UART interface has one from
        the moment the machine is built, recording what the board sends."""
        console = self.consoles.get(interface)
        if console is None:
            uarts = [i.name for i in self.board.interfaces if i.kind == "uart"]
            if interface in uarts:
                raise ConfigError(
                    f"{self.name}: the console on {interface} exists once the simulation starts"
                )
            raise ConfigError(
                f"{self.name}: no UART interface {interface!r} (has: {', '.join(uarts) or 'none'})"
            )
        return console

    def port(self, interface: str) -> UartPort:
        if interface not in self.ports:
            raise ConfigError(f"{self.name}: no UART interface {interface!r}")
        return self.ports[interface]

    def execute(self, command: str) -> str:
        """A monitor command in this machine's context."""
        session = self.simulation.session
        session.select_machine(self.name)
        return session.execute(command)

    # ------------------------------------------------------------- inputs

    def set_sensor(self, name: str, volts: float) -> int:
        """Drive a sensor input; returns the code the converter will report."""
        sensor = self.board.sensor(name)
        self.execute(f"{sensor.adc} SetChannelVoltage {sensor.channel} {volts!r}")
        self.simulation.emit(self.name, "sensor", f"{name} = {volts:g} V")
        return self.sensor_code(name)

    def set_time(self, iso: str) -> None:
        """Set the board's real-time clock (board.yaml ``rtc``)."""
        self.execute(f'{self._rtc()} SetDateTime "{iso}"')
        self.simulation.emit(self.name, "rtc", iso)

    def rtc_time(self) -> str:
        reply = self.execute(f"{self._rtc()} GetDateTime").strip()
        return reply.splitlines()[0] if reply else ""

    def _rtc(self) -> str:
        if self.board.rtc is None:
            raise ConfigError(f"board {self.board.name} names no real-time clock (board.yaml rtc)")
        return self.board.rtc

    def sensor_code(self, name: str, volts: float | None = None) -> int:
        """The code the sensor's converter reports now, or for an input of
        ``volts`` (the ADC model's CodeFor)."""
        sensor = self.board.sensor(name)
        if volts is None:
            command = f"{sensor.adc} GetChannelCode {sensor.channel}"
        else:
            command = f"{sensor.adc} CodeFor {float(volts)!r}"
        reply = self.execute(command).strip()
        try:
            return int(reply.splitlines()[0], 0)
        except (ValueError, IndexError):
            raise SimulationError(
                f"{self.name}: unexpected reply from {sensor.adc}: {reply!r}"
            ) from None

    # -------------------------------------------------------- power, reset

    def reset(self) -> None:
        """Assert the board's system-reset input (a reset button)."""
        self.simulation.emit(self.name, "reset", "system reset input")
        self.driver.reset(self.config, self.simulation.session)

    def _supply_on(self) -> None:
        session = self.simulation.session
        if not self.booted:
            self.driver.boot(self.config, session)
            self.booted = True
        else:
            self.driver.power_on(self.config, session)
        self.powered = True

    def power_on(self) -> None:
        """Apply power (nothing happens when it is on already)."""
        if self.powered:
            return
        self._supply_on()
        self.simulation.emit(self.name, "power", "on")

    def power_off(self) -> None:
        """Remove power (nothing happens when it is off already)."""
        if not self.powered:
            return
        self.simulation.emit(self.name, "power", "off")
        self.driver.power_off(self.config, self.simulation.session)
        self.powered = False

    async def power_cycle(self, off: float = 1.0) -> None:
        """Remove power for ``off`` simulated seconds, then restore it."""
        self.simulation.emit(self.name, "power", f"off for {off:g} s")
        if self.powered:
            self.driver.power_off(self.config, self.simulation.session)
            self.powered = False
        await self.simulation.scheduler.sleep(off)
        self._supply_on()
        self.simulation.emit(self.name, "power", "on")

    def boot(self) -> None:
        """Run the board's boot flow again: what its SoC driver does for the
        machine's boot mode at the start of a run (``SocDriver.boot``).

        A board whose boot mode needs a debugger waits for one after
        ``power_cycle``, or ``power_on`` after ``power_off``, as hardware does:
        a Zynq-7000 booted over JTAG stays in its BootROM until this replays
        the debugger's load. A board that boots from its own storage is
        running again after power-on, and this runs the flow once more from
        that state. Nothing resets the board first. The board must be powered."""
        if not self.powered:
            raise ConfigError(f"{self.name} is powered off: power it on before booting it")
        self.driver.boot(self.config, self.simulation.session)
        self.booted = True
        self.simulation.emit(self.name, "boot", self.config.boot)

    # ------------------------------------------------------------ building

    def required_files(self, *, boot: bool = True) -> list[tuple[str, Path]]:
        """What the machine needs on disk, as (what it is, path): its platform
        descriptions, the register-map sources and co-simulation inputs of
        its options, the images its storage starts from and, when it will
        boot, every firmware file board.yaml names plus what the SoC driver
        adds through an optional ``required_files(config)`` method (for
        example a file its own settings name)."""
        files = [("platform description", path) for path in self.platform_files()]
        for source in self.config.register_map_sources():
            files.append((f"register map source for {source.target}", source.package))
            if source.module is not None:
                files.append((f"register map source for {source.target}", source.module))
        for choice in self.config.choices():
            if choice.cosim is not None:
                files += [(f"co-simulation input of {choice.name}", p) for p in choice.cosim.inputs]
        for storage, image in self.config.storage_images.items():
            files.append((f"image for {storage}", image))
        if boot:
            files += [(f"firmware {role}", path) for role, path in self.board.firmware.items()]
            extra = getattr(self.driver, "required_files", None)
            if callable(extra):
                found: Mapping[str, Path] = extra(self.config)
                files += [(str(what), Path(path)) for what, path in found.items()]
        return files

    def platform_files(self) -> list[Path]:
        """The platform descriptions the machine loads, in order: the SoC
        platform, the board's, then those its chosen options add."""
        files = []
        if self.board.soc_platform is not None:
            files.append(platforms.path(self.board.soc_platform))
        files.append(self.board.platform)
        files += [c.platform for c in self.config.choices() if c.platform is not None]
        return files

    def _build(self, session: RenodeSession, *, own_time: bool = False) -> None:
        """Create the machine, load its platform and connect its interfaces.

        ``own_time`` gives the machine its own time source, which a system of
        several boards needs (see ``RenodeSession.create_machine``)."""
        self.config.workdir.mkdir(parents=True, exist_ok=True)
        choices = self.config.choices()
        # Co-simulated RTL builds before Renode loads anything, so a failed
        # build stops the run with its log instead of a half-built machine.
        libraries = {
            choice.name: cosim.library(choice.cosim, choice.platform or self.board.platform)
            for choice in choices
            if choice.cosim is not None
        }
        includes = "".join(f'using "{path.as_posix()}"\n' for path in self.platform_files())
        overlay = self.config.workdir / "machine.repl"
        overlay.write_text(
            f"{includes}\n{self.driver.overlay(self.config)}\n",
            encoding="utf-8",
            newline="\n",
        )
        session.create_machine(self.name, own_time=own_time)
        session.execute(f"machine LoadPlatformDescription @{overlay.as_posix()}")
        self.driver.configure(self.config, session)
        for choice in choices:
            for command in choice.commands:
                session.execute(command.format(workdir=self.config.workdir.as_posix()))
            if choice.cosim is not None:
                cosim.attach(session, choice.cosim, libraries[choice.name], self.config.workdir)
        for interface in self.board.interfaces:
            if interface.kind == "uart":
                port = UartPort.attach(
                    session, self.name, interface.peripheral, f"{self.name}.{interface.name}"
                )
                self.ports[interface.name] = port
                self.simulation._register_uart(port)
                # Recorded from the start, whether or not a plan reads it as a console.
                transcript = self.config.workdir / f"{interface.name}.log"
                self.consoles[interface.name] = Console(
                    self.simulation, port, transcript, interface.eol
                )

    def _start(self, session: RenodeSession) -> None:
        """Boot the machine, or leave it unpowered (its initial power state)."""
        if self.initial_power == "off":
            self.driver.power_off(self.config, session)
            self.powered = False
            self.simulation.emit(self.name, "power", "off")
            return
        self.driver.boot(self.config, session)
        self.booted = True
        self.powered = True
        self.simulation.emit(self.name, "boot", self.config.boot)


class Simulation:
    """Boards in one Renode emulation, advanced in steps of simulated time."""

    def __init__(
        self,
        *,
        name: str = "run",
        workdir: Path | None = None,
        seed: int = 0,
        step: float = DEFAULT_STEP,
        step_wall_limit: float | None = 600.0,
        renode: Path | None = None,
        allow_untested_renode: bool = False,
    ) -> None:
        self.name = name
        self.seed = seed
        self.workdir = workdir or _new_run_directory(name)
        self.step_ns = seconds_to_ns(step)
        if self.step_ns <= 0:
            raise ConfigError("the simulation step must be positive")
        self.step_wall_limit = step_wall_limit
        self.renode = renode  # an explicit Renode (--renode), else discovery decides
        self.allow_untested_renode = allow_untested_renode  # lifts the supported range only
        self._session: RenodeSession | None = None
        self.scheduler = Scheduler(self, step_ns=self.step_ns)
        self.machines: dict[str, Machine] = {}
        self.can_buses: dict[str, CanBusPort] = {}
        self.bus_observers: dict[str, dict[str, Any]] = {}  # bus -> observer name -> observer
        self.events: list[Event] = []
        self._event_listeners: list[Callable[[Event], None]] = []
        self.background = BackgroundRegistry()  # work plans leave running (sensor drivers)
        self.recorder = Recorder(self.workdir)  # events, steps, telemetry, observer tables
        self._flushed_ns = 0
        self._bus_specs: dict[str, tuple[float, list[tuple[str, str]]]] = {}
        self._uart_by_external: dict[str, UartPort] = {}
        self._started = False

    @property
    def session(self) -> RenodeSession:
        if self._session is None:
            raise ConfigError("the simulation has not started")
        return self._session

    # ------------------------------------------------------------- building

    def add_board(
        self,
        name: str,
        board: str | Board,
        *,
        boot: str | None = None,
        performance_mips: int | None = None,
        storage: dict[str, Path] | None = None,
        options: dict[str, str] | None = None,
        power: str | None = None,
    ) -> Machine:
        """Adds a board. ``options`` picks values for the board's options
        (``board.yaml`` ``options``); the others take their defaults. ``power``
        (``on`` or ``off``) overrides the board's ``power.initial``."""
        if self._started:
            raise ConfigError("boards must be added before the simulation starts")
        if name in self.machines:
            raise ConfigError(f"a machine named {name!r} already exists")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise ConfigError(f"machine name {name!r} must be an identifier")
        definition = board if isinstance(board, Board) else boards.find(board)
        plugins = registry.default()
        plugins.load_board(definition)
        driver = driver_for(definition)
        mode = boot or definition.default_boot
        if mode not in driver.boot_modes:
            raise ConfigError(
                f"{name}: boot mode {mode!r} is not one of {', '.join(driver.boot_modes)}"
            )
        for storage_name in storage or {}:
            if storage_name not in {s.name for s in definition.storage}:
                raise ConfigError(
                    f"{name}: board {definition.name} has no storage {storage_name!r}"
                )
        initial = power or definition.power_initial
        if initial not in boards.POWER_STATES:
            raise ConfigError(f"{name}: power must be on or off, not {initial!r}")
        config = MachineConfig(
            name=name,
            board=definition,
            boot=mode,
            performance_mips=performance_mips or definition.performance_mips,
            workdir=self.workdir / name,
            storage_images=dict(storage or {}),
            options=definition.resolve_options(options),
            hooks=plugins.hooks_for(definition),
        )
        machine = Machine(self, config, driver, initial)
        self.machines[name] = machine
        return machine

    def add_can_bus(
        self, name: str, *, bitrate: float = 1_000_000, members: list[tuple[str, str]] | None = None
    ) -> None:
        """A CAN bus joining the named (machine, interface) pairs; the harness is a node on it."""
        if self._started:
            raise ConfigError("buses must be added before the simulation starts")
        if name in self._bus_specs:
            raise ConfigError(f"a bus named {name!r} already exists")
        for machine_name, interface in members or []:
            if machine_name not in self.machines:
                raise ConfigError(f"bus {name}: no machine {machine_name!r}")
            spec = self.machines[machine_name].board.interface(interface)
            if spec.kind != "can":
                raise ConfigError(f"bus {name}: {machine_name}.{interface} is not a CAN interface")
        self._bus_specs[name] = (bitrate, list(members or []))

    def start(self, *, boot: bool = True) -> None:
        """Start Renode and build every machine and bus; then boot each board
        (or leave it unpowered, as its power state says). ``boot=False``
        stops after building, for checks that only inspect the machines.

        Every file a board needs (``Machine.required_files``) is checked
        first, boards that start powered off included, so a missing one
        stops the run with a ConfigError naming it before Renode starts."""
        if self._started:
            return
        if not self.machines:
            raise ConfigError("a simulation needs at least one board")
        self.check_files(boot=boot)
        limit = _keep_runs()
        if limit > 0 and self.workdir.parent == project.output_dir() / "runs":
            # Each run keeps its storage images: bound the history.
            prune_runs(self.workdir.parent, limit - 1)
        self.workdir.mkdir(parents=True, exist_ok=True)
        definitions = [machine.board for machine in self.machines.values()]
        install = find_renode(
            explicit=self.renode,
            config=renode_config(project.current()),
            allow_untested=self.allow_untested_renode,
        )
        session = open_session(
            workdir=self.workdir / "renode",
            install=install,
            model_sets=boards.model_sets(definitions),
            seed=self.seed,
            step_wall_limit=self.step_wall_limit,
        )
        self._session = session
        session.start()
        self._started = True
        several = len(self.machines) > 1
        for machine in self.machines.values():
            machine._build(session, own_time=several)
        if several:
            # Machines take turns through each quantum instead of running on
            # parallel threads, so an event one machine sends another lands at
            # the same sync point in every run.
            session.execute("emulation SetGlobalSerialExecution true")
        observers = registry.default().bus_observers_for(d.source for d in definitions)
        for bus_name, (bitrate, members) in self._bus_specs.items():
            bus = CanBusPort.create(session, bus_name, bitrate)
            for machine_name, interface in members:
                peripheral = self.machines[machine_name].board.interface(interface).peripheral
                bus.connect(machine_name, peripheral)
            self.can_buses[bus_name] = bus
            context = BusContext(bus_name, bus, self.recorder)
            self.bus_observers[bus_name] = {
                observer: entry.value(context) for observer, entry in observers.items()
            }
        if boot:
            for machine in self.machines.values():
                machine._start(session)

    def check_files(self, *, boot: bool = True) -> None:
        """Raise a ConfigError naming every file a board needs that is missing
        (``Machine.required_files``; with ``boot``, its firmware too)."""
        missing = []
        for machine in self.machines.values():
            for what, path in machine.required_files(boot=boot):
                if not path.exists():
                    hint = " (build the firmware first)" if what.startswith("firmware") else ""
                    missing.append(f"{machine.name}: {what} {path} not found{hint}")
        if len(missing) == 1:
            raise ConfigError(missing[0])
        if missing:
            listed = "".join(f"\n    {line}" for line in missing)
            raise ConfigError(f"{len(missing)} files the boards need are missing:{listed}")

    @property
    def started(self) -> bool:
        return self._started

    def close(self) -> None:
        for machine in self.machines.values():
            for console in machine.consoles.values():
                console.close()
        self.recorder.close()
        if self._session is not None:
            self._session.stop()

    def __enter__(self) -> Simulation:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _register_uart(self, port: UartPort) -> None:
        self._uart_by_external[port.port] = port

    # --------------------------------------------------------------- events

    def emit(self, source: str, kind: str, detail: str = "") -> Event:
        event = Event(self.now_ns, source, kind, detail)
        self.events.append(event)
        self.recorder.event(event.time_ns, source, kind, detail)
        for listener in self._event_listeners:
            listener(event)
        return event

    def subscribe(self, listener: Callable[[Event], None]) -> None:
        """Call ``listener`` with every event from now on (dashboards, recorders)."""
        self._event_listeners.append(listener)

    # ----------------------------------------------------------------- time

    @property
    def now_ns(self) -> int:
        return 0 if self._session is None else self._session.elapsed_ns

    @property
    def now(self) -> float:
        return self.now_ns / NANOSECONDS_PER_SECOND

    def advance(self, nanoseconds: int) -> None:
        """One step: run the emulation and hand every port what it carried.

        The scheduler calls this; code outside tasks uses ``run``/``run_for``."""
        if not self._started:
            raise ConfigError("the simulation has not started")
        collected = self.session.step_and_collect(nanoseconds)
        for line in collected.splitlines():
            if line.startswith("U "):
                name, data, stamps = uart_transport.parse_collected(line)
                port = self._uart_by_external.get(name)
                if port is not None:
                    port.feed(data, stamps)
            elif line.startswith("C "):
                name, frames = can_transport.parse_collected(line)
                bus = self.can_buses.get(name)
                if bus is not None:
                    bus.feed(frames)
            elif line.strip():
                log.debug("unexpected collection line %r", line[:80])
        if self.now_ns - self._flushed_ns >= RECORDER_FLUSH_NS:
            self.recorder.flush()
            self._flushed_ns = self.now_ns

    def run(self, awaitable: Awaitable[T]) -> T:
        """Drive the scheduler until ``awaitable`` completes and return its result."""
        return self.scheduler.run(awaitable)

    def sleep(self, seconds: float) -> Wait:
        """An awaitable that lets ``seconds`` of simulated time pass."""
        return self.scheduler.sleep(seconds)

    def run_for(self, seconds: float) -> None:
        self.run(self.scheduler.sleep(seconds))

    def wait_until(self, condition: Callable[[], Any], timeout: float) -> bool:
        """Advance until ``condition()`` is true or ``timeout`` simulated seconds pass."""

        async def wait() -> bool:
            try:
                await self.scheduler.wait_for(lambda: True if condition() else None, timeout)
            except WaitTimeoutError:
                return False
            return True

        return self.run(wait())
