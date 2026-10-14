"""ARM L2C-310 cache controller registers (Jig.Arm.PL310): reset values of
UG585 v1.7 B.23, configuration registers that only take writes while the
cache is disabled, maintenance operations that complete at once, and no error
interrupts."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

L2 = 0xF8F02000
CACHE_ID, CACHE_TYPE = L2 + 0x000, L2 + 0x004
CONTROL, AUX_CONTROL, TAG_RAM, DATA_RAM = L2 + 0x100, L2 + 0x104, L2 + 0x108, L2 + 0x10C
INT_MASK, INT_MASKED, INT_RAW, INT_CLEAR = L2 + 0x214, L2 + 0x218, L2 + 0x21C, L2 + 0x220
CACHE_SYNC, INVALIDATE_WAY, CLEAN_INVALIDATE_WAY = L2 + 0x730, L2 + 0x77C, L2 + 0x7FC
DEBUG_CONTROL, PREFETCH_CONTROL, POWER_CONTROL = L2 + 0xF40, L2 + 0xF60, L2 + 0xF80

MACHINE = "l2cache: Jig.Arm.PL310 @ sysbus <0xF8F02000, +0x1000>\n"


@pytest.fixture
def l2(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def test_reset_values(l2: Bench) -> None:
    expected = {
        CACHE_ID: 0x410000C8,  # ARM, L2C-310, RTL release r3p2
        CACHE_TYPE: 0x9E300300,
        CONTROL: 0,
        AUX_CONTROL: 0x02060000,
        TAG_RAM: 0x777,
        DATA_RAM: 0x777,
        INT_MASK: 0,
        INT_RAW: 0,
        DEBUG_CONTROL: 0,
        POWER_CONTROL: 0,
    }
    assert {address: l2.read(address) for address in expected} == expected


def test_configuration_needs_the_cache_disabled(l2: Bench) -> None:
    l2.write(AUX_CONTROL, 0x72360000)
    l2.write(TAG_RAM, 0x111)
    l2.write(DATA_RAM, 0x121)
    l2.write(CONTROL, 1)
    l2.write(AUX_CONTROL, 0)  # refused while enabled
    l2.write(TAG_RAM, 0)
    assert l2.read(AUX_CONTROL) == 0x72360000
    assert l2.read(TAG_RAM) == 0x111
    assert l2.read(DATA_RAM) == 0x121
    l2.write(CONTROL, 0)
    l2.write(AUX_CONTROL, 0x02060000)
    assert l2.read(AUX_CONTROL) == 0x02060000


def test_maintenance_completes_at_once(l2: Bench) -> None:
    l2.write(INVALIDATE_WAY, 0xFF)
    assert l2.read(INVALIDATE_WAY) == 0
    l2.write(CLEAN_INVALIDATE_WAY, 0xFF)
    assert l2.read(CLEAN_INVALIDATE_WAY) == 0
    l2.write(CACHE_SYNC, 0)
    assert l2.read(CACHE_SYNC) == 0


def test_no_error_interrupt_ever_raises(l2: Bench) -> None:
    l2.write(INT_MASK, 0x1FF)
    l2.write(INT_CLEAR, 0x1FF)
    assert l2.read(INT_MASK) == 0x1FF
    assert l2.read(INT_RAW) == 0 and l2.read(INT_MASKED) == 0


def test_identification_is_read_only(l2: Bench) -> None:
    l2.write(CACHE_ID, 0)
    l2.write(CACHE_TYPE, 0)
    assert l2.read(CACHE_ID) == 0x410000C8
    assert l2.read(CACHE_TYPE) == 0x9E300300


def test_registers_hold_values_until_reset(l2: Bench) -> None:
    l2.write(PREFETCH_CONTROL, 0x70000000)
    l2.write(DEBUG_CONTROL, 0x3)
    assert l2.read(PREFETCH_CONTROL) == 0x70000000
    l2.execute("machine Reset")
    assert l2.read(PREFETCH_CONTROL) == 0
    assert l2.read(AUX_CONTROL) == 0x02060000
