"""Zynq-7000 Quad-SPI controller (Jig.Xilinx.Zynq7000_QSPI) with two fixture
flash dies: reset values (UG585 v1.7 B.26), I/O mode transfers and their
timing, manual chip select and start, quad data phases, linear reads, the
dual-stacked page select, FIFO status and interrupts, and the SLCR reset."""

from __future__ import annotations

from pathlib import Path

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

QSPI = 0xE000D000
CR = QSPI + 0x00
ISR = QSPI + 0x04
IER = QSPI + 0x08
IDR = QSPI + 0x0C
IMR = QSPI + 0x10
EN = QSPI + 0x14
DELAY = QSPI + 0x18
TXD0 = QSPI + 0x1C
RXD = QSPI + 0x20
SICR = QSPI + 0x24
TXWR = QSPI + 0x28
RXWR = QSPI + 0x2C
GPIO = QSPI + 0x30
LPBK_DLY_ADJ = QSPI + 0x38
TXD1 = QSPI + 0x80
TXD2 = QSPI + 0x84
TXD3 = QSPI + 0x88
LQSPI_CFG = QSPI + 0xA0
LQSPI_STS = QSPI + 0xA4
MOD_ID = QSPI + 0xFC
LINEAR = 0xFC000000

MSTREN = 1 << 0
FIFO_WIDTH_32 = 0x3 << 6
PCS = 1 << 10
SSFORCE = 1 << 14
MANSTRTEN = 1 << 15
MANSTRT = 1 << 16
IFMODE = 1 << 31

RX_OVERFLOW = 1 << 0
TX_NOT_FULL = 1 << 2  # TX FIFO below its watermark
RX_NOT_EMPTY = 1 << 4
RX_FULL = 1 << 5

LQ_MODE = 1 << 31
TWO_MEM = 1 << 30
U_PAGE = 1 << 28

DIE = 0x100000  # 1 MiB dies keep the image files small
MACHINE = """\
slcr: Jig.Xilinx.Zynq7000_SLCR @ sysbus <0xF8000000, +0x1000>

qspi: Jig.Xilinx.Zynq7000_QSPI @ {
    sysbus new Bus.BusMultiRegistration { address: 0xE000D000; size: 0x1000; region: "registers" };
    sysbus new Bus.BusMultiRegistration { address: 0xFC000000; size: 0x2000000; region: "linear" }
    }
    referenceClockFrequency: 200000000
    slcr: slcr
    -> probe@0

flash0: JigFixtures.SpiNorFlash @ qspi 0
    size: 0x100000
    manufacturerId: 0xA5
    memoryType: 0x5A

flash1: JigFixtures.SpiNorFlash @ qspi 1
    size: 0x100000
    manufacturerId: 0xC3
    memoryType: 0x3C

probe: JigFixtures.GpioProbe @ sysbus
"""


def contents(die: int) -> bytes:
    return bytes((i * 13 + 7 * die + 1) & 0xFF for i in range(DIE))


@pytest.fixture
def qspi(bench: Bench, tmp_path: Path) -> Bench:
    bench.load(MACHINE)
    for die in (0, 1):
        image = tmp_path / f"die{die}.img"
        image.write_bytes(contents(die))
        bench.execute(f'qspi.flash{die} AttachImage "{image.as_posix()}"')
    return bench


def io_mode(bench: Bench, *, divider: int = 0, manual_start: bool = False) -> int:
    """I/O mode with manual chip select, asserted; returns the CR value."""
    bench.write(LQSPI_CFG, 0x0000016B)  # linear mode off
    control = IFMODE | FIFO_WIDTH_32 | SSFORCE | MSTREN | divider << 3
    control |= MANSTRTEN if manual_start else 0
    bench.write(CR, control | PCS)
    bench.write(EN, 1)
    bench.write(CR, control)
    return control


def drain(bench: Bench) -> list[int]:
    words = []
    while bench.read(ISR) & RX_NOT_EMPTY:
        words.append(bench.read(RXD))
    return words


