"""Jig's stm32 family on the STM32U5 platforms (parts STM32U575 and
STM32U595: st/stm32u575 and st/stm32u595). The firmware is an ELF the test writes: a
two-word vector table and a WFI loop, so no toolchain is needed. The driver
boots it, tells the RCC each reset's cause (Pin for reset, PowerOn for the
power-on reset), holds a powered-off board in a power-on reset so a running
watchdog cannot restart it, and leaves Renode alone where the RCC records no
causes. Through a Simulation, a board file's sensors and rtc reach the U5 ADC4
and RTC models, and both behave as the backup domain and the analog pins do
across resets and power cycles."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from jig.renode import platforms
from jig.system import boards
from jig.system.socs import MachineConfig
from jig.system.socs.stm32 import Stm32Driver
from jig.system.system import Simulation

from .bench import Bench

pytestmark = pytest.mark.renode

FLASH = 0x08000000
RCC_CSR = 0x46020CF4
TAMP_BKP0R = 0x46007D00
IWDG_KR = 0x40003000
PINRSTF, BORRSTF, IWDGRSTF = 1 << 26, 1 << 27, 1 << 29


def idle_elf(path: Path, stack: int = 0x20030000) -> Path:
    """An ARM ELF with one segment at 0x08000000: the initial SP, the reset
    vector (Thumb), then WFI and a branch back to it."""
    image = struct.pack("<IIHH", stack, FLASH + 9, 0xBF30, 0xE7FD)
    data_offset = 0x60
    header = struct.pack(
        "<16sHHIIIIIHHHHHH",
        b"\x7fELF\x01\x01\x01" + bytes(9),  # 32-bit, little-endian, version 1
        2,  # ET_EXEC
        40,  # EM_ARM
        1,
        FLASH + 9,  # entry
        52,  # program headers right after this header
        0,  # no section headers
        0x05000200,  # EABI version 5, soft float
        52,
        32,
        1,
        40,
        0,
        0,
    )
    segment = struct.pack(
        "<IIIIIIII", 1, data_offset, FLASH, FLASH, len(image), len(image), 5, 4
    )  # PT_LOAD, read and execute
    blob = header + segment
    path.write_bytes(blob + bytes(data_offset - len(blob)) + image)
    return path


def board_directory(root: Path, part: str) -> Path:
    """A board on a Jig U5 platform: the ELF, a sensor on ADC4 and the RTC."""
    directory = root / "u5-board"
    directory.mkdir(parents=True)
    idle_elf(directory / "idle.elf")
    (directory / "board.jig").write_text(
        'board "u5-board" {\n'
        '    root: "."\n'
        "    rtc: mcu.rtc\n"
        f'    device mcu: stm32(part: "{part}", mips: 160) {{\n'
        '        firmware: "idle.elf"\n'
        "    }\n"
        "    sensor temperature: mcu.adc4.channel[13]\n"
        "}\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def u5(bench: Bench, tmp_path: Path) -> tuple[Bench, Stm32Driver, MachineConfig]:
    """A U575 machine (no overlay: the platform alone) and a driver for it."""
    board = boards.load(board_directory(tmp_path, "STM32U575ZIT6") / "board.jig")
    bench.execute(f"machine LoadPlatformDescription @{platforms.path('st/stm32u575').as_posix()}")
    config = MachineConfig(bench.name, board, "flash", 160, tmp_path / "work")
    return bench, Stm32Driver(), config


def cause(bench: Bench) -> str:
    return bench.text("rcc LastResetCause")


def test_the_driver_finds_an_rcc_that_records_causes(
    u5: tuple[Bench, Stm32Driver, MachineConfig], tmp_path: Path
) -> None:
    bench, driver, config = u5
    assert driver.records_reset_causes(config, bench.session)
    stock = Bench(bench.session, tmp_path / "stock")
    try:
        stock.load(
            "rtc: Timers.STM32F4_RTC @ sysbus 0x40002800\n\n"
            "rcc: Miscellaneous.STM32F4_RCC @ sysbus 0x40023800\n"
            "    rtcPeripheral: rtc\n"
        )
        other = MachineConfig(stock.name, config.board, "flash", 160, tmp_path / "other")
        assert not driver.records_reset_causes(other, bench.session)
    finally:
        stock.remove()


def test_boot_runs_the_image_from_flash(u5: tuple[Bench, Stm32Driver, MachineConfig]) -> None:
    bench, driver, config = u5
    driver.boot(config, bench.session)
    bench.run_ms(1)
    assert not bench.flag("cpu IsHalted")
    assert FLASH + 8 <= bench.number("cpu PC") <= FLASH + 10  # in the WFI loop
    assert cause(bench) == "PowerOn"


def test_reset_reports_a_pin_reset_and_keeps_the_backup_domain(
    u5: tuple[Bench, Stm32Driver, MachineConfig],
) -> None:
    bench, driver, config = u5
    driver.boot(config, bench.session)
    bench.execute('rtc SetDateTime "2026-10-09T12:00:00"')
    bench.write(TAMP_BKP0R, 0x20230315)
    bench.execute("adc4 SetChannelVoltage 13 0.736")
    bench.write(RCC_CSR, 1 << 23)  # RMVF
    bench.run_ms(500)
    driver.reset(config, bench.session)
    assert cause(bench) == "Pin"
    assert bench.read(RCC_CSR) & 0xFE000000 == PINRSTF
    bench.run_ms(500)
    assert not bench.flag("cpu IsHalted")
    assert bench.text("rtc GetDateTime") == "2026-10-09T12:00:01.000"
    assert bench.read(TAMP_BKP0R) == 0x20230315
    assert bench.number("adc4 GetChannelCode 13") == 913


def test_power_off_holds_a_power_on_reset_until_power_on(
    u5: tuple[Bench, Stm32Driver, MachineConfig],
) -> None:
    bench, driver, config = u5
    driver.boot(config, bench.session)
    bench.execute('rtc SetDateTime "2026-10-09T12:00:00"')
    bench.write(TAMP_BKP0R, 0x20230315)
    bench.execute("adc4 SetChannelVoltage 13 0.736")
    bench.write(IWDG_KR, 0xCCCC)  # the watchdog runs: 4096 x 4 / 32 kHz = 0.512 s
    bench.run_ms(100)
    driver.power_off(config, bench.session)
    assert bench.flag("cpu IsHalted")
    assert cause(bench) == "PowerOn"
    assert bench.text("rtc GetDateTime") == "2000-01-01T00:00:00.000"  # no backup battery
    assert bench.read(TAMP_BKP0R) == 0
    bench.run_ms(2000)  # four watchdog periods: the board stays off
    assert bench.flag("cpu IsHalted")
    assert cause(bench) == "PowerOn"
    assert bench.read(RCC_CSR) & IWDGRSTF == 0
    driver.power_on(config, bench.session)
    assert cause(bench) == "PowerOn"
    assert bench.read(RCC_CSR) & 0xFE000000 == BORRSTF | PINRSTF
    bench.run_ms(1)
    assert not bench.flag("cpu IsHalted")
    assert bench.number("adc4 GetChannelCode 13") == 913  # the pin voltage stayed


def test_power_off_before_boot_leaves_the_machine_alone(
    u5: tuple[Bench, Stm32Driver, MachineConfig],
) -> None:
    bench, driver, config = u5
    bench.execute("rcc NextResetCause Software")  # pending: no reset may consume it
    driver.power_off(config, bench.session)
    assert bench.flag("cpu IsHalted")
    assert bench.text("rcc NextResetCause") == "Software"


@pytest.mark.parametrize(
    ("part", "platform"), [("STM32U575ZIT6", "st/stm32u575"), ("STM32U595VJT6", "st/stm32u595")]
)
def test_a_u5_board_through_a_simulation(tmp_path: Path, part: str, platform: str) -> None:
    board = boards.load(board_directory(tmp_path, part) / "board.jig")
    with Simulation(name="u5", workdir=tmp_path / "run") as simulation:
        dut = simulation.add_board("dut", board)
        simulation.start()
        assert f'using "{platform}.repl"' in dut.description()
        assert dut.set_sensor("temperature", 0.736) == 913
        dut.set_time("2026-10-09T12:00:00.5")
        simulation.run_for(1.0)
        assert dut.rtc_time() == "2026-10-09T12:00:01.500"
        dut.reset()
        simulation.run_for(1.0)
        assert dut.execute("rcc LastResetCause").splitlines()[0].strip() == "Pin"
        assert dut.rtc_time() == "2026-10-09T12:00:02.500"  # a system reset keeps the time
        simulation.run(dut.power_cycle(off=0.5))
        assert dut.execute("rcc LastResetCause").splitlines()[0].strip() == "PowerOn"
        simulation.run_for(0.25)
        assert dut.rtc_time() == "2000-01-01T00:00:00.250"  # lost at power-off
        assert dut.sensor_code("temperature") == 913
        kinds = [(e.kind, e.detail) for e in simulation.events if e.kind in ("reset", "power")]
        assert kinds == [
            ("reset", "system reset input"),
            ("power", "off for 0.5 s"),
            ("power", "on"),
        ]


def test_starting_voltages_on_one_converter_share_its_init_block(tmp_path: Path) -> None:
    """Three sensors on ADC4 with starting voltages: the description gives
    the converter one init block, and Renode sets every channel from it."""
    path = board_directory(tmp_path, "STM32U575ZIT6") / "board.jig"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    sensor temperature: mcu.adc4.channel[13]\n",
            "    sensor temperature: mcu.adc4.channel[13] = 736 mV\n"
            "    sensor supply: mcu.adc4.channel[0] = 1.65 V\n"
            "    sensor battery: mcu.adc4.channel[1] = 3.3 V\n",
        ),
        encoding="utf-8",
    )
    with Simulation(name="u5", workdir=tmp_path / "run") as simulation:
        dut = simulation.add_board("dut", boards.load(path))
        simulation.start()
        description = dut.description()
        assert description.count("init add:") == 1
        assert description.count("SetChannelVoltage") == 3
        assert dut.sensor_code("temperature") == 913
        assert dut.sensor_code("supply") == dut.sensor_code("supply", volts=1.65)
        assert dut.sensor_code("battery") == dut.sensor_code("battery", volts=3.3)
