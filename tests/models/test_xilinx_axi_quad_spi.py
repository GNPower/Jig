"""Xilinx AXI Quad SPI v3.2 in standard SPI master mode
(Jig.Xilinx.XilinxAxiQuadSpi, PG153) with the fixture flash on slave select
0: reset values, a READ ID through manual slave select with its byte timing,
the master transaction inhibit, automatic slave select, loopback, LSB-first
order, the FIFOs and their occupancy, the soft reset and the interrupts.

The SPICR (0x180) and SPISSR (all ones) reset values are the model's reading
of PG153; they are still to be checked against the generated IP."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

QSPI = 0x40030000
DGIER, IPISR, IPIER, SRR = QSPI + 0x1C, QSPI + 0x20, QSPI + 0x28, QSPI + 0x40
SPICR, SPISR, SPIDTR, SPIDRR, SPISSR = (QSPI + o for o in (0x60, 0x64, 0x68, 0x6C, 0x70))
TX_OCCUPANCY, RX_OCCUPANCY = QSPI + 0x74, QSPI + 0x78

LOOP, SPE, MASTER = 1 << 0, 1 << 1, 1 << 2
TX_FIFO_RESET, RX_FIFO_RESET, MANUAL_SS, INHIBIT, LSB_FIRST = (1 << n for n in (5, 6, 7, 8, 9))
RX_EMPTY, RX_FULL, TX_EMPTY, TX_FULL, SLAVE_MODE_SELECT = (1 << n for n in (0, 1, 2, 3, 5))
DTR_EMPTY, DRR_FULL, DRR_OVERRUN, DRR_NOT_EMPTY = 1 << 2, 1 << 4, 1 << 5, 1 << 8
GLOBAL_ENABLE = 1 << 31

MACHINE = """\
axiQuadSpi: Jig.Xilinx.XilinxAxiQuadSpi @ sysbus <0x40030000, +0x80>
    spiClockFrequency: 10000000
    fifoDepth: 16
    -> probe@0

flash: JigFixtures.SpiNorFlash @ axiQuadSpi 0
    size: 0x100000

