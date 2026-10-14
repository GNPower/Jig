"""Xilinx AXI Central DMA in simple mode (Jig.Xilinx.XilinxAxiCdma, PG034): a
transfer between two memories with its duration, the completion flag and
interrupt, a decode error outside the reachable address map, and the soft
reset."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

CDMA = 0x40020000
CDMACR, CDMASR, SA, SA_MSB, DA, DA_MSB, BTT = (
    CDMA + o for o in (0x00, 0x04, 0x18, 0x1C, 0x20, 0x24, 0x28)
)
RESET, IOC_IRQ_EN, ERR_IRQ_EN = 1 << 2, 1 << 12, 1 << 14
IDLE, DMA_DEC_ERR, IOC_IRQ, ERR_IRQ = 1 << 1, 1 << 6, 1 << 12, 1 << 14

SOURCE, DESTINATION, ELSEWHERE = 0x10000000, 0x20000000, 0x30000000
DATA = bytes(range(256))

MACHINE = """\
cdma: Jig.Xilinx.XilinxAxiCdma @ sysbus <0x40020000, +0x1000>
    reachableRanges: "0x10000000:0x10000, 0x20000000:0x10000"
    dataBytesPerSecond: 100000000
    -> probe@0

source: Memory.MappedMemory @ sysbus 0x10000000
    size: 0x10000

destination: Memory.MappedMemory @ sysbus 0x20000000
    size: 0x10000

elsewhere: Memory.MappedMemory @ sysbus 0x30000000
    size: 0x10000

probe: JigFixtures.GpioProbe @ sysbus
"""


@pytest.fixture
def cdma(bench: Bench) -> Bench:
    bench.load(MACHINE)
    bench.write_bytes(SOURCE, DATA)
    return bench


def transfer(bench: Bench, source: int, destination: int, count: int) -> None:
    bench.write(SA, source)
    bench.write(DA, destination)
    bench.write(BTT, count)


def test_reset_values(cdma: Bench) -> None:
    assert cdma.read(CDMACR) == 0
    assert cdma.read(CDMASR) == IDLE
    assert cdma.read(BTT) == 0


def test_a_transfer_copies_at_the_data_rate(cdma: Bench) -> None:
    transfer(cdma, SOURCE, DESTINATION, len(DATA))  # 256 bytes at 100 MB/s: 2.56 us
    assert cdma.read(CDMASR) & IDLE == 0
    cdma.run_ns(2500)
    assert cdma.read(CDMASR) & IDLE == 0
    cdma.run_ns(100)
    assert cdma.read(CDMASR) == IDLE | IOC_IRQ
    assert cdma.read_bytes(DESTINATION, len(DATA)) == DATA


def test_completion_raises_the_interrupt(cdma: Bench) -> None:
    cdma.write(CDMACR, IOC_IRQ_EN)
    transfer(cdma, SOURCE, DESTINATION, 16)
    cdma.run_us(1)
    assert cdma.flag("probe Level 0")
    cdma.write(CDMASR, IOC_IRQ)  # write one to clear
    assert cdma.read(CDMASR) == IDLE
    assert not cdma.flag("probe Level 0")


def test_an_unreachable_address_ends_in_a_decode_error(cdma: Bench) -> None:
    cdma.write(CDMACR, ERR_IRQ_EN)
    transfer(cdma, SOURCE, ELSEWHERE, 16)
    cdma.run_us(1)
    assert cdma.read(CDMASR) == IDLE | DMA_DEC_ERR | ERR_IRQ
    assert cdma.flag("probe Level 0")
    assert cdma.read_bytes(ELSEWHERE, 16) == bytes(16)


def test_the_soft_reset_abandons_a_transfer(cdma: Bench) -> None:
    transfer(cdma, SOURCE, DESTINATION, len(DATA))
    cdma.write(CDMACR, RESET)
    assert cdma.read(CDMACR) == 0  # self-clearing
    assert cdma.read(CDMASR) == IDLE
    cdma.run_us(5)
    assert cdma.read_bytes(DESTINATION, 4) == bytes(4)


def test_a_new_count_while_busy_is_ignored(cdma: Bench) -> None:
    transfer(cdma, SOURCE, DESTINATION, 16)
    cdma.write(BTT, 32)
    cdma.run_us(1)
    assert cdma.read_bytes(DESTINATION, 32) == DATA[:16] + bytes(16)


def test_address_msb_registers_hold_their_values(cdma: Bench) -> None:
    cdma.write(SA_MSB, 0x1)
    cdma.write(DA_MSB, 0x2)
    assert (cdma.read(SA_MSB), cdma.read(DA_MSB)) == (1, 2)
