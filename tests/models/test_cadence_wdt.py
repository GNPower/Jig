"""Cadence watchdog (Jig.Cadence.CadenceWDT, the Zynq-7000 SWDT): reset values
(UG585 v1.7 B.31: disabled at reset), the access keys, the counter and its
prescaler, restart, and expiry raising a system watchdog reset through the
SLCR (REBOOT_STATUS[16]) or an interrupt pulse."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

SWDT = 0xF8005000
MODE, CONTROL, RESTART, STATUS = SWDT + 0x0, SWDT + 0x4, SWDT + 0x8, SWDT + 0xC
REBOOT_STATUS = 0xF8000258
SWDT_RST = 1 << 16
POR = 1 << 22

ZKEY = 0xABC << 12
CKEY = 0x248 << 14
RESTART_KEY = 0x1999
WDEN, RSTEN, IRQEN = 1 << 0, 1 << 1, 1 << 2
IRQLN_32 = 0x3 << 7  # interrupt pulse length field, as at reset

MACHINE = """\
slcr: Jig.Xilinx.Zynq7000_SLCR @ sysbus <0xF8000000, +0x1000>

swdt: Jig.Cadence.CadenceWDT @ sysbus <0xF8005000, +0x1000>
    frequency: 100000000
    resetController: slcr
    -> probe@0

probe: JigFixtures.GpioProbe @ sysbus
"""

# CRV 0: the counter restarts at 0xFFF; at 100 MHz / 8 it reaches zero after 327.6 us.
SHORT_US = 0xFFF * 8 / 100


@pytest.fixture
def swdt(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def arm_watchdog(bench: Bench, mode: int, clock_select: int = 0) -> None:
    bench.write(CONTROL, CKEY | 0 << 2 | clock_select)
    bench.write(MODE, ZKEY | IRQLN_32 | mode)
    bench.write(RESTART, RESTART_KEY)


def test_reset_values(swdt: Bench) -> None:
    # UG585 v1.7 B.31's values; not yet checked on hardware.
    assert swdt.read(MODE) == 0x1C0  # WDEN, RSTEN and IRQEN clear
    assert swdt.read(CONTROL) == 0x3FFC  # CRV 0xFFF, CLKSEL 0
    assert swdt.read(STATUS) == 0


def test_writes_need_their_keys(swdt: Bench) -> None:
    swdt.write(MODE, 0x123 << 12 | WDEN)
    swdt.write(CONTROL, 0x111 << 14 | 0x3)
    assert swdt.read(MODE) == 0x1C0
    assert swdt.read(CONTROL) == 0x3FFC
    swdt.write(MODE, ZKEY | 0x1C0 | IRQEN)
    assert swdt.read(MODE) == 0x1C0 | IRQEN  # the key reads back as 0


def test_the_counter_runs_without_resetting_while_disabled(swdt: Bench) -> None:
    arm_watchdog(swdt, mode=0)
    swdt.run_us(SHORT_US + 2)
    assert swdt.read(STATUS) == 1  # WDZ: the counter reached zero
    assert swdt.read(REBOOT_STATUS) == POR
    assert swdt.number("probe Rises 0") == 0


def test_expiry_resets_the_system_through_the_slcr(swdt: Bench) -> None:
    arm_watchdog(swdt, mode=WDEN | RSTEN)
    swdt.run_us(SHORT_US - 1)
    assert swdt.read(REBOOT_STATUS) == POR
    swdt.run_us(2)
    assert swdt.read(REBOOT_STATUS) == POR | SWDT_RST
    swdt.run_us(1)  # the machine resets at its next synchronisation point
    assert swdt.read(MODE) == 0x1C0
    assert swdt.text("slcr LastResetCause") == "SystemWatchdog"


def test_expiry_pulses_the_interrupt(swdt: Bench) -> None:
    arm_watchdog(swdt, mode=WDEN | IRQEN)
    swdt.run_us(SHORT_US + 2)
    assert swdt.number("probe Rises 0") == 1
    assert not swdt.flag("probe Level 0")  # a pulse, not a level
    assert swdt.read(REBOOT_STATUS) == POR


def test_restarting_keeps_the_watchdog_from_expiring(swdt: Bench) -> None:
    arm_watchdog(swdt, mode=WDEN | RSTEN)
    for _ in range(5):
        swdt.run_us(20)
        swdt.write(RESTART, RESTART_KEY)
    assert swdt.read(REBOOT_STATUS) == POR
    swdt.write(RESTART, 0x1234)  # a wrong key does not restart it
    swdt.run_us(SHORT_US)
    assert swdt.read(REBOOT_STATUS) == POR | SWDT_RST


def test_the_prescaler_divides_the_clock(swdt: Bench) -> None:
    arm_watchdog(swdt, mode=0, clock_select=1)  # / 64: 0xFFF ticks take 2620.8 us
    swdt.run_us(2600)
    assert swdt.read(STATUS) == 0
    swdt.run_us(40)
    assert swdt.read(STATUS) == 1


def test_a_restart_clears_the_zero_flag(swdt: Bench) -> None:
    arm_watchdog(swdt, mode=0)
    swdt.run_us(SHORT_US + 2)
    assert swdt.read(STATUS) == 1
    swdt.write(RESTART, RESTART_KEY)
    assert swdt.read(STATUS) == 0
