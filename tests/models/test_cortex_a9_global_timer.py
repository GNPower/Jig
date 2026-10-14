"""Cortex-A9 MPCore global timer (Jig.Arm.CortexA9GlobalTimer, ARM DDI 0407
section 4.4): the prescaler reads back as written, so read-modify-writes of
CONTROL keep the rate (Renode's stock ARM_GlobalTimer returns prescaler + 1);
the 64-bit counter, the comparator event and interrupt, and auto-increment.
Reset values are UG585 v1.7 B.24's (all zero)."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

TIMER = 0xF8F00200
COUNTER_LO, COUNTER_HI, CONTROL, STATUS = TIMER + 0x00, TIMER + 0x04, TIMER + 0x08, TIMER + 0x0C
COMPARATOR_LO, COMPARATOR_HI, AUTO_INCREMENT = TIMER + 0x10, TIMER + 0x14, TIMER + 0x18

ENABLE, COMPARATOR_ENABLE, IRQ_ENABLE, AUTO_INCREMENT_ENABLE = 1 << 0, 1 << 1, 1 << 2, 1 << 3

MACHINE = """\
globalTimer: Jig.Arm.CortexA9GlobalTimer @ sysbus <0xF8F00200, +0x100>
    frequency: 100000000
    0 -> probe@0

probe: JigFixtures.GpioProbe @ sysbus
"""


@pytest.fixture
def timer(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def count(bench: Bench) -> int:
    return bench.read(COUNTER_HI) << 32 | bench.read(COUNTER_LO)


def test_reset_values(timer: Bench) -> None:
    for address in (COUNTER_LO, COUNTER_HI, CONTROL, STATUS, COMPARATOR_LO, AUTO_INCREMENT):
        assert timer.read(address) == 0, hex(address)


def test_the_counter_runs_at_the_peripheral_clock(timer: Bench) -> None:
    timer.write(CONTROL, ENABLE)
    timer.run_ms(1)
    assert count(timer) == 100_000


def test_the_prescaler_reads_back_as_written(timer: Bench) -> None:
    timer.write(CONTROL, 5 << 8 | ENABLE)
    assert timer.read(CONTROL) == 5 << 8 | ENABLE
    for _ in range(10):  # read-modify-writes keep the prescaler
        timer.write(CONTROL, timer.read(CONTROL))
    assert timer.read(CONTROL) == 5 << 8 | ENABLE
    before = count(timer)
    timer.run_ms(1)
    assert count(timer) - before == 100_000 // 6


def test_the_counter_is_64_bits(timer: Bench) -> None:
    timer.write(COUNTER_LO, 0xFFFFFF00)
    timer.write(COUNTER_HI, 0x00000001)
    assert count(timer) == 0x1FFFFFF00
    timer.write(CONTROL, ENABLE)
    timer.run_us(10)  # 1000 ticks
    assert count(timer) == 0x1FFFFFF00 + 1000


def test_the_comparator_sets_the_event_and_interrupt(timer: Bench) -> None:
    timer.write(COMPARATOR_LO, 1000)
    timer.write(CONTROL, ENABLE | COMPARATOR_ENABLE | IRQ_ENABLE)
    timer.run_us(9)
    assert timer.read(STATUS) == 0 and not timer.flag("probe Level 0")
    timer.run_us(2)
    assert timer.read(STATUS) == 1
    assert timer.flag("probe Level 0")
    timer.write(STATUS, 1)  # write one to clear
    assert timer.read(STATUS) == 0 and not timer.flag("probe Level 0")


def test_a_comparator_already_passed_matches_at_once(timer: Bench) -> None:
    timer.write(COUNTER_LO, 5000)
    timer.write(COMPARATOR_LO, 4000)
    timer.write(CONTROL, COMPARATOR_ENABLE)
    assert timer.read(STATUS) == 1


def test_auto_increment_moves_the_comparator(timer: Bench) -> None:
    timer.write(COMPARATOR_LO, 1000)
    timer.write(AUTO_INCREMENT, 1000)
    timer.write(CONTROL, ENABLE | COMPARATOR_ENABLE | AUTO_INCREMENT_ENABLE)
    timer.run_us(35)  # events at 1000, 2000 and 3000 ticks
    assert timer.read(COMPARATOR_LO) == 4000
    assert timer.read(STATUS) == 1


def test_the_comparator_is_64_bits(timer: Bench) -> None:
    timer.write(COMPARATOR_HI, 1)
    timer.write(CONTROL, ENABLE | COMPARATOR_ENABLE)
    timer.run_us(100)
    assert timer.read(STATUS) == 0  # 2^32 ticks away
    assert timer.read(COMPARATOR_HI) == 1


STOCK = """\
cpu0: CPU.ARMv7A @ sysbus
    cpuType: "cortex-a9"
    genericInterruptController: gic

gic: IRQControllers.ARM_GenericInterruptController @ {
  sysbus new Bus.BusMultiRegistration { address: 0xF8F01000; size: 0x1000; region: "distributor" };
  sysbus new Bus.BusMultiRegistration { address: 0xF8F00100; size: 0x100; region: "cpuInterface" }
    }
    supportsTwoSecurityStates: false
    architectureVersion: .GICv1

stockTimer: Timers.ARM_GlobalTimer @ sysbus 0xF8F00200
    frequency: 100000000
    irqController: gic
"""


def test_renodes_stock_timer_reads_the_prescaler_back_one_higher(bench: Bench) -> None:
    # The defect this model replaces Renode's ARM_GlobalTimer for: each
    # read-modify-write of CONTROL raises the prescaler by one. When a Renode
    # release fixes it, this test fails and the replacement can go.
    bench.load(STOCK)
    bench.execute("cpu0 IsHalted true")
    bench.write(CONTROL, 5 << 8 | ENABLE, cpu="cpu0")
    assert bench.read(CONTROL, cpu="cpu0") >> 8 & 0xFF == 6
