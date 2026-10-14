"""Zynq-7000 PS GPIO (Jig.Xilinx.Zynq7000_GPIO): reset values (UG585 v1.7
B.19), masked data writes, outputs driving pins, external levels and pulls,
and the per-bank interrupt logic (edge, level, both edges, masking)."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

GPIO = 0xE000A000
MASK_DATA_0_LSW = GPIO + 0x000
MASK_DATA_0_MSW = GPIO + 0x004
DATA_0 = GPIO + 0x040
DATA_0_RO = GPIO + 0x060
DATA_2_RO = GPIO + 0x068


def bank(register: int, number: int) -> int:
    """A per-bank register (DIRM 0x204, OEN 0x208, INT_MASK 0x20C, INT_EN
    0x210, INT_DIS 0x214, INT_STAT 0x218, INT_TYPE 0x21C, INT_POLARITY 0x220,
    INT_ANY 0x224) of bank ``number``."""
    return GPIO + register + 0x40 * number


DIRM, OEN, INT_MASK, INT_EN, INT_DIS = 0x204, 0x208, 0x20C, 0x210, 0x214
INT_STAT, INT_TYPE, INT_POLARITY, INT_ANY = 0x218, 0x21C, 0x220, 0x224

# Pins 0-3 (bank 0) go to probe inputs 0-3, the interrupt to probe input 8;
# probe outputs 10 and 11 drive EMIO pins 54 and 55 (bank 2, bits 0 and 1).
MACHINE = """\
gpio: Jig.Xilinx.Zynq7000_GPIO @ sysbus <0xE000A000, +0x1000>
    [0-3] -> probe@[0-3]
    IRQ -> probe@8

probe: JigFixtures.GpioProbe @ sysbus
    [10-11] -> gpio@[54-55]
"""


@pytest.fixture
def gpio(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def irq(bench: Bench) -> bool:
    return bench.flag("probe Level 8")


def test_reset_values(gpio: Bench) -> None:
    for number, interrupt_type in enumerate((0xFFFFFFFF, 0x003FFFFF, 0xFFFFFFFF, 0xFFFFFFFF)):
        for register in (DIRM, OEN, INT_MASK, INT_STAT, INT_POLARITY, INT_ANY):
            assert gpio.read(bank(register, number)) == 0, (hex(register), number)
        assert gpio.read(bank(INT_TYPE, number)) == interrupt_type


def test_masked_writes_change_only_unprotected_bits(gpio: Bench) -> None:
    gpio.write(MASK_DATA_0_LSW, 0xFFF0 << 16 | 0xABCD)  # only bits 3:0 writable
    assert gpio.read(DATA_0) == 0x000D
    gpio.write(MASK_DATA_0_MSW, 0xFFFE << 16 | 0x0001)  # bit 16
    assert gpio.read(DATA_0) == 0x0001000D
    assert gpio.read(MASK_DATA_0_LSW) == 0x000D
    assert gpio.read(MASK_DATA_0_MSW) == 0x0001
    gpio.write(DATA_0, 0x12345678)
    assert gpio.read(DATA_0) == 0x12345678


def test_enabled_outputs_drive_their_pins(gpio: Bench) -> None:
    gpio.write(DATA_0, 0x5)
    assert [gpio.flag(f"probe Level {n}") for n in range(4)] == [False] * 4  # inputs still
    gpio.write(bank(DIRM, 0), 0xF)
    assert [gpio.flag(f"probe Level {n}") for n in range(4)] == [False] * 4  # not enabled
    gpio.write(bank(OEN, 0), 0xF)
    assert [gpio.flag(f"probe Level {n}") for n in range(4)] == [True, False, True, False]
    assert gpio.read(DATA_0_RO) & 0xF == 0x5
    assert gpio.flag("gpio IsOutput 2") and not gpio.flag("gpio IsOutput 4")


def test_inputs_show_external_levels_and_pulls(gpio: Bench) -> None:
    gpio.execute("probe Drive 10 true")
    assert gpio.read(DATA_2_RO) == 0x1
    gpio.execute("gpio SetPull 55 true")
    assert gpio.read(DATA_2_RO) == 0x3
    assert gpio.flag("gpio GetPinLevel 55")
    gpio.execute("probe Drive 10 false")
    assert gpio.read(DATA_2_RO) == 0x2


def test_rising_edges_latch_the_interrupt(gpio: Bench) -> None:
    gpio.write(bank(INT_POLARITY, 2), 0x1)  # INT_TYPE resets to edge
    gpio.write(bank(INT_EN, 2), 0x1)
    gpio.execute("probe Drive 10 true")
    assert gpio.read(bank(INT_STAT, 2)) == 0x1
    assert irq(gpio)
    gpio.write(bank(INT_STAT, 2), 0x1)  # write one to clear
    assert gpio.read(bank(INT_STAT, 2)) == 0 and not irq(gpio)
    gpio.execute("probe Drive 10 false")  # a falling edge: not this polarity
    assert gpio.read(bank(INT_STAT, 2)) == 0


def test_any_edge(gpio: Bench) -> None:
    gpio.write(bank(INT_ANY, 2), 0x1)
    gpio.execute("probe Drive 10 true")
    gpio.write(bank(INT_STAT, 2), 0x1)
    gpio.execute("probe Drive 10 false")
    assert gpio.read(bank(INT_STAT, 2)) == 0x1


def test_level_interrupts_reassert_while_the_level_lasts(gpio: Bench) -> None:
    gpio.write(bank(INT_TYPE, 2), 0xFFFFFFFE)  # bit 0 level-sensitive
    gpio.write(bank(INT_POLARITY, 2), 0x1)  # active high
    gpio.execute("probe Drive 10 true")
    assert gpio.read(bank(INT_STAT, 2)) == 0x1
    gpio.write(bank(INT_STAT, 2), 0x1)
    assert gpio.read(bank(INT_STAT, 2)) == 0x1
    gpio.execute("probe Drive 10 false")
    gpio.write(bank(INT_STAT, 2), 0x1)
    assert gpio.read(bank(INT_STAT, 2)) == 0


def test_masked_events_are_recorded_without_the_interrupt(gpio: Bench) -> None:
    gpio.write(bank(INT_POLARITY, 2), 0x1)
    gpio.write(bank(INT_DIS, 2), 0x1)
    assert gpio.read(bank(INT_MASK, 2)) == 0x1
    gpio.execute("probe Drive 10 true")
    assert gpio.read(bank(INT_STAT, 2)) == 0x1 and not irq(gpio)
    gpio.write(bank(INT_EN, 2), 0x1)
    assert gpio.read(bank(INT_MASK, 2)) == 0 and irq(gpio)
    gpio.write(bank(INT_MASK, 2), 0x1)  # read-only
    assert gpio.read(bank(INT_MASK, 2)) == 0


def test_bank_1_has_22_pins(gpio: Bench) -> None:
    gpio.write(bank(DIRM, 1), 0xFFFFFFFF)
    assert gpio.read(bank(DIRM, 1)) == 0x003FFFFF
