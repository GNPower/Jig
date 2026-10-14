"""Cortex-A9 private timer and watchdog (Jig.Arm.CortexA9PrivateTimerWatchdog,
ARM DDI 0407 sections 4.1-4.3): the timer counting down at the peripheral
clock over the prescaler, one-shot and auto-reload, the event flag and its
interrupt; the watchdog in timer mode, and in watchdog mode resetting the
system through the SLCR (REBOOT_STATUS[17]) with its reset flag kept across
the reset, the kick, and the disable sequence. Reset values are UG585 v1.7
B.24's (all zero)."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

BASE = 0xF8F00600
TIMER_LOAD, TIMER_COUNTER, TIMER_CONTROL, TIMER_STATUS = (
    BASE + o for o in (0x00, 0x04, 0x08, 0x0C)
)
WD_LOAD, WD_COUNTER, WD_CONTROL, WD_STATUS = (BASE + o for o in (0x20, 0x24, 0x28, 0x2C))
WD_RESET_STATUS, WD_DISABLE = BASE + 0x30, BASE + 0x34
REBOOT_STATUS = 0xF8000258
AWDT0_RST = 1 << 17
POR = 1 << 22

ENABLE, AUTO_RELOAD, IRQ_ENABLE, WATCHDOG_MODE = 1 << 0, 1 << 1, 1 << 2, 1 << 3

MACHINE = """\
slcr: Jig.Xilinx.Zynq7000_SLCR @ sysbus <0xF8000000, +0x1000>

privateTimer: Jig.Arm.CortexA9PrivateTimerWatchdog @ sysbus <0xF8F00600, +0x40>
    frequency: 100000000
    resetController: slcr
    TimerIRQ -> probe@0
    WatchdogIRQ -> probe@1

probe: JigFixtures.GpioProbe @ sysbus
"""


@pytest.fixture
def timers(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def test_reset_values(timers: Bench) -> None:
    for offset in range(0x00, 0x38, 4):
        if offset in (0x10, 0x14, 0x18, 0x1C):
            continue
        assert timers.read(BASE + offset) == 0, hex(offset)


def test_the_timer_counts_down_once(timers: Bench) -> None:
    timers.write(TIMER_LOAD, 1000)
    timers.write(TIMER_CONTROL, ENABLE | IRQ_ENABLE)
    timers.run_us(5)
    assert 495 <= timers.read(TIMER_COUNTER) <= 505
    timers.run_us(6)
    assert timers.read(TIMER_COUNTER) == 0
    assert timers.read(TIMER_STATUS) == 1
    assert timers.flag("probe Level 0")
    timers.run_us(20)
    assert timers.read(TIMER_COUNTER) == 0  # one-shot: it stays at zero
    timers.write(TIMER_STATUS, 1)
    assert timers.read(TIMER_STATUS) == 0 and not timers.flag("probe Level 0")


def test_auto_reload_restarts_the_count(timers: Bench) -> None:
    timers.write(TIMER_LOAD, 1000)
    timers.write(TIMER_CONTROL, ENABLE | AUTO_RELOAD)
    timers.run_us(25)  # 2.5 periods
    assert timers.read(TIMER_STATUS) == 1
    assert 450 <= timers.read(TIMER_COUNTER) <= 550


def test_the_prescaler_divides_the_clock(timers: Bench) -> None:
    timers.write(TIMER_LOAD, 1000)
    timers.write(TIMER_CONTROL, 9 << 8 | ENABLE)  # / 10
    timers.run_us(95)
    assert timers.read(TIMER_STATUS) == 0
    timers.run_us(10)
    assert timers.read(TIMER_STATUS) == 1


def test_the_watchdog_as_a_timer(timers: Bench) -> None:
    timers.write(WD_LOAD, 1000)
    timers.write(WD_CONTROL, ENABLE | IRQ_ENABLE)
    timers.run_us(11)
    assert timers.read(WD_STATUS) == 1
    assert timers.flag("probe Level 1")
    assert timers.read(REBOOT_STATUS) == POR


def test_the_watchdog_resets_the_system(timers: Bench) -> None:
    timers.write(WD_LOAD, 1000)
    timers.write(WD_CONTROL, ENABLE | WATCHDOG_MODE)
    timers.run_us(9)
    assert timers.read(WD_RESET_STATUS) == 0
    timers.run_us(2)
    assert timers.read(REBOOT_STATUS) == POR | AWDT0_RST
    timers.run_us(1)  # the machine resets at its next synchronisation point
    assert timers.read(WD_CONTROL) == 0
    assert timers.read(WD_RESET_STATUS) == 1  # survives the reset it caused
    timers.write(WD_RESET_STATUS, 1)  # software clears it
    assert timers.read(WD_RESET_STATUS) == 0


def test_a_power_on_reset_clears_the_reset_flag(timers: Bench) -> None:
    timers.write(WD_LOAD, 1000)
    timers.write(WD_CONTROL, ENABLE | WATCHDOG_MODE)
    timers.run_us(12)
    assert timers.read(WD_RESET_STATUS) == 1
    timers.execute("slcr OnGPIO 0 true")  # PS_POR_B
    timers.run_us(1)
    assert timers.read(WD_RESET_STATUS) == 0


def test_writing_load_kicks_the_watchdog(timers: Bench) -> None:
    timers.write(WD_LOAD, 1000)
    timers.write(WD_CONTROL, ENABLE | WATCHDOG_MODE)
    for _ in range(10):
        timers.run_us(5)
        timers.write(WD_LOAD, 1000)
    assert timers.read(REBOOT_STATUS) == POR


def test_watchdog_mode_is_left_only_through_the_disable_sequence(timers: Bench) -> None:
    timers.write(WD_LOAD, 0xFFFFFF)
    timers.write(WD_CONTROL, ENABLE | WATCHDOG_MODE)
    timers.write(WD_CONTROL, ENABLE)  # a plain write keeps watchdog mode
    assert timers.read(WD_CONTROL) & WATCHDOG_MODE
    timers.write(WD_DISABLE, 0x87654321)  # out of order
    timers.write(WD_DISABLE, 0x12345678)
    timers.write(WD_DISABLE, 0x0)
    assert timers.read(WD_CONTROL) & WATCHDOG_MODE
    timers.write(WD_DISABLE, 0x12345678)
    timers.write(WD_DISABLE, 0x87654321)
    assert timers.read(WD_CONTROL) & WATCHDOG_MODE == 0
