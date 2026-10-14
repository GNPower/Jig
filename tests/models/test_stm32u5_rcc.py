"""STM32U5 reset and clock control (Jig.ST.STM32U5_RCC, ST RM0456 "Reset and
clock control (RCC)"): the reset values, ready flags that follow their
enables, CFGR1.SWS following SW, the reset flags of RCC_CSR (power-on, causes
announced through NextResetCause, AIRCR.SYSRESETREQ through the NVIC, RMVF),
and RCC_BDCR as backup-domain state with its BackupDomainReset output."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

RCC = 0x46020C00
CR, ICSCR1, ICSCR2, ICSCR3, CFGR1 = 0x00, 0x08, 0x0C, 0x10, 0x1C
PLL1DIVR, PLL2DIVR, PLL3DIVR = 0x34, 0x3C, 0x44
CIER, CIFR, AHB1ENR, CCIPR1 = 0x50, 0x54, 0x88, 0xE0
BDCR, CSR = 0xF0, 0xF4

OBLRSTF, PINRSTF, BORRSTF, SFTRSTF = 1 << 25, 1 << 26, 1 << 27, 1 << 28
IWDGRSTF, WWDGRSTF, LPWRRSTF = 1 << 29, 1 << 30, 1 << 31
RESET_FLAGS = 0xFE000000
RMVF = 1 << 23

# RCC_BDCR
LSEON, LSERDY, LSESYSEN, LSESYSRDY = 1 << 0, 1 << 1, 1 << 7, 1 << 11
RTCSEL_LSE, RTCEN, BDRST, LSION, LSIRDY = 0b01 << 8, 1 << 15, 1 << 16, 1 << 26, 1 << 27

MACHINE = """\
rcc: Jig.ST.STM32U5_RCC @ sysbus 0x46020C00
    BackupDomainReset -> probe@0

probe: JigFixtures.GpioProbe @ sysbus
"""

# The NVIC's SYSRESETREQ path needs a core (the NVIC's owner) and the NVIC,
# and the core needs a program once the reset starts it.
WITH_CORE = """\
cpu: CPU.CortexM @ sysbus
    cpuType: "cortex-m33"
    nvic: nvic

nvic: IRQControllers.NVIC @ sysbus 0xE000E000
    priorityMask: 0xF0
    IRQ -> cpu@0
    SystemResetRequest -> rcc@0

flash: Memory.MappedMemory @ sysbus 0x08000000
    size: 0x1000

sram: Memory.MappedMemory @ sysbus 0x20000000
    size: 0x1000
