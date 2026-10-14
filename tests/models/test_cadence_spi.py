"""Cadence SPI controller (Jig.Cadence.CadenceSPI, Zynq-7000 SPI0/1) with the
fixture flash on chip select 0: reset values (UG585 v1.7 B.30), byte timing
from the prescaler, manual and automatic chip select, manual start, decoded
slave select, FIFO status bits and the interrupt."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

SPI = 0xE0006000
CR, ISR, IER, IDR, IMR, EN, DELAY = (SPI + o for o in (0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18))
TXD, RXD, SICR, TXWR, RXWR, MOD_ID = (SPI + o for o in (0x1C, 0x20, 0x24, 0x28, 0x2C, 0xFC))

MSTREN = 1 << 0
PERI_SEL = 1 << 9  # decoded slave select
MANUAL_CS = 1 << 14
MAN_START_EN = 1 << 15
MAN_START = 1 << 16

RX_OVERFLOW, TX_NOT_FULL, TX_FULL, RX_NOT_EMPTY, RX_FULL = 1 << 0, 1 << 2, 1 << 3, 1 << 4, 1 << 5

MACHINE = """\
spi0: Jig.Cadence.CadenceSPI @ sysbus <0xE0006000, +0x1000>
    referenceClockFrequency: 160000000
    -> probe@0

flash: JigFixtures.SpiNorFlash @ spi0 0
    size: 0x100000

probe: JigFixtures.GpioProbe @ sysbus
"""


@pytest.fixture
def spi(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def chip_select(slave: int) -> int:
    """The CS field (bits 13:10) asserting one slave (active low, one-hot)."""
    return (0xF & ~(1 << slave)) << 10


def master(bench: Bench, control: int) -> None:
    bench.write(CR, control | MSTREN)
    bench.write(EN, 1)


def receive(bench: Bench) -> list[int]:
    data = []
    while bench.read(ISR) & RX_NOT_EMPTY:
        data.append(bench.read(RXD))
    return data


def test_reset_values(spi: Bench) -> None:
    expected = {CR: 0x00020000, ISR: TX_NOT_FULL, IMR: 0, EN: 0, DELAY: 0}
    expected |= {SICR: 0xFF, TXWR: 1, RXWR: 1, MOD_ID: 0x00090106}
    assert {address: spi.read(address) for address in expected} == expected


def test_read_id_with_manual_chip_select(spi: Bench) -> None:
    # Prescaler 1: 160 MHz / 4 = 40 MHz, 200 ns a byte.
    master(spi, MANUAL_CS | chip_select(0) | 1 << 3)
    for byte in (0x9F, 0, 0, 0):
        spi.write(TXD, byte)
    spi.run_ns(750)
    assert receive(spi) == [0xFF, 0xA5, 0x5A]
    spi.run_ns(100)
    assert receive(spi) == [0x14]  # capacity code 20: 1 MiB


def test_the_prescaler_sets_the_byte_time(spi: Bench) -> None:
    master(spi, MANUAL_CS | chip_select(0) | 3 << 3)  # / 16: 800 ns a byte
    spi.write(TXD, 0x9F)
    spi.run_ns(790)
    assert receive(spi) == []
    spi.run_ns(20)
    assert receive(spi) == [0xFF]


def test_automatic_chip_select_ends_the_command_with_each_burst(spi: Bench) -> None:
    master(spi, chip_select(0))
    spi.write(TXD, 0x9F)
    spi.write(TXD, 0x00)
    spi.run_ns(1000)
    spi.write(TXD, 0x9F)  # a new burst: chip select went high in between
    spi.write(TXD, 0x00)
    spi.run_ns(1000)
    assert receive(spi) == [0xFF, 0xA5, 0xFF, 0xA5]


def test_manual_start_holds_the_fifo(spi: Bench) -> None:
    control = MANUAL_CS | chip_select(0) | MAN_START_EN
    master(spi, control)
    spi.write(TXD, 0x9F)
    spi.run_ns(1000)
    assert receive(spi) == []
    spi.write(CR, control | MSTREN | MAN_START)
    spi.run_ns(1000)
    assert receive(spi) == [0xFF]


def test_an_empty_chip_select_reads_the_floating_line(spi: Bench) -> None:
    master(spi, MANUAL_CS | chip_select(1))
    spi.write(TXD, 0x9F)
    spi.write(TXD, 0x00)
    spi.run_ns(1000)
    assert receive(spi) == [0xFF, 0xFF]


def test_decoded_slave_select(spi: Bench) -> None:
    master(spi, MANUAL_CS | PERI_SEL | 0x0 << 10)  # decoded: CS field 0 selects slave 0
    spi.write(TXD, 0x9F)
    spi.write(TXD, 0x00)
    spi.run_ns(1000)
    assert receive(spi) == [0xFF, 0xA5]


def test_fifo_status_and_overflow(spi: Bench) -> None:
    master(spi, MANUAL_CS | chip_select(0))
    spi.write(TXWR, 4)
    assert spi.read(ISR) & TX_NOT_FULL
    for _ in range(129):  # the TX FIFO holds 128 bytes
        spi.write(TXD, 0)
    assert spi.read(ISR) & TX_FULL
    assert spi.read(ISR) & TX_NOT_FULL == 0
    spi.run_us(200)
    status = spi.read(ISR)
    assert status & RX_FULL
    assert status & RX_OVERFLOW == 0  # 128 bytes left the TX FIFO, 128 arrived
    spi.write(TXD, 0)
    spi.run_us(2)
    assert spi.read(ISR) & RX_OVERFLOW
    spi.write(ISR, RX_OVERFLOW)  # sticky: write one to clear
    assert spi.read(ISR) & RX_OVERFLOW == 0


def test_the_interrupt_follows_the_rx_watermark(spi: Bench) -> None:
    master(spi, MANUAL_CS | chip_select(0))
    spi.write(RXWR, 2)
    spi.write(IER, RX_NOT_EMPTY)
    assert spi.read(IMR) == RX_NOT_EMPTY
    spi.write(TXD, 0x9F)
    spi.run_ns(500)
    assert not spi.flag("probe Level 0")
    spi.write(TXD, 0x00)
    spi.run_ns(500)
    assert spi.flag("probe Level 0")
    spi.write(IDR, RX_NOT_EMPTY)
    assert not spi.flag("probe Level 0")


def test_the_delay_register_holds_its_value(spi: Bench) -> None:
    spi.write(DELAY, 0x01020304)
    assert spi.read(DELAY) == 0x01020304
