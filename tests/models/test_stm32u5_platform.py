"""The STM32U5 platforms Jig ships (soc_platform st/stm32u575 and
st/stm32u595, ST RM0456 and ST's CMSIS device headers): each loads and runs
1 ms with its core halted; flash, SRAM and the SRAM1 alias sit where the
part has them; the system-memory words; Jig's models at their addresses; the
backup domain wired from the RCC to the RTC and TAMP; ADC4 inputs across a
reset a model requests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from jig.renode import platforms

from .bench import Bench

pytestmark = pytest.mark.renode


@dataclass(frozen=True)
class Part:
    name: str
    flash_kb: int
    srams: tuple[tuple[int, int], ...]  # (base, size)


U575 = Part(
    "st/stm32u575",
    2048,
    ((0x20000000, 0x30000), (0x20030000, 0x10000), (0x20040000, 0x80000), (0x28000000, 0x4000)),
)
U595 = Part(
    "st/stm32u595",
    4096,
    (
        (0x20000000, 0xC0000),
        (0x200C0000, 0x10000),
        (0x200D0000, 0xD0000),
        (0x28000000, 0x4000),
        (0x201A0000, 0xD0000),
    ),
)
PARTS = pytest.mark.parametrize("part", [U575, U595], ids=["u575", "u595"])

FLASH = 0x08000000
FLASHSIZE, TS_CAL1, TS_CAL2 = 0x0BFA07A0, 0x0BFA0710, 0x0BFA0742
SRAM1_ALIAS = 0x0A000000
RCC, RTC, TAMP, LPTIM1, LPTIM2, FDCAN1, ADC4 = (
    0x46020C00,
    0x46007800,
    0x46007C00,
    0x46004400,
    0x40009400,
    0x4000A400,
    0x46021000,
)


# Initial SP (in SRAM1), the reset vector (Thumb), then WFI and a branch back to it.
IDLE_PROGRAM = (0x20030000, 0x08000009, 0xE7FDBF30)


def load(bench: Bench, part: Part) -> Bench:
    bench.execute(f"machine LoadPlatformDescription @{platforms.path(part.name).as_posix()}")
    bench.execute("cpu IsHalted true")  # no firmware: the core stays halted
    return bench


def idle_after_reset(bench: Bench) -> None:
    """A program for the core once a reset starts it: it sleeps in WFI."""
    for index, word in enumerate(IDLE_PROGRAM):
        bench.write(FLASH + 4 * index, word)
    bench.execute(f"cpu InitVectorTableOffset {FLASH:#x}")


def test_the_platforms_are_listed() -> None:
    assert {"st/stm32u5", "st/stm32u575", "st/stm32u595"} <= set(platforms.available())


@PARTS
def test_each_part_loads_and_runs_1_ms(bench: Bench, part: Part) -> None:
    load(bench, part)
    bench.run_ms(1)
    assert bench.session.elapsed_ns >= 1_000_000


@PARTS
def test_flash_and_sram_where_the_part_has_them(bench: Bench, part: Part) -> None:
    load(bench, part)
    last_flash_word = FLASH + part.flash_kb * 1024 - 4
    bench.write(last_flash_word, 0x600DF1A5)
    assert bench.read(last_flash_word) == 0x600DF1A5
    for base, size in part.srams:
        bench.write(base, base ^ 0x5A5A5A5A)
        bench.write(base + size - 4, size)
        assert (bench.read(base), bench.read(base + size - 4)) == (base ^ 0x5A5A5A5A, size)
    bench.write(SRAM1_ALIAS + 0x100, 0x11223344)  # the C-bus alias of SRAM1
    assert bench.read(0x20000100) == 0x11223344


@PARTS
def test_the_system_memory_words(bench: Bench, part: Part) -> None:
    load(bench, part)
    assert bench.read_word(FLASHSIZE) == part.flash_kb
    assert (bench.read_word(TS_CAL1), bench.read_word(TS_CAL2)) == (0x1000, 0x1590)


@PARTS
def test_jigs_models_at_their_addresses(bench: Bench, part: Part) -> None:
    load(bench, part)
    assert bench.read(RCC) == 0x35  # RCC_CR
    assert bench.read(RTC + 0x04) == 0x2101  # RTC_DR
    assert bench.read(LPTIM1 + 0x18) == 1 and bench.read(LPTIM2 + 0x18) == 1  # LPTIM_ARR
    assert bench.read(FDCAN1) == 0x32141218  # FDCAN_CREL
    expected = {
        "rcc": "Jig.ST.STM32U5_RCC",
        "rtc": "Jig.ST.STM32U5_RTC IRealTimeClock IPowerCycled",
        "tamp": "Jig.ST.STM32U5_TAMP IPowerCycled",
        "adc4": "Jig.ST.STM32U5_ADC4 IAnalogInputs=14",
        "fdcan1": "Jig.ST.STM32U5_FDCAN",
        "lptim2": "Jig.ST.STM32U5_LPTIM",
    }
    for name, reply in expected.items():
        assert bench.text(f'machine JigContracts "{name}"') == f"found {reply}"


@PARTS
def test_the_backup_domain_is_wired_from_the_rcc(bench: Bench, part: Part) -> None:
    load(bench, part)
    bench.execute('rtc SetDateTime "2026-10-09T12:00:00"')
    bench.write(TAMP + 0x100, 0x20230315)
    bench.execute("machine Reset")  # unannounced: a watchdog reset
    assert bench.text("rcc LastResetCause") == "IndependentWatchdog"
    assert bench.text("rtc GetDateTime") == "2026-10-09T12:00:00.000"
    assert bench.read(TAMP + 0x100) == 0x20230315
    bench.execute("rcc NextResetCause PowerOn")
    bench.execute("machine Reset")
    assert bench.text("rtc GetDateTime") == "2000-01-01T00:00:00.000"
    assert bench.read(TAMP + 0x100) == 0


@PARTS
def test_adc4_inputs_survive_a_reset_a_model_requests(bench: Bench, part: Part) -> None:
    load(bench, part)
    idle_after_reset(bench)
    bench.execute("adc4 SetChannelVoltage 13 0.736")
    bench.execute("rcc OnGPIO 0 true")  # the NVIC's SYSRESETREQ line
    bench.run_us(200)  # the reset happens at the next synchronisation point
    assert bench.text("rcc LastResetCause") == "Software"
    assert not bench.flag("cpu IsHalted")  # the reset started the core: it sleeps in WFI
    bench.write(ADC4 + 0x08, 1)  # ADEN
    bench.write(ADC4 + 0x28, 1 << 13)  # CHSELR: channel 13
    bench.write(ADC4 + 0x08, 0b101)  # ADSTART
    assert bench.read(ADC4 + 0x40) == 913