"""

# Initial SP, the reset vector (Thumb), then WFI and a branch back to it.
IDLE_PROGRAM = (0x20001000, 0x08000009, 0xE7FDBF30)


@pytest.fixture
def rcc(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def read(bench: Bench, offset: int) -> int:
    return bench.read(RCC + offset)


def write(bench: Bench, offset: int, value: int) -> None:
    bench.write(RCC + offset, value)


def reset_with(bench: Bench, cause: str) -> None:
    bench.execute(f"rcc NextResetCause {cause}")
    bench.execute("machine Reset")


def last_cause(bench: Bench) -> str:
    return bench.text("rcc LastResetCause")


def test_reset_values(rcc: Bench) -> None:
    expected = {
        CR: 0x00000035,  # MSIS and MSIK on and ready
        ICSCR1: 0x44000000,
        ICSCR2: 0x00084210,
        ICSCR3: 0x00100000,
        CFGR1: 0,
        PLL1DIVR: 0x01010280,
        PLL2DIVR: 0x01010280,
        PLL3DIVR: 0x01010280,
        AHB1ENR: 0,
        BDCR: 0,
        CSR: 0x0C004400,  # BORRSTF and PINRSTF after power-on, MSI ranges 4
    }
    assert {offset: read(rcc, offset) for offset in expected} == expected


@pytest.mark.parametrize(
    ("on", "ready"),
    [
        (1 << 0, 1 << 2),  # MSISON, MSISRDY
        (1 << 4, 1 << 5),  # MSIKON, MSIKRDY
        (1 << 8, 1 << 10),  # HSION, HSIRDY
        (1 << 12, 1 << 13),  # HSI48ON, HSI48RDY
        (1 << 14, 1 << 15),  # SHSION, SHSIRDY
        (1 << 16, 1 << 17),  # HSEON, HSERDY
        (1 << 24, 1 << 25),  # PLL1ON, PLL1RDY
        (1 << 26, 1 << 27),  # PLL2ON, PLL2RDY
        (1 << 28, 1 << 29),  # PLL3ON, PLL3RDY
    ],
)
def test_each_ready_flag_follows_its_enable(rcc: Bench, on: int, ready: int) -> None:
    write(rcc, CR, on)
    assert read(rcc, CR) & (on | ready) == on | ready
    write(rcc, CR, 0)
    assert read(rcc, CR) & ready == 0


def test_ready_flags_cannot_be_written(rcc: Bench) -> None:
    write(rcc, CR, 1 << 25 | 1 << 17)  # PLL1RDY and HSERDY alone
    assert read(rcc, CR) == 0


def test_backup_domain_oscillators_report_ready(rcc: Bench) -> None:
    write(rcc, BDCR, LSEON)
    assert read(rcc, BDCR) == LSEON | LSERDY
    write(rcc, BDCR, LSEON | LSESYSEN | LSION)
    assert read(rcc, BDCR) == LSEON | LSERDY | LSESYSEN | LSESYSRDY | LSION | LSIRDY
    write(rcc, BDCR, LSESYSEN)  # LSESYSRDY needs LSEON as well
    assert read(rcc, BDCR) == LSESYSEN


@pytest.mark.parametrize("source", [0, 1, 2, 3])  # MSIS, HSI16, HSE, PLL1
def test_sws_reports_the_selected_system_clock(rcc: Bench, source: int) -> None:
    write(rcc, CFGR1, source)
    assert read(rcc, CFGR1) == source | source << 2


def test_clock_interrupt_flags_stay_clear(rcc: Bench) -> None:
    write(rcc, CIER, 0xFFFF)
    assert read(rcc, CIER) == 0xFFFF
    assert read(rcc, CIFR) == 0


def test_other_registers_store_what_software_writes(rcc: Bench) -> None:
    write(rcc, AHB1ENR, 0x12345678)
    write(rcc, CCIPR1, 0x0000C003)
    assert read(rcc, AHB1ENR) == 0x12345678
    assert read(rcc, CCIPR1) == 0x0000C003
    rcc.execute("machine Reset")
    assert read(rcc, AHB1ENR) == 0  # a system reset clears them


def test_the_first_reset_is_a_power_on_reset(rcc: Bench) -> None:
    assert read(rcc, CSR) & RESET_FLAGS == BORRSTF | PINRSTF
    assert last_cause(rcc) == "PowerOn"


def test_rmvf_clears_the_reset_flags(rcc: Bench) -> None:
    write(rcc, CSR, RMVF | 0x4400)
    assert read(rcc, CSR) == 0x00004400
    assert read(rcc, CSR) & RMVF == 0  # RMVF reads 0


@pytest.mark.parametrize(
    ("cause", "flag"),
    [
        ("Pin", 0),
        ("Software", SFTRSTF),
        ("IndependentWatchdog", IWDGRSTF),
        ("WindowWatchdog", WWDGRSTF),
        ("LowPower", LPWRRSTF),
        ("OptionByteLoad", OBLRSTF),
    ],
)
def test_an_announced_reset_adds_its_flag_and_pinrstf(rcc: Bench, cause: str, flag: int) -> None:
    write(rcc, CSR, RMVF)
    reset_with(rcc, cause)
    assert read(rcc, CSR) & RESET_FLAGS == PINRSTF | flag
    assert last_cause(rcc) == cause


def test_flags_accumulate_until_rmvf(rcc: Bench) -> None:
    reset_with(rcc, "Software")
    reset_with(rcc, "IndependentWatchdog")
    assert read(rcc, CSR) & RESET_FLAGS == BORRSTF | PINRSTF | SFTRSTF | IWDGRSTF
    write(rcc, CSR, RMVF)
    assert read(rcc, CSR) & RESET_FLAGS == 0


def test_a_power_on_reset_replaces_the_flags(rcc: Bench) -> None:
    reset_with(rcc, "Software")
    reset_with(rcc, "PowerOn")
    assert read(rcc, CSR) & RESET_FLAGS == BORRSTF | PINRSTF


def test_an_unannounced_reset_reports_the_watchdog(rcc: Bench) -> None:
    write(rcc, CSR, RMVF)
    rcc.execute("machine Reset")
    assert read(rcc, CSR) & RESET_FLAGS == PINRSTF | IWDGRSTF
    rcc.execute("rcc UnannouncedResetCause WindowWatchdog")
    rcc.execute("machine Reset")
    assert last_cause(rcc) == "WindowWatchdog"


def test_an_announcement_lasts_one_reset(rcc: Bench) -> None:
    reset_with(rcc, "Pin")
    write(rcc, CSR, RMVF)
    rcc.execute("machine Reset")
    assert last_cause(rcc) == "IndependentWatchdog"


def test_sysresetreq_reports_a_software_reset(bench: Bench) -> None:
    bench.load(MACHINE + "\n" + WITH_CORE)
    for index, word in enumerate(IDLE_PROGRAM):
        bench.write(0x08000000 + 4 * index, word)
    bench.execute("cpu InitVectorTableOffset 0x08000000")
    bench.execute("cpu IsHalted true")
    bench.write(RCC + CSR, RMVF)
    # AIRCR with VECTKEY 0x05FA and SYSRESETREQ, as CMSIS NVIC_SystemReset writes it
    bench.write(0xE000ED0C, 0x05FA0004, cpu="cpu")
    bench.run_us(200)  # the reset happens at the next synchronisation point
    assert bench.read(RCC + CSR) & RESET_FLAGS == PINRSTF | SFTRSTF
    assert last_cause(bench) == "Software"


def test_bdcr_survives_a_system_reset(rcc: Bench) -> None:
    value = LSEON | LSESYSEN | RTCSEL_LSE | RTCEN
    write(rcc, BDCR, value)
    reset_with(rcc, "Pin")
    reset_with(rcc, "Software")
    assert read(rcc, BDCR) == value | LSERDY | LSESYSRDY
    assert rcc.number("probe Rises 0") == 0


def test_a_power_on_reset_clears_bdcr_and_pulses_the_backup_domain_reset(rcc: Bench) -> None:
    write(rcc, BDCR, LSEON | RTCSEL_LSE | RTCEN)
    reset_with(rcc, "PowerOn")
    assert read(rcc, BDCR) == 0
    assert rcc.number("probe Rises 0") == 1
    assert not rcc.flag("probe Level 0")


def test_bdrst_holds_the_backup_domain_in_reset(rcc: Bench) -> None:
    write(rcc, BDCR, LSEON | RTCSEL_LSE | RTCEN)
    write(rcc, BDCR, read(rcc, BDCR) | BDRST)  # read-modify-write, as LL_RCC_ForceBackupDomainReset
    assert read(rcc, BDCR) == BDRST  # the rest of the register resets with the domain
    assert rcc.flag("probe Level 0")
    write(rcc, BDCR, read(rcc, BDCR) & ~BDRST)  # LL_RCC_ReleaseBackupDomainReset
    assert read(rcc, BDCR) == 0
    assert not rcc.flag("probe Level 0")
    assert rcc.number("probe Rises 0") == 1
