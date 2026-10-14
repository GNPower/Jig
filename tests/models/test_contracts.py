"""Board contracts: the C# interfaces in src/jig/models/Contracts, the monitor
query behind jig.contracts.verify, verify itself, and the contract semantics
on the fixture models that implement them (tests/fixtures/models)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from jig import contracts
from jig.contracts import Contracts, Problem
from jig.errors import RenodeError

from .bench import Bench


@dataclass(frozen=True)
class StorageEntry:
    name: str
    peripheral: str
    size: int


@dataclass(frozen=True)
class SensorEntry:
    name: str
    adc: str
    channel: int


@dataclass(frozen=True)
class BoardEntries:
    """The board.yaml keys verify reads, as jig.system.boards.Board holds them."""

    storage: tuple[StorageEntry, ...] = ()
    sensors: tuple[SensorEntry, ...] = ()
    rtc: str | None = None
    power_restart: tuple[str, ...] = ()


class FakeSession:
    """Answers the contract query from a table of replies."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.machine: str | None = None
        self.commands: list[str] = []

    def select_machine(self, name: str) -> None:
        self.machine = name

    def execute(self, command: str) -> str:
        self.commands.append(command)
        peripheral = command.split('"')[1]
        return self.replies.get(peripheral, "missing\n\n")


FLASH = "found JigFixtures.SpiNorFlash IStorage=1048576 IPowerCycled ISpiNorFlash\n\n"
ADC = "found JigFixtures.SimpleAdc IAnalogInputs=4 IPowerCycled\n\n"
RTC = "found JigFixtures.SimpleRtc IRealTimeClock IPowerCycled\n\n"
UART = "found Jig.Cadence.CadenceUART\n\n"

GOOD_BOARD = BoardEntries(
    storage=(StorageEntry("flash0", "spi0.flash0", 0x100000),),
    sensors=(SensorEntry("rail", "adc", 3),),
    rtc="rtc",
    power_restart=("spi0.flash0", "adc", "rtc"),
)


def test_parse_reads_the_query_reply() -> None:
    assert contracts.parse(FLASH) == Contracts(
        "JigFixtures.SpiNorFlash",
        frozenset({"IStorage", "IPowerCycled", "ISpiNorFlash"}),
        size=0x100000,
    )
    assert contracts.parse(ADC) == Contracts(
        "JigFixtures.SimpleAdc", frozenset({"IAnalogInputs", "IPowerCycled"}), channel_count=4
    )
    assert contracts.parse(UART) == Contracts("Jig.Cadence.CadenceUART")
    assert contracts.parse("missing\n\n") is None


@pytest.mark.parametrize("reply", ["", "found", "error: no such method", "found X IStorage=big"])
def test_parse_refuses_other_replies(reply: str) -> None:
    with pytest.raises(RenodeError):
        contracts.parse(reply)


def test_verify_accepts_peripherals_that_meet_their_contracts() -> None:
    session = FakeSession({"spi0.flash0": FLASH, "adc": ADC, "rtc": RTC})
    assert contracts.verify(session, "dut", GOOD_BOARD) == []
    assert session.machine == "dut"
    # One query per peripheral, however many keys name it.
    assert session.commands == [
        'machine JigContracts "spi0.flash0"',
        'machine JigContracts "adc"',
        'machine JigContracts "rtc"',
    ]


def test_verify_names_every_key_that_is_not_met() -> None:
    board = BoardEntries(
        storage=(
            StorageEntry("flash0", "spi0.flash0", 0x200000),
            StorageEntry("log", "uart0", 0x1000),
        ),
        sensors=(SensorEntry("rail", "adc", 4), SensorEntry("temp", "nowhere", 0)),
        rtc="uart0",
        power_restart=("adc", "uart0"),
    )
    session = FakeSession({"spi0.flash0": FLASH, "adc": ADC, "uart0": UART})
    problems = contracts.verify(session, "dut", board)
    assert [(p.key, p.peripheral) for p in problems] == [
        ("storage.flash0.size", "spi0.flash0"),
        ("storage.log.peripheral", "uart0"),
        ("sensors.rail.channel", "adc"),
        ("sensors.temp.adc", "nowhere"),
        ("rtc", "uart0"),
        ("power.restart[1]", "uart0"),
    ]
    messages = [p.message for p in problems]
    assert messages[0] == "board.yaml gives 2097152 bytes, the model has 1048576"
    assert messages[1] == (
        "Jig.Cadence.CadenceUART does not implement IStorage (the harness calls AttachImage)"
    )
    assert messages[2] == "channel 4 is outside 0-3"
    assert messages[3] == "no such peripheral in machine dut"
    assert "SetDateTime and GetDateTime" in messages[4]
    assert "IPowerCycled" in messages[5]


