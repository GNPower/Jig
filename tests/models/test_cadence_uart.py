"""Cadence UART (Jig.Cadence.CadenceUART, Zynq-7000 UART0/1) through a
harness UartPort: reset values (UG585 v1.7 B.33), the baud rate from
BAUDGEN and BAUDDIV, character timing on the line, the 64-byte FIFOs and
their overflow, the receiver timeout, the loopback modes, receive errors and
the interrupt."""

from __future__ import annotations

import itertools

import pytest

from jig.renode.session import RenodeSession

from .bench import Bench

pytestmark = pytest.mark.renode

UART = 0xE0000000
CR, MR, IER, IDR, IMR, ISR = (UART + o for o in (0x00, 0x04, 0x08, 0x0C, 0x10, 0x14))
BAUDGEN, RXTOUT, RXWM, MODEMCR, SR, FIFO = (UART + o for o in (0x18, 0x1C, 0x20, 0x24, 0x2C, 0x30))
BAUDDIV, FLOWDEL, TXWM, RXBS = (UART + o for o in (0x34, 0x38, 0x44, 0x48))

RXRST, TXRST, RX_EN, RX_DIS, TX_EN, TX_DIS = (1 << n for n in range(6))
NO_PARITY = 0x4 << 3
LOCAL_LOOPBACK = 0x2 << 8
AUTOMATIC_ECHO = 0x1 << 8

REMPTY, TEMPTY, TFUL = 1 << 1, 1 << 3, 1 << 4
RACTIVE, TACTIVE = 1 << 10, 1 << 11
# Interrupt status bits.
RTRIG, TOVR, TIMEOUT, PARE, FRAME = 1 << 0, 1 << 12, 1 << 8, 1 << 7, 1 << 6

MACHINE = """\
uart0: Jig.Cadence.CadenceUART @ sysbus <0xE0000000, +0x1000>
    clockFrequency: 50000000
    -> probe@0

probe: JigFixtures.GpioProbe @ sysbus
"""

CHARACTER_NS = 86_800  # 10 bits at 50 MHz / (62 * 7) = 115207 baud


@pytest.fixture(scope="module", autouse=True)
def fine_quantum(renode: RenodeSession) -> None:
    # A harness port stamps a character with its machine's last
    # synchronisation point; without a CPU those come once per global quantum
    # (100 us by default), too coarse for 87 us characters.
    renode.execute('emulation SetGlobalQuantum "0.000001"')


