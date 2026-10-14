"""Zynq-7000 programmable-logic reset domain (Jig.Xilinx.Zynq7000_PlDomain):
a core that reads an AXI slave whose FCLK reset is asserted halts (at the end
of the translation block holding the access); with the reset released the
same program runs on. Block RAM in the PL loses its contents when the PL is
cleared, configured or reset."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig.errors import RenodeError

from . import arm
from .bench import Bench

pytestmark = pytest.mark.renode

AXI_GPIO = 0x40000000
BRAM = 0x40100000
MARKER = 0x20000
DEVC_CTRL = 0xF8007000
FPGA_RST_CTRL = 0xF8000240

MACHINE = """\
cpu0: CPU.ARMv7A @ sysbus
    cpuType: "cortex-a9"

slcr: Jig.Xilinx.Zynq7000_SLCR @ sysbus <0xF8000000, +0x1000>

devc: Jig.Xilinx.Zynq7000_DevC @ sysbus <0xF8007000, +0x100>
    slcr: slcr

ocm: Memory.MappedMemory @ sysbus 0x0
    size: 0x30000

axiGpio: Jig.Xilinx.XilinxAxiGpio @ sysbus <0x40000000, +0x200>

bram: Memory.MappedMemory @ sysbus 0x40100000
    size: 0x2000

pl: Jig.Xilinx.Zynq7000_PlDomain @ sysbus
    devc: devc
    slcr: slcr
    cpu: cpu0
    init:
        AddPeripheral sysbus.axiGpio 0
        AddMemory sysbus.bram
"""

# Reads the AXI GPIO data register, branches (ending the translation block),
# then stores a marker and loops.
PROGRAM = arm.words(
    arm.ldr_literal(0, 20),  # 0x00: r0 = AXI_GPIO (the word at 0x1C)
    arm.ldr(1, 0),  # 0x04: the AXI read
    0xEAFFFFFF,  # 0x08: B 0x0C, the end of the block
    arm.ldr_literal(2, 12),  # 0x0C: r2 = MARKER (0x20)
    arm.ldr_literal(3, 12),  # 0x10: r3 = 0x600DF00D (0x24)
    arm.str_(3, 2),  # 0x14
    arm.branch_to_self(),  # 0x18
    AXI_GPIO,
    MARKER,
    0x600DF00D,
)


@pytest.fixture
def pl(bench: Bench) -> Bench:
    bench.load(MACHINE)
    bench.write_bytes(0, PROGRAM)  # the core starts at 0 when time first runs
    return bench


def release_fclk_resets(bench: Bench) -> None:
    bench.write(0xF8000008, 0xDF0D)  # SLCR unlock
    bench.write(FPGA_RST_CTRL, 0)


def test_an_access_to_a_slave_in_reset_halts_the_core(pl: Bench) -> None:
    assert pl.read(FPGA_RST_CTRL) & 1  # FCLK_RESET0_N asserted after reset
    pl.run_us(200)
    assert pl.flag("cpu0 IsHalted")
    # The halt lands where Renode leaves the translation block holding the
    # access: the next block, with the marker store, never runs.
    assert pl.number("cpu0 PC") == 0x0C
    assert pl.read(MARKER) == 0


def test_with_the_reset_released_the_access_completes(pl: Bench) -> None:
    release_fclk_resets(pl)
    pl.run_us(200)
    assert not pl.flag("cpu0 IsHalted")
    assert pl.read(MARKER) == 0x600DF00D
    assert pl.number("cpu0 PC") == 0x18


def test_a_slave_on_another_fclk_reset_is_unaffected(bench: Bench) -> None:
    bench.load(MACHINE.replace("AddPeripheral sysbus.axiGpio 0", "AddPeripheral sysbus.axiGpio 2"))
    bench.write_bytes(0, PROGRAM)
    bench.write(0xF8000008, 0xDF0D)
    bench.write(FPGA_RST_CTRL, 1 << 0)  # only FCLK_RESET0_N asserted
    bench.run_us(200)
    assert bench.read(MARKER) == 0x600DF00D


def test_block_ram_loses_its_contents_with_the_configuration(pl: Bench) -> None:
    pl.execute("cpu0 IsHalted true")
    pl.write(BRAM + 0x10, 0x12345678)
    pl.write(DEVC_CTRL, 0x0C006000 | 1 << 30)  # PCFG_PROG_B high
    assert pl.read(BRAM + 0x10) == 0x12345678
    pl.write(BRAM + 0x10, 0x12345678)
    pl.write(DEVC_CTRL, 0x0C006000)  # PCFG_PROG_B low: the PL is cleared
    assert pl.read(BRAM + 0x10) == 0x12345678  # not configured yet: nothing to lose
    pl.write(DEVC_CTRL, 0x0C006000 | 1 << 30)
    pl.execute(f'devc ConfigureFromJtag "{Path(__file__).as_posix()}"')  # any file will do
    pl.write(BRAM + 0x10, 0x12345678)
    pl.write(DEVC_CTRL, 0x0C006000)  # PROG_B low clears a configured PL
    assert pl.read(BRAM + 0x10) == 0


def test_block_ram_is_cleared_by_a_reset(pl: Bench) -> None:
    pl.execute("cpu0 IsHalted true")
    pl.write(BRAM, 0xCAFE)
    pl.execute("machine Reset")
    assert pl.read(BRAM) == 0


def test_fclk_resets_number_zero_to_three(pl: Bench) -> None:
    with pytest.raises(RenodeError, match="does not exist"):
        pl.execute("pl AddPeripheral sysbus.axiGpio 4")
