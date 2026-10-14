"""Board contracts: what a board.yaml key requires of the peripheral it names.

The harness drives some board IC models through monitor methods: it attaches
storage images, sets sensor inputs, sets and reads the real-time clock and
power-cycles the chips on the board's supply. Each board.yaml key that names
such a peripheral requires it to implement one C# interface from
``Antmicro.Renode.Peripherals.Jig.Contracts`` (``src/jig/models/Contracts``):

``storage.<name>.peripheral``
    ``IStorage`` (``AttachImage(path)``, ``Size``); ``storage.<name>.size``
    must equal the model's ``Size``.
``sensors.<name>.adc``
    ``IAnalogInputs`` (``SetChannelVoltage``, ``GetChannelCode``,
    ``CodeFor``, ``ChannelCount``); ``sensors.<name>.channel`` must be below
    ``ChannelCount``.
``rtc``
    ``IRealTimeClock`` (``SetDateTime``, ``GetDateTime``).
``power.restart``
    ``IPowerCycled`` (``PowerCycle``) for every peripheral in the list.

``verify`` checks a board's machine, built in a running session, against
these and returns what is wrong; ``jig check`` reports the problems before a
plan reaches a missing method. It asks Renode with the monitor query
``machine JigContracts "<peripheral>"`` (``Contracts/ContractQueries.cs``).
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
    """A ``storage.<name>`` entry (``jig.system.boards.Storage``)."""

    @property
    def name(self) -> str: ...

    @property
    def peripheral(self) -> str: ...

    @property
    def size(self) -> int: ...


class SensorKey(Protocol):
    """A ``sensors.<name>`` entry (``jig.system.boards.Sensor``)."""

    @property
    def name(self) -> str: ...

    @property
    def adc(self) -> str: ...

    @property
    def channel(self) -> int: ...


class BoardKeys(Protocol):
    """The board.yaml keys ``verify`` checks (``jig.system.boards.Board``)."""

    @property
    def storage(self) -> Sequence[StorageKey]: ...

    @property
    def sensors(self) -> Sequence[SensorKey]: ...

    @property
    def rtc(self) -> str | None: ...

    @property
    def power_restart(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class Problem:
    """A board.yaml key whose peripheral does not meet its contract."""

    key: str  # the board.yaml key, e.g. "storage.flash0.peripheral" or "power.restart[2]"
    peripheral: str  # the monitor name the key gives
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
    """Check every board.yaml key that names a peripheral of ``board`` (a
    ``jig.system.boards.Board``) against the contract it needs, in ``machine``,
    the name of the board's machine built in ``session``. The machine need not
    be booted: the query reads its peripheral tree and runs no simulated time.

    Covers ``storage.<name>.peripheral`` (IStorage, with ``size``),
    ``sensors.<name>.adc`` (IAnalogInputs, with ``channel``), ``rtc``
    (IRealTimeClock) and each entry of ``power.restart`` (IPowerCycled).
    Returns the problems in board.yaml order; an empty list means every key is
    met."""
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
        key = f"storage.{storage.name}.peripheral"
        contracts = require(key, storage.peripheral, STORAGE)
        if contracts is not None and contracts.size != storage.size:
            problems.append(
                Problem(
                    f"storage.{storage.name}.size",
                    storage.peripheral,
                    f"board.yaml gives {storage.size} bytes, the model has {contracts.size}",
                )
            )
    for sensor in board.sensors:
        key = f"sensors.{sensor.name}.adc"
        contracts = require(key, sensor.adc, ANALOG_INPUTS)
        count = None if contracts is None else contracts.channel_count
        if count is not None and not 0 <= sensor.channel < count:
            problems.append(
                Problem(
                    f"sensors.{sensor.name}.channel",
                    sensor.adc,
                    f"channel {sensor.channel} is outside 0-{count - 1}",
                )
            )
    if board.rtc is not None:
        require("rtc", board.rtc, REAL_TIME_CLOCK)
    for index, peripheral in enumerate(board.power_restart):
        require(f"power.restart[{index}]", peripheral, POWER_CYCLED)
    return problems