@pytest.fixture
def uart(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def configure(bench: Bench, mode: int = NO_PARITY) -> str:
    """115200 8N1, receiver and transmitter on; returns the harness port's name."""
    port = bench.uart_port("uart0")
    bench.write(MR, mode)
    bench.write(BAUDGEN, 62)
    bench.write(BAUDDIV, 6)
    bench.write(CR, RX_EN | TX_EN)
    return port


def collect(bench: Bench, port: str, nanoseconds: int) -> tuple[bytes, list[int]]:
    """Runs and returns what the port saw: the bytes and their times (us)."""
    data = b""
    stamps: list[int] = []
    for line in bench.session.step_and_collect(nanoseconds).splitlines():
        kind, _, rest = line.partition(" ")
        name, _, rest = rest.partition(" ")
        if kind == "U" and name == port:
            # "U <name> <first>,<gap>,... <hex>": the first stamp is absolute.
            times, _, hexdata = rest.partition(" ")
            first, *gaps = (int(value) for value in times.split(","))
            stamps.append(first)
            for gap in gaps:
                stamps.append(stamps[-1] + gap)
            data += bytes.fromhex(hexdata)
    return data, stamps


def test_reset_values(uart: Bench) -> None:
    expected = {CR: 0x128, MR: 0, IMR: 0, BAUDGEN: 0x28B, RXTOUT: 0, RXWM: 0x20}
    expected |= {MODEMCR: 0, BAUDDIV: 0xF, FLOWDEL: 0, TXWM: 0x20}
    assert {address: uart.read(address) for address in expected} == expected
    # Live status: both FIFOs empty.
    assert uart.read(SR) & (REMPTY | TEMPTY) == REMPTY | TEMPTY


def test_baud_rate_and_character_timing(uart: Bench) -> None:
    port = configure(uart)
    assert uart.number("uart0 BaudRate") == 115207
    for byte in b"Hi\r\n":
        uart.write(FIFO, byte)
    assert uart.read(SR) & TACTIVE
    data, stamps = collect(uart, port, 400_000)
    assert data == b"Hi\r\n"
    gaps = [later - earlier for earlier, later in itertools.pairwise(stamps)]
    assert all(86 <= gap <= 88 for gap in gaps), gaps
    assert uart.read(SR) & (TEMPTY | TACTIVE) == TEMPTY


def test_a_second_stop_bit_and_parity_lengthen_the_character(uart: Bench) -> None:
    port = configure(uart, mode=0x2 << 6)  # even parity, two stop bits: 12 bits
    uart.write(FIFO, 0x41)
    uart.write(FIFO, 0x42)
    data, stamps = collect(uart, port, 300_000)
    assert data == b"AB"
    assert 103 <= stamps[1] - stamps[0] <= 105  # 12 bit periods of 8.68 us


def test_the_tx_fifo_holds_64_bytes(uart: Bench) -> None:
    port = configure(uart)
    for index in range(70):
        uart.write(FIFO, 0x30 + index % 10)
    # One character is on the line at once; 64 wait; 5 are lost.
    assert uart.read(SR) & TFUL
    assert uart.read(ISR) & TOVR
    data, _ = collect(uart, port, 66 * CHARACTER_NS)
    assert len(data) == 65


def test_received_characters_arrive_at_line_rate(uart: Bench) -> None:
    port = configure(uart)
    uart.write(RXWM, 2)
    uart.execute(f'{port} Write "414243"')
    uart.run_ns(CHARACTER_NS + 1000)
    assert uart.read(SR) & REMPTY == 0
    assert uart.read(ISR) & RTRIG == 0  # one character, trigger level two
    uart.run_ns(2 * CHARACTER_NS)
    assert uart.read(ISR) & RTRIG
    assert [uart.read(FIFO) for _ in range(3)] == [0x41, 0x42, 0x43]
    assert uart.read(SR) & REMPTY


def test_the_receiver_timeout(uart: Bench) -> None:
    port = configure(uart)
    uart.write(RXTOUT, 10)  # 40 bit periods: 347 us after the last character
    uart.execute(f'{port} Write "41"')
    uart.run_ns(CHARACTER_NS + 300_000)
    assert uart.read(ISR) & TIMEOUT == 0
    uart.run_ns(60_000)
    assert uart.read(ISR) & TIMEOUT


def test_a_disabled_receiver_drops_characters(uart: Bench) -> None:
    port = configure(uart)
    uart.write(CR, RX_DIS | TX_EN)
    uart.execute(f'{port} Write "41"')
    uart.run_ns(2 * CHARACTER_NS)
    assert uart.read(SR) & REMPTY


def test_local_loopback_keeps_characters_inside(uart: Bench) -> None:
    port = configure(uart, mode=NO_PARITY | LOCAL_LOOPBACK)
    uart.write(FIFO, 0x5A)
    data, _ = collect(uart, port, 2 * CHARACTER_NS)
    assert data == b""
    assert uart.read(FIFO) == 0x5A


def test_automatic_echo_returns_what_arrives(uart: Bench) -> None:
    port = configure(uart, mode=NO_PARITY | AUTOMATIC_ECHO)
    uart.execute(f'{port} Write "6162"')
    data, _ = collect(uart, port, 3 * CHARACTER_NS)
    assert data == b"ab"
    assert [uart.read(FIFO) for _ in range(2)] == [0x61, 0x62]


def test_receive_errors_are_reported_per_byte(uart: Bench) -> None:
    configure(uart)
    uart.execute('uart0 InjectReceiveError 0x41 "parity"')
    uart.execute('uart0 InjectReceiveError 0x42 "framing"')
    uart.run_ns(3 * CHARACTER_NS)
    assert uart.read(ISR) & (PARE | FRAME) == PARE | FRAME
    assert uart.read(RXBS) & 0x3F == 0b010_001  # byte 0 parity, byte 1 framing


def test_resets_empty_the_fifos(uart: Bench) -> None:
    port = configure(uart)
    uart.execute(f'{port} Write "4142"')
    uart.run_ns(3 * CHARACTER_NS)
    uart.write(CR, RX_EN | TX_EN | RXRST)
    assert uart.read(SR) & REMPTY
    assert uart.read(CR) & RXRST == 0  # self-clearing


def test_one_byte_to_an_idle_transmitter_latches_tx_empty(uart: Bench) -> None:
    # The byte passes through the FIFO into the transmitter: an empty ->
    # not empty -> empty transition (drivers clear TEMPTY, write, then enable it).
    configure(uart)
    uart.write(ISR, 0x3FFF)
    uart.write(FIFO, 0x41)
    assert uart.read(ISR) & TEMPTY


def test_the_interrupt_follows_the_mask(uart: Bench) -> None:
    configure(uart)
    uart.write(ISR, 0x3FFF)  # clear what reset and configuration latched
    uart.write(IER, TEMPTY)
    assert uart.read(IMR) == TEMPTY
    uart.write(FIFO, 0x41)
    uart.write(FIFO, 0x42)
    uart.write(ISR, TEMPTY)  # the first byte's pass through the FIFO latched it
    assert not uart.flag("probe Level 0")  # the second byte waits in the FIFO
    uart.run_ns(CHARACTER_NS + 1000)  # and leaves it
    assert uart.flag("probe Level 0")
    uart.write(ISR, TEMPTY)
    assert not uart.flag("probe Level 0")
    uart.write(IDR, TEMPTY)
    assert uart.read(IMR) == 0
