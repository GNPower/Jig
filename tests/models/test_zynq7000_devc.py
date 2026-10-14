"""Zynq-7000 device configuration interface (Jig.Xilinx.Zynq7000_DevC): reset
values (UG585 v1.7 B.16), PL initialisation through PCFG_PROG_B, a PCAP load
of a dummy bitstream with its timing and done flags, the error flags, the
interrupt mask, memory-to-memory DMA, JTAG configuration and MULTIBOOT_ADDR
across resets."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

DEVC = 0xF8007000
CTRL = DEVC + 0x00
LOCK = DEVC + 0x04
CFG = DEVC + 0x08
INT_STS = DEVC + 0x0C
INT_MASK = DEVC + 0x10
STATUS = DEVC + 0x14
DMA_SRC_ADDR = DEVC + 0x18
DMA_DST_ADDR = DEVC + 0x1C
DMA_SRC_LEN = DEVC + 0x20
DMA_DST_LEN = DEVC + 0x24
MULTIBOOT_ADDR = DEVC + 0x2C
UNLOCK = DEVC + 0x34
MCTRL = DEVC + 0x80

PCFG_PROG_B = 1 << 30
PCAP_PR = 1 << 27
PCAP_MODE = 1 << 26
PCAP_RATE_EN = 1 << 25

DMA_CMD_ERR = 1 << 15
DMA_Q_OV = 1 << 14
DMA_DONE = 1 << 13
D_P_DONE = 1 << 12
PCFG_DONE = 1 << 2
PCFG_INIT_PE = 1 << 1
PCFG_INIT_NE = 1 << 0

DMA_CMD_Q_E = 1 << 30
DMA_DONE_CNT = 0x3 << 28
PCFG_INIT = 1 << 4
CONFIGURED = 0x7 << 8  # PSS_GPWRDWN_B, PSS_GTS_CFG_B, PSS_FST_CFG_B (bits 9, 8, 10)

PCAP = 0xFFFFFFFF
BITSTREAM_AT = 0x1000
BITSTREAM = bytes((i * 31 + 5) & 0xFF for i in range(4096))  # 1024 words

MACHINE = """\
slcr: Jig.Xilinx.Zynq7000_SLCR @ sysbus <0xF8000000, +0x1000>

devc: Jig.Xilinx.Zynq7000_DevC @ sysbus <0xF8007000, +0x100>
    slcr: slcr
    pcapClockFrequency: 100000000
    -> probe@0

ocm: Memory.MappedMemory @ sysbus 0x0
    size: 0x30000

