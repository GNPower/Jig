"""STM32U5 real-time clock and backup registers (Jig.ST.STM32U5_RTC and
Jig.ST.STM32U5_TAMP, ST RM0456 "Real-time clock (RTC)" and "Tamper and backup
registers (TAMP)"): the reset values, write protection and initialization
mode, the BCD calendar with its roll-overs, the AM/PM format, RSF, ADD1H and
SUB1H, alarms, the wakeup timer, the IRealTimeClock contract, and the backup
domain: the calendar and the backup registers keep their state across system
resets and lose it on a power-on reset, on RCC_BDCR.BDRST and on PowerCycle.

The last test pins the defect of Renode's STM32F4_RTC this model replaces on
the STM32U5 platform: it fails once a Renode release fixes it."""

from __future__ import annotations

import pytest

from jig.errors import RenodeError

from .bench import Bench

pytestmark = pytest.mark.renode

RTC = 0x46007800
TR, DR, SSR, ICSR, PRER, WUTR, CR, PRIVCFGR = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C
WPR, CALR, ALRMAR, ALRMASSR, ALRMBR, SR, MISR, SCR = 0x24, 0x28, 0x40, 0x44, 0x48, 0x50, 0x54, 0x5C

WUTWF, INITS, RSF, INITF, INIT = 1 << 2, 1 << 4, 1 << 5, 1 << 6, 1 << 7
BYPSHAD, FMT, ALRAE, ALRBE, WUTE = 1 << 5, 1 << 6, 1 << 8, 1 << 9, 1 << 10
ALRAIE, ALRBIE, WUTIE, ADD1H, SUB1H = 1 << 12, 1 << 13, 1 << 14, 1 << 16, 1 << 17
ALRAF, ALRBF, WUTF = 1 << 0, 1 << 1, 1 << 2
MSK1, MSK2, MSK3, PM, WDSEL, MSK4 = 1 << 7, 1 << 15, 1 << 23, 1 << 22, 1 << 30, 1 << 31

TAMP = 0x46007C00
BKP0R, BKP31R, COUNTR, TAMP_SR = 0x100, 0x17C, 0x40, 0x30

RCC = 0x46020C00
BDCR, BDRST = 0xF0, 1 << 16

MACHINE = """\
rtc: Jig.ST.STM32U5_RTC @ sysbus 0x46007800
    IRQ -> probe@0

tamp: Jig.ST.STM32U5_TAMP @ sysbus 0x46007C00

rcc: Jig.ST.STM32U5_RCC @ sysbus 0x46020C00
    BackupDomainReset -> rtc@0 | tamp@0

probe: JigFixtures.GpioProbe @ sysbus
"""