def test_a_problem_reads_as_key_peripheral_message() -> None:
    problem = Problem("rtc", "uart0", "Jig.Cadence.CadenceUART does not implement IRealTimeClock")
    assert str(problem) == "rtc: uart0: Jig.Cadence.CadenceUART does not implement IRealTimeClock"


MACHINE = """\
spi0: Jig.Cadence.CadenceSPI @ sysbus <0xE0006000, +0x1000>

flash0: JigFixtures.SpiNorFlash @ spi0 0
    size: 0x100000

adc: JigFixtures.SimpleAdc @ sysbus <0x40020000, +0x100>
    channels: 4

rtc: JigFixtures.SimpleRtc @ sysbus

uart0: Jig.Cadence.CadenceUART @ sysbus <0xE0000000, +0x1000>
"""


@pytest.fixture
def board(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


@pytest.mark.renode
def test_the_query_reports_each_models_contracts(board: Bench) -> None:
    def query(name: str) -> Contracts | None:
        return contracts.query(board.session, board.name, name)

    flash = query("spi0.flash0")
    assert flash is not None and flash.type_name == "JigFixtures.SpiNorFlash"
    assert flash.interfaces == {"IStorage", "IPowerCycled", "ISpiNorFlash"}
    assert flash.size == 0x100000
    assert query("sysbus.spi0.flash0") == flash
    adc = query("adc")
    assert adc is not None and adc.interfaces == {"IAnalogInputs", "IPowerCycled"}
    assert adc.channel_count == 4
    rtc = query("rtc")
    assert rtc is not None and rtc.interfaces == {"IRealTimeClock", "IPowerCycled"}
    assert query("uart0") == Contracts("Jig.Cadence.CadenceUART")
    assert query("spi0.flash9") is None


@pytest.mark.renode
def test_verify_on_a_built_machine(board: Bench) -> None:
    assert contracts.verify(board.session, board.name, GOOD_BOARD) == []
    broken = BoardEntries(rtc="adc", power_restart=("uart0",))
    assert [p.key for p in contracts.verify(board.session, board.name, broken)] == [
        "rtc",
        "power.restart[0]",
    ]


@pytest.mark.renode
def test_analog_inputs_report_the_code_for_their_voltage(board: Bench) -> None:
    # SimpleAdc: 12 bits against 2.5 V; codes on the bus at 4 * channel.
    assert board.number("adc ChannelCount") == 4
    assert board.number("adc GetChannelCode 1") == board.number("adc CodeFor 0") == 0
    board.execute("adc SetChannelVoltage 1 1.25")
    assert board.number("adc CodeFor 1.25") == 2048
    assert board.number("adc GetChannelCode 1") == 2048
    assert board.read(0x40020004) == 2048
    assert board.number("adc CodeFor 3.0") == 4095  # limited to full scale
    assert board.number("adc CodeFor -1.0") == 0
    with pytest.raises(RenodeError, match="does not exist"):
        board.execute("adc SetChannelVoltage 4 1.0")
    # The input comes from outside: a power cycle keeps it, clears the volatile read count.
    assert board.read(0x40020080) == 1
    board.execute("adc PowerCycle")
    assert board.read(0x40020080) == 0
    assert board.number("adc GetChannelCode 1") == 2048


@pytest.mark.renode
def test_the_clock_counts_in_simulated_time(board: Bench) -> None:
    assert board.text("rtc GetDateTime") == "2000-01-01T00:00:00"
    board.execute('rtc SetDateTime "2026-10-08T12:34:56.78"')
    assert board.text("rtc GetDateTime") == "2026-10-08T12:34:56"  # whole seconds
    board.run_ms(2500)
    assert board.text("rtc GetDateTime") == "2026-10-08T12:34:58"
    board.execute("rtc PowerCycle")
    assert board.text("rtc GetDateTime") == "2000-01-01T00:00:00"
    with pytest.raises(RenodeError, match="not an ISO 8601"):
        board.execute('rtc SetDateTime "yesterday"')
    with pytest.raises(RenodeError, match="outside"):
        board.execute('rtc SetDateTime "1999-12-31T23:59:59"')
