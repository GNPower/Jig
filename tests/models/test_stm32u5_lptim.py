"""STM32U5 low-power timer (Jig.ST.STM32U5_LPTIM, ST RM0456 "Low-power timer
(LPTIM)"): the reset values, the OK flags register writes raise, counting at
the kernel clock over CFGR.PRESC, ARRM and the update event (UE) each period,
the repetition counter, CC1IF at CCR1 including CCR1 = 0 after the wrap, the
interrupt line, single and continuous mode, COUNTRST and disabling.

The last tests pin the defects of Renode's STM32L0_LpTimer this model
replaces on the STM32U5 platform: they fail once a Renode release fixes
them."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

LPTIM = 0x46004400
ISR, ICR, DIER, CFGR, CR, CCR1, ARR, CNT, RCR, CCR2 = (
    0x00,
    0x04,
    0x08,
    0x0C,
    0x10,
    0x14,
    0x18,
    0x1C,
    0x28,
    0x34,
)

CC1IF, ARRM, CMP1OK, ARROK, UE, REPOK, CMP2OK, DIEROK = (
    1 << 0,
    1 << 1,
    1 << 3,
    1 << 4,
    1 << 7,
    1 << 8,
    1 << 19,
    1 << 24,
)
ALL_FLAGS = 0x010833FF
ENABLE, SNGSTRT, CNTSTRT, COUNTRST = 1 << 0, 1 << 1, 1 << 2, 1 << 3
PRESC_DIV8 = 0b011 << 9

KERNEL_HZ = 32768

MACHINE = """\
lptim: Jig.ST.STM32U5_LPTIM @ sysbus 0x46004400
    frequency: 32768
    IRQ -> probe@0