def test_reset_values(qspi: Bench) -> None:
    expected = {
        CR: 0x80020000,
        ISR: TX_NOT_FULL,
        IMR: 0,
        EN: 0,
        DELAY: 0,
        SICR: 0xFF,
        TXWR: 1,
        RXWR: 1,
        GPIO: 1,
        LPBK_DLY_ADJ: 0x33,
        # UG585 B.26's field resets read 0x03A002EB; the model starts where a
        # Quad-SPI boot leaves the controller (see the model's header).
        LQSPI_CFG: 0x8000016B,
        LQSPI_STS: 0,
        MOD_ID: 0x01090101,
    }
    assert {address: qspi.read(address) for address in expected} == expected


def test_read_id_in_io_mode(qspi: Bench) -> None:
    io_mode(qspi)
    qspi.write(TXD0, 0x0000009F)  # 0x9F then three bytes, least significant first
    assert drain(qspi) == []  # 32 clocks at 100 MHz have not passed yet
    qspi.run_ns(400)
    # Each byte shifts in from the top: the ID lands in bytes 1-3.
    assert drain(qspi) == [0x145AA5FF]  # capacity code 20: 1 MiB


def test_short_entries_fill_the_top_of_the_word(qspi: Bench) -> None:
    io_mode(qspi)
    qspi.write(TXD1, 0x9F)
    qspi.write(TXD3, 0)
    qspi.run_ns(1000)
    first, second = drain(qspi)
    assert first >> 24 == 0xFF
    assert second >> 8 == 0x145AA5


def test_the_baud_divider_sets_the_byte_time(qspi: Bench) -> None:
    io_mode(qspi, divider=2)  # 200 MHz / 8 = 25 MHz: 1.28 us for four bytes
    qspi.write(TXD0, 0x0000009F)
    qspi.run_ns(1200)
    assert drain(qspi) == []
    qspi.run_ns(200)
    assert drain(qspi) == [0x145AA5FF]


def test_manual_start_holds_the_fifo(qspi: Bench) -> None:
    control = io_mode(qspi, manual_start=True)
    qspi.write(TXD0, 0x0000009F)
    qspi.run_ns(1000)
    assert drain(qspi) == []
    qspi.write(CR, control | MANSTRT)
    assert qspi.read(CR) & MANSTRT == 0  # write-only
    qspi.run_ns(1000)
    assert drain(qspi) == [0x145AA5FF]


def test_releasing_chip_select_ends_the_command(qspi: Bench) -> None:
    control = io_mode(qspi)
    qspi.write(TXD1, 0x9F)
    qspi.run_ns(200)
    qspi.write(CR, control | PCS)  # chip select high: READ ID ends
    qspi.write(CR, control)
    qspi.write(TXD1, 0x9F)
    qspi.write(TXD1, 0x00)
    qspi.run_ns(500)
    assert [word >> 24 for word in drain(qspi)] == [0xFF, 0xFF, 0xA5]


def test_automatic_chip_select_frames_each_burst(qspi: Bench) -> None:
    qspi.write(LQSPI_CFG, 0x0000016B)
    qspi.write(CR, IFMODE | FIFO_WIDTH_32 | MSTREN)
    qspi.write(EN, 1)
    qspi.write(TXD0, 0x0000009F)
    qspi.run_ns(1000)
    qspi.write(TXD0, 0x0000009F)  # a new burst: a new command
    qspi.run_ns(1000)
    assert drain(qspi) == [0x145AA5FF, 0x145AA5FF]


def quad_read(bench: Bench, opcode: int, words: int, nanoseconds: int) -> int:
    """Sends a read with one dummy byte and ``words`` data words, runs for
    ``nanoseconds``, and returns the number of RX words then."""
    io_mode(bench)
    bench.write(TXD0, opcode | 0x00 << 8 | 0x01 << 16 | 0x00 << 24)  # address 0x000100
    bench.write(TXD1, 0)  # 8 dummy clocks
    for _ in range(words):
        bench.write(TXD0, 0)
    bench.run_ns(nanoseconds)
    return len(drain(bench))


def test_quad_data_phases_run_on_four_lines(qspi: Bench) -> None:
    # Command and address: 320 ns, dummy byte: 80 ns, then 80 ns a word on 4
    # lines against 320 ns on 1 (100 MHz SCLK).
    assert quad_read(qspi, 0x6B, 4, 750) == 6


def test_single_line_data_phases_take_longer(qspi: Bench) -> None:
    assert quad_read(qspi, 0x0B, 4, 750) == 3


