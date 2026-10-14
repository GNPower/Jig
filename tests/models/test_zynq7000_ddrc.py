"""Zynq-7000 DDR controller registers (Jig.Xilinx.Zynq7000_DDRC): storage for
ps7_init's read-modify-writes and the operating mode that follows the soft
reset bit (UG585 v1.7 B.6 ddrc_ctrl, mode_sts_reg)."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

DDRC = 0xF8006000
DDRC_CTRL = DDRC + 0x000
TWO_RANK_CFG = DDRC + 0x004
MODE_STS = DDRC + 0x054

MACHINE = "ddrc: Jig.Xilinx.Zynq7000_DDRC @ sysbus <0xF8006000, +0x1000>\n"


@pytest.fixture
def ddrc(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def test_operating_mode_follows_the_soft_reset_bit(ddrc: Bench) -> None:
    assert ddrc.read(MODE_STS) == 0  # DDRC init
    ddrc.write(DDRC_CTRL, 0x00000081)  # reg_ddrc_soft_rstb released
    assert ddrc.read(MODE_STS) & 0x7 == 1  # normal operation
    ddrc.write(DDRC_CTRL, 0x00000080)
    assert ddrc.read(MODE_STS) & 0x7 == 0


def test_mode_status_is_read_only(ddrc: Bench) -> None:
    ddrc.write(MODE_STS, 0xFFFFFFFF)
    assert ddrc.read(MODE_STS) == 0


def test_registers_hold_what_ps7_init_writes(ddrc: Bench) -> None:
    ddrc.write(TWO_RANK_CFG, 0x000C1076)
    value = ddrc.read(TWO_RANK_CFG)
    ddrc.write(TWO_RANK_CFG, (value & ~0x1F) | 0x12)  # a masked write
    assert ddrc.read(TWO_RANK_CFG) == 0x000C1072


def test_a_reset_clears_the_registers(ddrc: Bench) -> None:
    ddrc.write(TWO_RANK_CFG, 0x1234)
    ddrc.write(DDRC_CTRL, 1)
    ddrc.execute("machine Reset")
    assert ddrc.read(TWO_RANK_CFG) == 0
    assert ddrc.read(MODE_STS) == 0