probe: JigFixtures.GpioProbe @ sysbus
"""


@pytest.fixture
def lptim(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def read(bench: Bench, offset: int) -> int:
    return bench.read(LPTIM + offset)


def write(bench: Bench, offset: int, value: int) -> None:
    bench.write(LPTIM + offset, value)


def start(bench: Bench, *, arr: int, presc: int = 0, mode: int = CNTSTRT, rcr: int = 0) -> None:
    """Configure and start the counter, then clear every flag the set-up raised."""
    write(bench, CFGR, presc)
    write(bench, CR, ENABLE)
    write(bench, ARR, arr)
    write(bench, RCR, rcr)
    write(bench, CR, ENABLE | mode)
    write(bench, ICR, ALL_FLAGS)


def test_reset_values(lptim: Bench) -> None:
    expected = {ISR: 0, DIER: 0, CFGR: 0, CR: 0, CCR1: 0, ARR: 1, CNT: 0, RCR: 0}
    assert {offset: read(lptim, offset) for offset in expected} == expected


@pytest.mark.parametrize(
    ("offset", "flag"),
    [(ARR, ARROK), (CCR1, CMP1OK), (CCR2, CMP2OK), (RCR, REPOK), (DIER, DIEROK)],
)
def test_a_register_write_raises_its_ok_flag(lptim: Bench, offset: int, flag: int) -> None:
    write(lptim, CR, ENABLE)
    write(lptim, offset, 0x10 if offset != DIER else 0)
    assert read(lptim, ISR) == flag
    write(lptim, ICR, flag)
    assert read(lptim, ISR) == 0


@pytest.mark.parametrize(("presc", "divider"), [(0, 1), (PRESC_DIV8, 8)])
def test_the_counter_runs_at_the_kernel_clock_over_presc(
    lptim: Bench, presc: int, divider: int
) -> None:
    start(lptim, arr=0xFFFF, presc=presc)
    lptim.run_ms(100)
    assert read(lptim, CNT) == KERNEL_HZ // divider // 10  # 100 ms of counts, rounded down


def test_arrm_and_the_update_event_once_per_period(lptim: Bench) -> None:
    start(lptim, arr=KERNEL_HZ - 1)  # a 1 s period
    lptim.run_ms(999.9)
    assert read(lptim, ISR) == 0
    lptim.run_ms(0.2)  # CNT reached ARR at 0.99997 s and wrapped at 1 s
    # CCR1 keeps its reset value 0, which matches at the wrap
    assert read(lptim, ISR) == CC1IF | ARRM | UE
    assert read(lptim, CNT) == 3
    write(lptim, ICR, CC1IF | ARRM | UE)
    lptim.run_ms(1000)
    assert read(lptim, ISR) == CC1IF | ARRM | UE


def test_cc1if_when_the_counter_reaches_ccr1(lptim: Bench) -> None:
    start(lptim, arr=0xFFFF)
    write(lptim, CCR1, 100)
    write(lptim, ICR, CMP1OK)
    lptim.run_us(3000)  # 98 counts
    assert read(lptim, ISR) & CC1IF == 0
    lptim.run_us(100)  # 101 counts
    assert read(lptim, ISR) & CC1IF


def test_a_compare_value_of_0_matches_after_each_wrap(lptim: Bench) -> None:
    # The sequence of the upstream report: 4096 counts per second, a 16 s
    # period, CCR1 = 0xFFFE, then CCR1 = 0 written just before the wrap.
    start(lptim, arr=0xFFFF, presc=PRESC_DIV8)
    write(lptim, CCR1, 0xFFFE)
    lptim.run_ms(15999.6)
    assert read(lptim, CNT) == 0xFFFE
    assert read(lptim, ISR) & CC1IF
    write(lptim, ICR, ALL_FLAGS)
    write(lptim, CCR1, 0)
    write(lptim, ICR, CMP1OK)
    lptim.run_ms(0.5)  # 16.0001 s: wrapped at 16 s
    assert read(lptim, CNT) == 0
    assert read(lptim, ISR) == CC1IF | ARRM | UE
    write(lptim, ICR, ALL_FLAGS)
    lptim.run_ms(16000)  # 32.0001 s
    assert read(lptim, CNT) == 0
    assert read(lptim, ISR) == CC1IF | ARRM | UE


def test_the_repetition_counter_spaces_the_update_events(lptim: Bench) -> None:
    start(lptim, arr=32, rcr=2)  # 33 counts a period, UE every third overflow
    period_us = 33 / KERNEL_HZ * 1e6
    lptim.run_us(2.5 * period_us)
    assert read(lptim, ISR) & UE == 0
    lptim.run_us(1.0 * period_us)  # third overflow
    assert read(lptim, ISR) & UE
    write(lptim, ICR, UE)
    lptim.run_us(2.0 * period_us)
    assert read(lptim, ISR) & UE == 0
    lptim.run_us(1.0 * period_us)  # sixth overflow
    assert read(lptim, ISR) & UE


def test_the_interrupt_follows_the_flags_and_dier(lptim: Bench) -> None:
    start(lptim, arr=KERNEL_HZ // 100 - 1)  # a 10 ms period
    write(lptim, DIER, UE)
    write(lptim, ICR, DIEROK)
    assert not lptim.flag("probe Level 0")
    lptim.run_ms(10.1)
    assert lptim.flag("probe Level 0")
    write(lptim, ICR, UE)
    assert not lptim.flag("probe Level 0")
    write(lptim, DIER, ARRM)  # ARRM is set, now enabled
    assert lptim.flag("probe Level 0")


def test_single_mode_stops_after_one_period(lptim: Bench) -> None:
    start(lptim, arr=KERNEL_HZ // 100 - 1, mode=SNGSTRT)
    lptim.run_ms(25)
    assert read(lptim, ISR) == CC1IF | ARRM | UE  # CC1IF: CCR1 = 0 at the wrap
    assert read(lptim, CNT) == 0
    write(lptim, ICR, ALL_FLAGS)
    lptim.run_ms(25)
    assert read(lptim, ISR) == 0  # stopped


def test_countrst_and_disable_clear_the_counter(lptim: Bench) -> None:
    start(lptim, arr=0xFFFF)
    lptim.run_ms(10)
    assert read(lptim, CNT) == 327
    write(lptim, CR, ENABLE | COUNTRST)
    assert read(lptim, CNT) == 0
    lptim.run_ms(10)
    # The kernel clock keeps its phase: its edges fall at k / 32768 s, and
    # 328 of them (k = 328 to 655) follow the reset at 10 ms within 20 ms.
    assert read(lptim, CNT) == 328
    write(lptim, CR, 0)
    assert read(lptim, CNT) == 0
    lptim.run_ms(10)
    assert read(lptim, CNT) == 0


STOCK = """\
stockTimer: Timers.STM32L0_LpTimer @ sysbus 0x46004400
    frequency: 32768
