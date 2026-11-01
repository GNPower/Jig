"""Board contracts: what a board file's settings require of the peripherals
they name.

The harness drives some board IC models through monitor methods: it attaches
storage images, sets sensor inputs, sets and reads the real-time clock and
power-cycles the chips on the board's supply. Each setting that names such a
peripheral requires it to implement one C# interface from
``Antmicro.Renode.Peripherals.Jig.Contracts`` (``src/jig/models/Contracts``):

``storage: [...]``
    ``IStorage`` (``AttachImage(path)``, ``Size``) for every peripheral in the
    list; a ``size`` property the peripheral declares must equal the model's
    ``Size``.
``sensor NAME: ...``
    ``IAnalogInputs`` (``SetChannelVoltage``, ``GetChannelCode``,
    ``CodeFor``, ``ChannelCount``) for the converter, and the channel must be
    below ``ChannelCount``; a device's own converter that Jig drives through
    an ``Analog.ADCChannelSource`` is left out.
``rtc``
    ``IRealTimeClock`` (``SetDateTime``, ``GetDateTime``).

Power cycles need no setting: every peripheral off the ``backup_supply``
whose model implements ``IPowerCycled`` restarts with the board, and the
driver asks the machine which ones do (``jig.system.socs.power_cycled``).

``verify`` checks a board's machine, built in a running session, against
these and returns what is wrong; ``jig check --contracts`` reports the
problems before a plan reaches a missing method. It asks Renode with the
monitor query ``machine JigContracts "<peripheral>"``
(``Contracts/ContractQueries.cs``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from jig.errors import RenodeError

QUERY = "JigContracts"

STORAGE = "IStorage"
ANALOG_INPUTS = "IAnalogInputs"
REAL_TIME_CLOCK = "IRealTimeClock"
POWER_CYCLED = "IPowerCycled"

# The monitor methods the harness calls through each contract, for messages.
_METHODS = {
    STORAGE: "AttachImage",
    ANALOG_INPUTS: "SetChannelVoltage, GetChannelCode and CodeFor",
    REAL_TIME_CLOCK: "SetDateTime and GetDateTime",
    POWER_CYCLED: "PowerCycle",
}


class MonitorSession(Protocol):
    """The part of ``jig.renode.session.RenodeSession`` that ``verify`` uses."""

    def select_machine(self, name: str) -> None: ...

    def execute(self, command: str) -> str: ...


class StorageKey(Protocol):
    """A peripheral on the ``storage`` list (``jig.system.boards.Storage``)."""

    @property
    def name(self) -> str: ...

    @property
    def peripheral(self) -> str: ...

    @property
    def size(self) -> int | None: ...


class SensorKey(Protocol):
    """A ``sensor`` line (``jig.system.boards.Sensor``)."""

    @property
    def name(self) -> str: ...

    @property
    def adc(self) -> str: ...

    @property
    def channel(self) -> int: ...

    @property
    def source(self) -> str | None: ...


class BoardKeys(Protocol):
    """The settings ``verify`` checks (``jig.system.boards.Board``)."""

    @property
    def storage(self) -> Sequence[StorageKey]: ...

    @property
    def sensors(self) -> Sequence[SensorKey]: ...

    @property
    def rtc(self) -> str | None: ...


@dataclass(frozen=True)
class Problem:
    """A setting whose peripheral does not meet its contract."""

    key: str  # the setting, e.g. "storage flash0", "sensor supply" or "rtc"
    peripheral: str  # the peripheral's name in the machine's monitor
    message: str

    def __str__(self) -> str:
        return f"{self.key}: {self.peripheral}: {self.message}"


@dataclass(frozen=True)
class Contracts:
    """The contracts one peripheral implements, as the monitor query reports them."""

    type_name: str
    interfaces: frozenset[str] = field(default_factory=frozenset)
    size: int | None = None  # IStorage.Size
    channel_count: int | None = None  # IAnalogInputs.ChannelCount


def parse(reply: str) -> Contracts | None:
    """The answer to ``machine JigContracts "<name>"``; None when no
    peripheral has that name. Raises ``RenodeError`` for any other reply."""
    lines = [line.strip() for line in reply.splitlines() if line.strip()]
    words = lines[0].split() if lines else []
    if words == ["missing"]:
        return None
    if len(words) < 2 or words[0] != "found":
        raise RenodeError(f"unexpected reply to the {QUERY} query: {reply.strip()!r}")
    interfaces = set()
    values: dict[str, int] = {}
    for word in words[2:]:
        name, _, value = word.partition("=")
        interfaces.add(name)
        if value:
            try:
                values[name] = int(value)
            except ValueError:
                raise RenodeError(f"unexpected value in the {QUERY} reply: {word!r}") from None
    return Contracts(
        type_name=words[1],
        interfaces=frozenset(interfaces),
        size=values.get(STORAGE),
        channel_count=values.get(ANALOG_INPUTS),
    )


def query(session: MonitorSession, machine: str, peripheral: str) -> Contracts | None:
    """The contracts ``peripheral`` implements in ``machine``, or None if it does not exist."""
    session.select_machine(machine)
    return parse(session.execute(f'machine {QUERY} "{peripheral}"'))


def verify(session: MonitorSession, machine: str, board: BoardKeys) -> list[Problem]:
    """Check every setting that names a peripheral of ``board`` (a
    ``jig.system.boards.Board``) against the contract it needs, in ``machine``,
    the name of the board's machine built in ``session``. The machine need not
    be booted: the query reads its peripheral tree and runs no simulated time.

    Covers ``storage`` (IStorage, with the declared ``size``), the
    converters of ``sensor`` lines (IAnalogInputs, with the channel) and
    ``rtc`` (IRealTimeClock). Returns the problems in the board's order; an
    empty list means every setting is met."""
    found: dict[str, Contracts | None] = {}

    def contracts_of(peripheral: str) -> Contracts | None:
        if peripheral not in found:
            found[peripheral] = query(session, machine, peripheral)
        return found[peripheral]

    problems: list[Problem] = []

    def require(key: str, peripheral: str, interface: str) -> Contracts | None:
        contracts = contracts_of(peripheral)
        if contracts is None:
            problems.append(Problem(key, peripheral, f"no such peripheral in machine {machine}"))
            return None
        if interface not in contracts.interfaces:
            problems.append(
                Problem(
                    key,
                    peripheral,
                    f"{contracts.type_name} does not implement {interface} "
                    f"(the harness calls {_METHODS[interface]})",
                )
            )
            return None
        return contracts

    for storage in board.storage:
        key = f"storage {storage.name}"
        contracts = require(key, storage.peripheral, STORAGE)
        if contracts is not None and storage.size is not None and contracts.size != storage.size:
            problems.append(
                Problem(
                    key,
                    storage.peripheral,
                    f"its size property gives {storage.size} bytes, the model has {contracts.size}",
                )
            )
    for sensor in board.sensors:
        if sensor.source is not None:
            continue  # an ADCChannelSource drives it, not IAnalogInputs
        key = f"sensor {sensor.name}"
        contracts = require(key, sensor.adc, ANALOG_INPUTS)
        count = None if contracts is None else contracts.channel_count
        if count is not None and not 0 <= sensor.channel < count:
            problems.append(
                Problem(key, sensor.adc, f"channel {sensor.channel} is outside 0-{count - 1}")
            )
    if board.rtc is not None:
        require("rtc", board.rtc, REAL_TIME_CLOCK)
    return problems