probe: JigFixtures.GpioProbe @ sysbus
"""


@pytest.fixture
def devc(bench: Bench) -> Bench:
    bench.load(MACHINE)
    bench.write_bytes(BITSTREAM_AT, BITSTREAM)
    return bench


def initialise_pl(bench: Bench, control: int = PCAP_PR | PCAP_MODE) -> None:
    """PROG_B high, low, high: the sequence the FSBL runs before a PCAP load."""
    bench.write(CTRL, control | PCFG_PROG_B)
    bench.write(CTRL, control)
    bench.write(CTRL, control | PCFG_PROG_B)


def pcap_load(bench: Bench, source: int = BITSTREAM_AT, words: int = len(BITSTREAM) // 4) -> None:
    bench.write(DMA_SRC_ADDR, source | 1)  # bit 0: the drivers' last-transfer flag
    bench.write(DMA_DST_ADDR, PCAP)
    bench.write(DMA_SRC_LEN, words)
    bench.write(DMA_DST_LEN, words)


def test_reset_values(devc: Bench) -> None:
    expected = {
        CTRL: 0x0C006000,
        LOCK: 0,
        CFG: 0x00000508,
        INT_STS: 0,
        INT_MASK: 0xFFFFFFFF,
        STATUS: 0x40000820,
        MULTIBOOT_ADDR: 0,
        UNLOCK: 0,
    }
    assert {address: devc.read(address) for address in expected} == expected
    # MCTRL: PS_VERSION in [31:28] (the psVersion parameter, 3 by default) and
    # PCFG_POR_B (bit 8: the PL has power).
    assert devc.read(MCTRL) == 0x30000100


def test_prog_b_initialises_the_pl(devc: Bench) -> None:
    devc.write(CTRL, PCAP_PR | PCAP_MODE | PCFG_PROG_B)
    assert devc.read(INT_STS) == PCFG_INIT_PE
    assert devc.read(STATUS) & PCFG_INIT
    devc.write(CTRL, PCAP_PR | PCAP_MODE)
    assert devc.read(INT_STS) == PCFG_INIT_PE | PCFG_INIT_NE
    assert devc.read(STATUS) & PCFG_INIT == 0
    devc.write(INT_STS, PCFG_INIT_PE | PCFG_INIT_NE)  # write one to clear
    assert devc.read(INT_STS) == 0


def test_a_pcap_load_configures_the_pl(devc: Bench) -> None:
    initialise_pl(devc)
    devc.write(INT_STS, 0xFFFFFFFF)
    pcap_load(devc)
    # 1024 words at 32 bits per 100 MHz PCAP clock: 10.24 us.
    devc.run_us(10)
    assert devc.read(INT_STS) & DMA_DONE == 0
    assert devc.read(STATUS) & DMA_CMD_Q_E == 0
    devc.run_us(1)
    assert devc.read(INT_STS) == DMA_DONE | D_P_DONE | PCFG_DONE
    status = devc.read(STATUS)
    assert status & DMA_CMD_Q_E and status & PCFG_INIT
    assert status & DMA_DONE_CNT == 1 << 28
    assert status & CONFIGURED == CONFIGURED
    assert devc.flag("devc PlConfigured")
    assert devc.text("devc ConfiguredBitstreamSha256") == hashlib.sha256(BITSTREAM).hexdigest()
    devc.write(STATUS, DMA_DONE_CNT)  # writing ones clears the done count
    assert devc.read(STATUS) & DMA_DONE_CNT == 0


def test_pcap_rate_enable_quarters_the_rate(devc: Bench) -> None:
    initialise_pl(devc, PCAP_PR | PCAP_MODE | PCAP_RATE_EN)
    pcap_load(devc)
    devc.run_us(40)
    assert devc.read(INT_STS) & DMA_DONE == 0
    devc.run_us(2)
    assert devc.read(INT_STS) & DMA_DONE


def test_prog_b_low_clears_the_configuration(devc: Bench) -> None:
    initialise_pl(devc)
    pcap_load(devc)
    devc.run_us(20)
    assert devc.flag("devc PlConfigured")
    devc.write(CTRL, PCAP_PR | PCAP_MODE)
    assert not devc.flag("devc PlConfigured")
    assert devc.read(STATUS) & (CONFIGURED | PCFG_INIT) == 0


def test_data_sent_before_initialisation_configures_nothing(devc: Bench) -> None:
    pcap_load(devc)  # PROG_B is still low after reset
    devc.run_us(20)
    assert devc.read(INT_STS) == DMA_DONE | D_P_DONE
    assert not devc.flag("devc PlConfigured")


def test_pcap_writes_need_pcap_mode(devc: Bench) -> None:
    devc.write(CTRL, PCFG_PROG_B)
    pcap_load(devc)
    assert devc.read(INT_STS) & DMA_CMD_ERR


def test_pcap_readback_is_refused(devc: Bench) -> None:
    devc.write(DMA_SRC_ADDR, PCAP)
    devc.write(DMA_DST_ADDR, 0x8000)
    devc.write(DMA_SRC_LEN, 4)
    devc.write(DMA_DST_LEN, 4)
    assert devc.read(INT_STS) & DMA_CMD_ERR


def test_a_second_command_while_busy_overflows_the_queue(devc: Bench) -> None:
    initialise_pl(devc)
    pcap_load(devc)
    pcap_load(devc)
    assert devc.read(INT_STS) & DMA_Q_OV


def test_the_interrupt_follows_the_mask(devc: Bench) -> None:
    initialise_pl(devc)
    devc.write(INT_STS, 0xFFFFFFFF)
    devc.write(INT_MASK, ~DMA_DONE & 0xFFFFFFFF)
    pcap_load(devc)
    devc.run_us(20)
    assert devc.flag("probe Level 0")
    devc.write(INT_STS, DMA_DONE)
    assert not devc.flag("probe Level 0")


def test_memory_to_memory_dma(devc: Bench) -> None:
    devc.write(DMA_SRC_ADDR, BITSTREAM_AT)
    devc.write(DMA_DST_ADDR, 0x8000)
    devc.write(DMA_SRC_LEN, 4)
    devc.write(DMA_DST_LEN, 4)
    devc.run_us(1)
    assert devc.read(INT_STS) == DMA_DONE
    assert devc.read_bytes(0x8000, 16) == BITSTREAM[:16]


def test_jtag_configuration_records_the_bitstream(devc: Bench, tmp_path: Path) -> None:
    bitstream = tmp_path / "design.bit"
    bitstream.write_bytes(BITSTREAM[:1000])
    devc.execute(f'devc ConfigureFromJtag "{bitstream.as_posix()}"')
    assert devc.flag("devc PlConfigured")
    assert (
        devc.text("devc ConfiguredBitstreamSha256") == hashlib.sha256(BITSTREAM[:1000]).hexdigest()
    )
    assert devc.read(INT_STS) & PCFG_DONE


def test_lock_bits_stay_set_until_reset(devc: Bench) -> None:
    devc.write(LOCK, 0x1A)
    devc.write(LOCK, 0)
    assert devc.read(LOCK) == 0x1A


def test_multiboot_address_survives_all_but_a_power_on_reset(devc: Bench) -> None:
    devc.write(MULTIBOOT_ADDR, 0x2005)  # [12:0] only
    assert devc.read(MULTIBOOT_ADDR) == 0x0005
    devc.execute("slcr OnGPIO 1 true")  # PS_SRST_B: a system reset
    devc.run_us(1)
    assert devc.read(MULTIBOOT_ADDR) == 5
    assert devc.read(CTRL) == 0x0C006000
    devc.execute("slcr OnGPIO 0 true")  # PS_POR_B
    devc.run_us(1)
    assert devc.read(MULTIBOOT_ADDR) == 0