"""

# Renode's STM32L0_LpTimer: CMP at 0x14, ISR.CMPM bit 0, ARRM bit 1; IER at 0x08.
CMPM = 1 << 0


def run_upstream_sequence(bench: Bench) -> list[tuple[int, int]]:
    """The register sequence of the upstream report on Renode's stock model:
    (CNT, ISR) at 15.9996 s, 16.0001 s and 32.0001 s."""
    bench.load(STOCK)
    write(bench, CFGR, PRESC_DIV8)
    write(bench, CR, ENABLE)
    write(bench, DIER, 0x9)  # CMPMIE | CMPOKIE
    write(bench, ARR, 0xFFFF)
    write(bench, CCR1, 0xFFFE)
    write(bench, CR, ENABLE | CNTSTRT)
    write(bench, ICR, 0x1F)
    samples = []
    bench.run_ms(15999.6)
    samples.append((read(bench, CNT), read(bench, ISR)))
    write(bench, ICR, 0x1F)
    write(bench, CCR1, 0)
    write(bench, ICR, 0x8)
    bench.run_ms(0.5)
    samples.append((read(bench, CNT), read(bench, ISR)))
    bench.run_ms(16000)
    samples.append((read(bench, CNT), read(bench, ISR)))
    return samples


def test_renodes_stock_lptimer_never_matches_a_compare_value_of_0(bench: Bench) -> None:
    # Renode's STM32L0_LpTimer refuses CMP = 0 ("Compare value cannot be 0"),
    # so the match after the wrap never comes. When a Renode release fixes
    # it, this test fails and STM32U5_LPTIM's reason to exist shrinks.
    samples = run_upstream_sequence(bench)
    assert samples[0][1] & CMPM  # CMP = 0xFFFE matched
    assert samples[1][1] & CMPM == 0
    assert samples[2][1] & CMPM == 0


def test_renodes_stock_lptimer_wraps_after_arr_counts(bench: Bench) -> None:
    # A period of ARR counts instead of ARR + 1: CNT gains a count each period.
    samples = run_upstream_sequence(bench)
    assert [cnt for cnt, _isr in samples[1:]] == [1, 2]


def test_renodes_limit_timer_checks_a_value_against_its_first_limit(bench: Bench) -> None:
    # Renode's LimitTimer refuses a Value above the limit it was constructed
    # with, whatever its limit is now, so STM32U5_LPTIM and STM32U5_RTC build
    # their timers with the largest limit they use. When a Renode release
    # checks the current limit, this test fails.
    bench.load(MACHINE)
    session = bench.session
    session.select_machine(bench.name)
    session.python(
        "from Antmicro.Renode.Peripherals.Timers import LimitTimer\n"
        "jig_timer = LimitTimer(self.Machine.ClockSource, 1000, None, 'jig_timer', 10)\n"
        "jig_timer.Limit = 100\n"
        "jig_timer.Value = 10\n"
        "try:\n"
        "    jig_timer.Value = 50\n"
        "    jig_value_check = 'accepted'\n"
        "except Exception:\n"
        "    jig_value_check = 'refused'\n"
    )
    assert session.python("jig_timer.Value") == "10"  # up to the first limit is taken
    assert session.python("jig_value_check") == "refused"


def test_renodes_stock_lptimer_has_no_update_event(bench: Bench) -> None:
    bench.load(STOCK)
    write(bench, CR, ENABLE)
    write(bench, DIER, UE)
    write(bench, ARR, 0xFF)
    write(bench, CR, ENABLE | CNTSTRT)
    bench.run_ms(20)  # more than two periods
    assert read(bench, ISR) & UE == 0