def test_io_mode_reads_return_flash_data(qspi: Bench) -> None:
    io_mode(qspi)
    qspi.write(TXD0, 0x6B | 0x00 << 8 | 0x01 << 16 | 0x00 << 24)
    qspi.write(TXD1, 0)
    qspi.write(TXD0, 0)
    qspi.write(TXD0, 0)
    qspi.run_ns(2000)
    data = b"".join(word.to_bytes(4, "little") for word in drain(qspi)[2:])
    assert data == contents(0)[0x100:0x108]


def test_linear_reads_use_the_configured_instruction(qspi: Bench) -> None:
    expected = contents(0)
    assert qspi.read(LINEAR + 0x200) == int.from_bytes(expected[0x200:0x204], "little")
    assert qspi.read_word(LINEAR + 0x202) == int.from_bytes(expected[0x202:0x204], "little")
    assert qspi.read_byte(LINEAR + 0x2FF) == expected[0x2FF]
    for instruction in (0x03, 0x0B, 0x3B, 0x6B):
        qspi.write(LQSPI_CFG, LQ_MODE | instruction)
        assert qspi.read(LINEAR + 0x10) == int.from_bytes(expected[0x10:0x14], "little")


def test_linear_window_is_read_only_and_needs_linear_mode(qspi: Bench) -> None:
    qspi.write(LINEAR + 0x40, 0x12345678)
    assert qspi.read(LQSPI_STS) == 1 << 1  # WR_RECVD
    qspi.write(LQSPI_STS, 1 << 1)
    assert qspi.read(LQSPI_STS) == 0
    qspi.write(LQSPI_CFG, 0x0000016B)  # linear mode off
    assert qspi.read(LINEAR + 0x40) == 0


def test_dual_stacked_dies(qspi: Bench) -> None:
    # Linear mode: address bit 24 picks the upper die.
    qspi.write(LQSPI_CFG, LQ_MODE | TWO_MEM | 0x6B)
    assert qspi.read_byte(LINEAR + 0x33) == contents(0)[0x33]
    assert qspi.read_byte(LINEAR + 0x1000033) == contents(1)[0x33]
    # I/O mode: U_PAGE sends commands to the upper die.
    control = io_mode(qspi)
    qspi.write(LQSPI_CFG, TWO_MEM | U_PAGE | 0x6B)
    qspi.write(CR, control | PCS)
    qspi.write(CR, control)
    qspi.write(TXD0, 0x0000009F)
    qspi.run_ns(400)
    assert drain(qspi) == [0x143CC3FF]


def test_rx_overflow_is_sticky_until_cleared(qspi: Bench) -> None:
    io_mode(qspi)
    qspi.write(TXD1, 0x9F)
    for _ in range(62):
        qspi.write(TXD1, 0)
    qspi.run_ns(63 * 80 + 100)
    assert qspi.read(ISR) & RX_FULL
    qspi.write(TXD1, 0)
    qspi.run_ns(200)
    assert qspi.read(ISR) & RX_OVERFLOW
    qspi.write(ISR, RX_OVERFLOW)
    assert qspi.read(ISR) & RX_OVERFLOW == 0


def test_rx_watermark_drives_the_interrupt(qspi: Bench) -> None:
    io_mode(qspi)
    qspi.write(RXWR, 2)
    qspi.write(IER, RX_NOT_EMPTY)
    assert qspi.read(IMR) == RX_NOT_EMPTY
    qspi.write(TXD0, 0x0000009F)
    qspi.run_ns(400)
    assert not qspi.flag("probe Level 0")  # one word, watermark two
    qspi.write(TXD0, 0)
    qspi.run_ns(400)
    assert qspi.flag("probe Level 0")
    qspi.write(IDR, RX_NOT_EMPTY)
    assert not qspi.flag("probe Level 0")
    assert qspi.read(IMR) == 0


def test_lqspi_rst_ctrl_resets_the_controller(qspi: Bench) -> None:
    io_mode(qspi)
    qspi.write(TXWR, 9)
    qspi.write(0xF8000008, 0xDF0D)  # SLCR unlock
    qspi.write(0xF8000230, 1)  # LQSPI_CPU1X_RST
    qspi.write(0xF8000230, 0)
    assert qspi.read(CR) == 0x80020000
    assert qspi.read(TXWR) == 1
    assert qspi.read(EN) == 0
    assert qspi.read(LQSPI_CFG) == 0x8000016B