probe: JigFixtures.GpioProbe @ sysbus
"""

BYTE_NS = 800  # 8 bits at 10 MHz


@pytest.fixture
def spi(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def receive(bench: Bench) -> list[int]:
    data = []
    while not bench.read(SPISR) & RX_EMPTY:
        data.append(bench.read(SPIDRR))
    return data


def exchange(bench: Bench, control: int, data: bytes) -> list[int]:
    """Queues ``data`` with the transaction inhibited, then releases it."""
    bench.write(SPICR, control | INHIBIT)
    for byte in data:
        bench.write(SPIDTR, byte)
    bench.write(SPICR, control)
    bench.run_ns(BYTE_NS * len(data) + 100)
    return receive(bench)


def test_reset_values(spi: Bench) -> None:
    assert spi.read(SPICR) == 0x180  # manual slave select, transaction inhibited
    assert spi.read(SPISSR) == 0xFFFFFFFF
    assert spi.read(SPISR) == RX_EMPTY | TX_EMPTY | SLAVE_MODE_SELECT
    assert (spi.read(DGIER), spi.read(IPISR), spi.read(IPIER)) == (0, 0, 0)
    assert (spi.read(TX_OCCUPANCY), spi.read(RX_OCCUPANCY)) == (0, 0)


def test_read_id_with_manual_slave_select(spi: Bench) -> None:
    spi.write(SPISSR, 0xFFFFFFFE)
    assert exchange(spi, SPE | MASTER | MANUAL_SS, b"\x9f\x00\x00\x00") == [0xFF, 0xA5, 0x5A, 0x14]


def test_the_inhibit_holds_queued_bytes(spi: Bench) -> None:
    spi.write(SPISSR, 0xFFFFFFFE)
    spi.write(SPICR, SPE | MASTER | MANUAL_SS | INHIBIT)
    for byte in (0x9F, 0, 0):
        spi.write(SPIDTR, byte)
    assert spi.read(TX_OCCUPANCY) == 2  # occupancy reads count - 1
    spi.run_us(10)
    assert receive(spi) == []
    spi.write(SPICR, SPE | MASTER | MANUAL_SS)
    spi.run_ns(BYTE_NS * 3 - 100)
    assert spi.read(RX_OCCUPANCY) == 1  # two bytes so far
    spi.run_ns(200)
    assert receive(spi) == [0xFF, 0xA5, 0x5A]


def test_automatic_slave_select_frames_each_burst(spi: Bench) -> None:
    spi.write(SPISSR, 0xFFFFFFFE)
    control = SPE | MASTER
    assert exchange(spi, control, b"\x9f\x00") == [0xFF, 0xA5]
    assert exchange(spi, control, b"\x9f\x00") == [0xFF, 0xA5]  # a new command


def test_loopback_returns_the_transmitted_bytes(spi: Bench) -> None:
    assert exchange(spi, SPE | MASTER | MANUAL_SS | LOOP, b"\x12\x34") == [0x12, 0x34]


def test_lsb_first_reverses_each_byte_on_the_wire(spi: Bench) -> None:
    spi.write(SPISSR, 0xFFFFFFFE)
    control = SPE | MASTER | MANUAL_SS | LSB_FIRST
    # 0xF9 goes out as 0x9F; the capacity code 0x14 comes back as 0x28.
    assert exchange(spi, control, b"\xf9\x00\x00\x00") == [0xFF, 0xA5, 0x5A, 0x28]


def test_the_fifos_and_their_resets(spi: Bench) -> None:
    spi.write(SPICR, SPE | MASTER | MANUAL_SS | INHIBIT)
    for byte in range(17):  # 16 entries fit
        spi.write(SPIDTR, byte)
    assert spi.read(SPISR) & TX_FULL
    assert spi.read(TX_OCCUPANCY) == 15
    spi.write(SPICR, SPE | MASTER | MANUAL_SS | INHIBIT | TX_FIFO_RESET)
    assert spi.read(SPICR) & TX_FIFO_RESET == 0  # self-clearing
    assert spi.read(SPISR) & TX_EMPTY


def test_the_rx_fifo_fills_then_overruns(spi: Bench) -> None:
    control = SPE | MASTER | MANUAL_SS | LOOP
    spi.write(SPICR, control | INHIBIT)
    for byte in range(16):
        spi.write(SPIDTR, byte)
    spi.write(SPICR, control)
    spi.run_ns(BYTE_NS * 16 + 100)
    assert spi.read(SPISR) & RX_FULL
    assert spi.read(IPISR) & DRR_FULL
    assert spi.read(RX_OCCUPANCY) == 15
    spi.write(SPIDTR, 0x99)
    spi.run_ns(BYTE_NS + 100)
    assert spi.read(IPISR) & DRR_OVERRUN
    assert receive(spi) == list(range(16))  # the late byte was lost


def test_the_soft_reset_needs_its_key(spi: Bench) -> None:
    spi.write(SPISSR, 0xFFFFFFFE)
    spi.write(SRR, 0x5)
    assert spi.read(SPISSR) == 0xFFFFFFFE
    spi.write(SRR, 0x0A)
    assert spi.read(SPISSR) == 0xFFFFFFFF
    assert spi.read(SPICR) == 0x180


def test_interrupts_toggle_on_write_and_need_the_global_enable(spi: Bench) -> None:
    spi.write(IPIER, DTR_EMPTY | DRR_NOT_EMPTY)
    exchange(spi, SPE | MASTER | MANUAL_SS | LOOP, b"\x01")
    assert spi.read(IPISR) & (DTR_EMPTY | DRR_NOT_EMPTY) == DTR_EMPTY | DRR_NOT_EMPTY
    assert not spi.flag("probe Level 0")
    spi.write(DGIER, GLOBAL_ENABLE)
    assert spi.flag("probe Level 0")
    spi.write(IPISR, DTR_EMPTY | DRR_NOT_EMPTY)  # toggles the set bits off
    assert spi.read(IPISR) & (DTR_EMPTY | DRR_NOT_EMPTY) == 0
    assert not spi.flag("probe Level 0")