@pytest.fixture
def rtc(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def read(bench: Bench, offset: int) -> int:
    return bench.read(RTC + offset)


def write(bench: Bench, offset: int, value: int) -> None:
    bench.write(RTC + offset, value)


def bcd(value: int) -> int:
    return (value // 10) << 4 | value % 10


def time_register(hours: int, minutes: int, seconds: int) -> int:
    return bcd(hours) << 16 | bcd(minutes) << 8 | bcd(seconds)


def date_register(year: int, month: int, day: int, week_day: int) -> int:
    return bcd(year % 100) << 16 | week_day << 13 | bcd(month) << 8 | bcd(day)


def unlock(bench: Bench) -> None:
    write(bench, WPR, 0xCA)
    write(bench, WPR, 0x53)


def lock(bench: Bench) -> None:
    write(bench, WPR, 0xFE)  # LL_RTC_EnableWriteProtection writes 0xFE then 0x64
    write(bench, WPR, 0x64)


def set_calendar(bench: Bench, time: int, date: int) -> None:
    """The HAL sequence: unlock, INIT and wait for INITF, TR and DR, leave INIT, lock."""
    unlock(bench)
    write(bench, ICSR, INIT)
    assert read(bench, ICSR) & INITF
    write(bench, TR, time)
    write(bench, DR, date)
    write(bench, ICSR, 0)
    lock(bench)


def date_time(bench: Bench) -> str:
    return bench.text("rtc GetDateTime")


def test_reset_values(rtc: Bench) -> None:
    expected = {TR: 0, DR: 0x00002101, SSR: 0xFF, ICSR: WUTWF, PRER: 0x007F00FF}
    expected |= {WUTR: 0xFFFF, CR: 0, SR: 0, MISR: 0}
    assert {offset: read(rtc, offset) for offset in expected} == expected
    assert date_time(rtc) == "2000-01-01T00:00:00.000"


def test_the_calendar_counts_from_the_prescalers(rtc: Bench) -> None:
    rtc.run_ms(1500)  # RTCCLK 32768 Hz / 128 / 256: one second, SSR 256 steps
    assert read(rtc, TR) == time_register(0, 0, 1)
    assert read(rtc, SSR) == 255 - 128
    assert date_time(rtc) == "2000-01-01T00:00:01.500"


def test_a_prescaler_change_sets_the_rate(rtc: Bench) -> None:
    unlock(rtc)
    write(rtc, ICSR, INIT)
    write(rtc, PRER, 0x1F << 16 | 0x3FF)  # 32768 / 32 / 1024: still 1 Hz, finer SSR
    write(rtc, ICSR, 0)
    assert read(rtc, PRER) == 0x001F03FF
    rtc.run_ms(2250)
    assert read(rtc, TR) == time_register(0, 0, 2)
    assert read(rtc, SSR) == 1023 - 256
    assert date_time(rtc) == "2000-01-01T00:00:02.2500"


def test_calendar_writes_need_the_keys_and_init_mode(rtc: Bench) -> None:
    write(rtc, ICSR, INIT)  # locked: dropped
    assert read(rtc, ICSR) & INITF == 0
    unlock(rtc)
    write(rtc, TR, time_register(9, 30, 0))  # not in initialization mode: dropped
    assert read(rtc, TR) == 0
    write(rtc, ICSR, INIT)
    assert read(rtc, ICSR) & (INIT | INITF) == INIT | INITF
    write(rtc, TR, time_register(9, 30, 0))
    write(rtc, DR, date_register(2026, 10, 9, 5))
    write(rtc, ICSR, 0)
    assert read(rtc, TR) == time_register(9, 30, 0)
    assert read(rtc, DR) == date_register(2026, 10, 9, 5)
    assert read(rtc, ICSR) & INITS  # the year is no longer 00
    write(rtc, WPR, 0xFF)  # a wrong key locks again
    write(rtc, CR, FMT)
    assert read(rtc, CR) == 0


def test_initialization_mode_stops_the_calendar(rtc: Bench) -> None:
    set_calendar(rtc, time_register(12, 0, 0), date_register(2026, 10, 9, 5))
    unlock(rtc)
    write(rtc, ICSR, INIT)
    rtc.run_ms(3000)
    assert read(rtc, TR) == time_register(12, 0, 0)
    write(rtc, ICSR, 0)
    rtc.run_ms(1000.5)
    assert read(rtc, TR) == time_register(12, 0, 1)


@pytest.mark.parametrize(
    ("start", "after"),
    [
        ("2024-02-28T23:59:59", "2024-02-29T00:00:00.000"),  # a leap year
        ("2023-02-28T23:59:59", "2023-03-01T00:00:00.000"),
        ("2026-04-30T23:59:59", "2026-05-01T00:00:00.000"),
        ("2026-12-31T23:59:59", "2027-01-01T00:00:00.000"),
        ("2099-12-31T23:59:59", "2000-01-01T00:00:00.000"),  # the year wraps from 99 to 00
    ],
)
def test_the_calendar_rolls_over(rtc: Bench, start: str, after: str) -> None:
    rtc.execute(f'rtc SetDateTime "{start}"')
    rtc.run_ms(1000.5)
    assert date_time(rtc) == after


def test_the_week_day_counts_on_from_what_software_wrote(rtc: Bench) -> None:
    set_calendar(rtc, time_register(23, 59, 59), date_register(2026, 10, 9, 7))
    rtc.run_ms(1000.5)
    assert read(rtc, DR) >> 13 & 0x7 == 1  # 7 rolls over to 1
    assert read(rtc, DR) & 0x3F == 0x10


def test_the_am_pm_format(rtc: Bench) -> None:
    set_calendar(rtc, time_register(13, 5, 0), date_register(2026, 10, 9, 5))
    unlock(rtc)
    write(rtc, CR, FMT)
    assert read(rtc, TR) == PM | time_register(1, 5, 0)
    write(rtc, ICSR, INIT)
    write(rtc, TR, time_register(12, 0, 0))  # 12 AM is midnight
    write(rtc, ICSR, 0)
    assert date_time(rtc) == "2026-10-09T00:00:00.000"
    assert read(rtc, TR) == time_register(12, 0, 0)


def test_rsf_reads_0_after_a_clear_then_1(rtc: Bench) -> None:
    write(rtc, ICSR, ~(RSF | INIT) & 0xFFFFFFFF)  # LL_RTC_ClearFlag_RS outside INIT
    assert read(rtc, ICSR) & RSF == 0
    assert read(rtc, ICSR) & RSF
    assert read(rtc, ICSR) & ~RSF == WUTWF  # BIN and BCDU unchanged outside INIT
    unlock(rtc)
    write(rtc, CR, BYPSHAD)
    assert read(rtc, ICSR) & RSF == 0  # bypassing the shadow registers
    assert read(rtc, ICSR) & RSF == 0


def test_add1h_and_sub1h_move_the_calendar_an_hour(rtc: Bench) -> None:
    set_calendar(rtc, time_register(23, 30, 0), date_register(2026, 10, 9, 5))
    unlock(rtc)
    write(rtc, CR, ADD1H)
    assert date_time(rtc) == "2026-10-10T00:30:00.000"
    assert read(rtc, CR) == 0  # both bits read 0
    write(rtc, CR, SUB1H)  # hour 0: no change
    assert date_time(rtc) == "2026-10-10T00:30:00.000"
    write(rtc, CR, ADD1H)
    write(rtc, CR, SUB1H)
    assert date_time(rtc) == "2026-10-10T00:30:00.000"


def test_alarm_a_fires_when_the_calendar_matches(rtc: Bench) -> None:
    set_calendar(rtc, time_register(12, 0, 0), date_register(2026, 10, 9, 5))
    unlock(rtc)
    write(rtc, ALRMAR, MSK4 | time_register(12, 0, 3))  # any date, 12:00:03
    write(rtc, CR, ALRAE | ALRAIE)
    rtc.run_ms(2500)
    assert read(rtc, SR) == 0 and not rtc.flag("probe Level 0")
    rtc.run_ms(1000)
    assert read(rtc, SR) == ALRAF and read(rtc, MISR) == ALRAF
    assert rtc.flag("probe Level 0")
    write(rtc, SCR, ALRAF)  # CALRAF
    assert read(rtc, SR) == 0 and not rtc.flag("probe Level 0")


def test_alarm_masks_and_the_week_day(rtc: Bench) -> None:
    set_calendar(rtc, time_register(12, 0, 58), date_register(2026, 10, 9, 5))
    unlock(rtc)
    every_minute = MSK4 | MSK3 | MSK2 | bcd(0)  # whenever the seconds are 00
    on_fridays = WDSEL | 5 << 24 | MSK3 | MSK2 | MSK1  # every second of a Friday
    write(rtc, ALRMAR, every_minute)
    write(rtc, ALRMBR, on_fridays)
    write(rtc, CR, ALRAE | ALRBE)
    rtc.run_ms(1000.5)  # 12:00:59
    assert read(rtc, SR) == ALRBF
    write(rtc, SCR, ALRBF)
    rtc.run_ms(1000)  # 12:01:00
    assert read(rtc, SR) == ALRAF | ALRBF
    assert read(rtc, MISR) == 0  # no interrupt enables
    assert not rtc.flag("probe Level 0")


def test_alarm_registers_take_writes_only_while_disabled(rtc: Bench) -> None:
    unlock(rtc)
    write(rtc, ALRMAR, MSK4 | 0x30)
    write(rtc, CR, ALRAE)
    write(rtc, ALRMAR, 0x45)  # dropped: alarm A is enabled
    write(rtc, ALRMASSR, 0x0F000000)
    assert read(rtc, ALRMAR) == MSK4 | 0x30 and read(rtc, ALRMASSR) == 0
    write(rtc, CR, 0)
    write(rtc, ALRMAR, 0x45)
    assert read(rtc, ALRMAR) == 0x45


@pytest.mark.parametrize(
    ("wucksel", "wut", "period_ms"),
    [
        (0b100, 1, 2000),  # ck_spre, 1 Hz: WUT + 1 = 2 seconds
        (0b000, 2047, 1000),  # RTCCLK / 16 = 2048 Hz
        (0b011, 1637, 100),  # RTCCLK / 2 = 16384 Hz: 1638 periods
    ],
)
def test_the_wakeup_timer(rtc: Bench, wucksel: int, wut: int, period_ms: int) -> None:
    unlock(rtc)
    assert read(rtc, ICSR) & WUTWF
    write(rtc, WUTR, wut)
    write(rtc, CR, wucksel)
    write(rtc, CR, wucksel | WUTE | WUTIE)
    assert read(rtc, ICSR) & WUTWF == 0
    write(rtc, WUTR, 5)  # dropped while the timer runs
    assert read(rtc, WUTR) == wut
    rtc.run_ms(period_ms * 0.99)
    assert read(rtc, SR) == 0
    rtc.run_ms(period_ms * 0.02)
    assert read(rtc, SR) == WUTF and rtc.flag("probe Level 0")
    write(rtc, SCR, WUTF)
    rtc.run_ms(period_ms)  # periodic
    assert read(rtc, SR) == WUTF


def test_set_and_get_date_time(rtc: Bench) -> None:
    rtc.execute('rtc SetDateTime "2026-10-09T12:34:56.5"')
    assert date_time(rtc) == "2026-10-09T12:34:56.500"
    assert read(rtc, DR) == date_register(2026, 10, 9, 5)  # a Friday
    assert read(rtc, TR) == time_register(12, 34, 56)
    assert read(rtc, SSR) == 127
    rtc.run_ms(600)
    assert date_time(rtc) == "2026-10-09T12:34:57.097"  # 1/256 s steps, rounded down
    assert (
        rtc.text('machine JigContracts "rtc"')
        == "found Jig.ST.STM32U5_RTC IRealTimeClock IPowerCycled"
    )


@pytest.mark.parametrize("text", ["1999-12-31T23:59:59", "2100-01-01T00:00:00", "noon"])
def test_set_date_time_refuses_what_the_clock_cannot_hold(rtc: Bench, text: str) -> None:
    with pytest.raises(RenodeError):
        rtc.execute(f'rtc SetDateTime "{text}"')


def configured(bench: Bench) -> None:
    """A calendar, a prescaler, CR and a backup register a firmware would set."""
    unlock(bench)
    write(bench, ICSR, INIT)
    write(bench, PRER, 0x007F00FF)
    write(bench, TR, time_register(12, 0, 0))
    write(bench, DR, date_register(2026, 10, 9, 5))
    write(bench, ICSR, 0)
    write(bench, CR, BYPSHAD)
    lock(bench)
    bench.write(TAMP + BKP0R, 0x20230315)
    bench.write(TAMP + BKP31R, 0xA5A5A5A5)


def test_the_backup_domain_survives_system_resets(rtc: Bench) -> None:
    configured(rtc)
    rtc.run_ms(1000.5)
    unlock(rtc)
    for cause in ("Pin", "Software", "IndependentWatchdog"):
        rtc.execute(f"rcc NextResetCause {cause}")
        rtc.execute("machine Reset")
    rtc.run_ms(1000)
    assert date_time(rtc) == "2026-10-09T12:00:02.000"
    assert read(rtc, CR) == BYPSHAD
    assert rtc.read(TAMP + BKP0R) == 0x20230315 and rtc.read(TAMP + BKP31R) == 0xA5A5A5A5
    write(rtc, CR, 0)  # a system reset locks the registers again
    assert read(rtc, CR) == BYPSHAD


def test_a_system_reset_ends_initialization_mode(rtc: Bench) -> None:
    configured(rtc)
    unlock(rtc)
    write(rtc, ICSR, INIT)
    rtc.execute("machine Reset")
    assert read(rtc, ICSR) & (INIT | INITF) == 0
    rtc.run_ms(1000.5)
    assert date_time(rtc) == "2026-10-09T12:00:01.000"


def assert_backup_domain_reset(bench: Bench) -> None:
    assert date_time(bench) == "2000-01-01T00:00:00.000"
    assert (read(bench, DR), read(bench, CR), read(bench, PRER)) == (0x2101, 0, 0x007F00FF)
    assert bench.read(TAMP + BKP0R) == 0 and bench.read(TAMP + BKP31R) == 0


def test_a_power_on_reset_clears_the_backup_domain(rtc: Bench) -> None:
    configured(rtc)
    rtc.execute("rcc NextResetCause PowerOn")
    rtc.execute("machine Reset")
    assert_backup_domain_reset(rtc)
    rtc.run_ms(1000.5)  # the calendar runs again from its reset value
    assert date_time(rtc) == "2000-01-01T00:00:01.000"


def test_bdrst_clears_the_backup_domain(rtc: Bench) -> None:
    configured(rtc)
    rtc.write(RCC + BDCR, BDRST)
    rtc.write(RCC + BDCR, 0)
    assert_backup_domain_reset(rtc)


def test_power_cycle_clears_the_backup_domain(rtc: Bench) -> None:
    configured(rtc)
    rtc.execute("rtc PowerCycle")
    rtc.execute("tamp PowerCycle")
    assert_backup_domain_reset(rtc)
    assert rtc.text('machine JigContracts "tamp"') == "found Jig.ST.STM32U5_TAMP IPowerCycled"


def test_tamp_stores_its_registers_and_counts_monotonically(rtc: Bench) -> None:
    pattern = {o: o * 0x01010101 & 0xFFFFFFFF for o in range(BKP0R, BKP31R + 4, 4)}
    for offset, value in pattern.items():
        rtc.write(TAMP + offset, value)
    assert {o: rtc.read(TAMP + o) for o in pattern} == pattern
    rtc.write(TAMP + 0x24, 0x8000)  # PRIVCFGR
    assert rtc.read(TAMP + 0x24) == 0x8000
    rtc.write(TAMP + COUNTR, 0)
    rtc.write(TAMP + COUNTR, 0x1234)
    assert rtc.read(TAMP + COUNTR) == 2  # each write adds one
    rtc.write(TAMP + TAMP_SR, 0xFF)
    assert rtc.read(TAMP + TAMP_SR) == 0  # no tamper events


STOCK = """\
stockRtc: Timers.STM32F4_RTC @ sysbus 0x40002800
"""

STOCK_RTC = 0x40002800  # STM32F4 layout: TR 0x00, DR 0x04, ISR 0x0C (INIT bit 7), WPR 0x24


def test_renodes_stock_rtc_restarts_its_calendar_on_a_system_reset(bench: Bench) -> None:
    # Renode's STM32F4_RTC returns its calendar to the reset date on every
    # machine reset, while on silicon the RTC lives in the backup domain. When
    # a Renode release fixes it, this test fails.
    bench.load(STOCK)
    reset_date = bench.read(STOCK_RTC + DR)
    bench.write(STOCK_RTC + WPR, 0xCA)
    bench.write(STOCK_RTC + WPR, 0x53)
    bench.write(STOCK_RTC + ICSR, INIT)
    bench.write(STOCK_RTC + DR, date_register(2026, 10, 9, 5))
    bench.write(STOCK_RTC + ICSR, 0)
    assert bench.read(STOCK_RTC + DR) == date_register(2026, 10, 9, 5)
    bench.execute("machine Reset")
    assert bench.read(STOCK_RTC + DR) == reset_date
