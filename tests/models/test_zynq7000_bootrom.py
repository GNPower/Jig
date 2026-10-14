"""Zynq-7000 BootROM model (Jig.Xilinx.Zynq7000_BootROM): Quad-SPI boot of
images built from UG585 v1.7's Boot Header (section 6.3.2, tests/models/
zynq_boot.py) on the fixture flash, the header search and MultiBoot (6.3.9,
6.3.10), register initialization, the lockdown codes of Table 6-18, OCM
clearing, JTAG mode and the power states. The code ends in OCM with the core
started at its entry and running it."""

from __future__ import annotations

from pathlib import Path
from string import Template

import pytest

from . import arm
from .bench import Bench
from .zynq_boot import HEADER_SEARCH_STEP, boot_image, flash_image

pytestmark = pytest.mark.renode

DIE = 0x100000
SLCR_UNLOCK = 0xF8000008
ARM_CLK_CTRL = 0xF8000120
PSS_RST_CTRL = 0xF8000200
REBOOT_STATUS = 0xF8000258
LOCKSTA = 0xF800000C
MULTIBOOT_ADDR = 0xF800702C
SWDT_MODE = 0xF8005000
MARKER_ADDRESS = 0x20000
POR = 1 << 22
SLC_RST = 1 << 19

# A core started after a halt or a reset runs from the next quantum boundary
# (Renode's global quantum, 100 us by default): run past one.
RUN_US = 200

MACHINE = Template(
    """\
cpu0: CPU.ARMv7A @ sysbus
    cpuType: "cortex-a9"

slcr: Jig.Xilinx.Zynq7000_SLCR @ sysbus <0xF8000000, +0x1000>
    bootMode: $boot_mode

devc: Jig.Xilinx.Zynq7000_DevC @ sysbus <0xF8007000, +0x100>
    slcr: slcr

swdt: Jig.Cadence.CadenceWDT @ sysbus <0xF8005000, +0x1000>
    frequency: 111111111
    resetController: slcr

qspi: Jig.Xilinx.Zynq7000_QSPI @ {
    sysbus new Bus.BusMultiRegistration { address: 0xE000D000; size: 0x1000; region: "registers" };
    sysbus new Bus.BusMultiRegistration { address: 0xFC000000; size: 0x2000000; region: "linear" }
    }
    slcr: slcr

flash0: JigFixtures.SpiNorFlash @ qspi 0
    size: 0x100000

ocmLow: Memory.MappedMemory @ sysbus 0x0
    size: 0x30000

ocmHigh: Memory.MappedMemory @ sysbus 0xFFFF0000
    size: 0x10000

bootrom: Jig.Xilinx.Zynq7000_BootROM @ sysbus
    cpu: cpu0
    slcr: slcr
    devc: devc
    qspi: qspi
    systemWatchdog: swdt
    ocmLow: ocmLow
    ocmHigh: ocmHigh
    clearOcmOnReset: $clear_ocm
"""
)


def program(marker: int, entry: int = 0) -> bytes:
    """Stores ``marker`` at MARKER_ADDRESS and loops; placed at ``entry``."""
    code = arm.words(
        arm.ldr_literal(0, 8),  # r0 = MARKER_ADDRESS (the word at +0x10)
        arm.ldr_literal(1, 8),  # r1 = marker (the word at +0x14)
        arm.str_(1, 0),
        arm.branch_to_self(),
        MARKER_ADDRESS,
        marker,
    )
    padded = b"\x00" * entry + code
    return padded + b"\x00" * (-len(padded) % 64)


def make(
    bench: Bench,
    tmp_path: Path,
    images: dict[int, bytes],
    *,
    boot_mode: int = 1,
    clear_ocm: bool = True,
) -> Bench:
    bench.load(MACHINE.substitute(boot_mode=boot_mode, clear_ocm=str(clear_ocm).lower()))
    flash = tmp_path / "flash.img"
    flash.write_bytes(flash_image(images, DIE))
    bench.execute(f'qspi.flash0 AttachImage "{flash.as_posix()}"')
    return bench


def state(bench: Bench) -> str:
    return bench.text("bootrom State")


def error_code(bench: Bench) -> int:
    return bench.number("bootrom ErrorCode")


def test_jtag_straps_wait_for_a_debugger(bench: Bench, tmp_path: Path) -> None:
    make(bench, tmp_path, {}, boot_mode=0)
    bench.execute("bootrom PowerOn")
    assert state(bench) == "WaitingForDebugger"
    assert bench.flag("cpu0 IsHalted")


def test_quad_spi_boot_copies_the_image_and_starts_it(bench: Bench, tmp_path: Path) -> None:
    code = program(0xC0DE0001)
    make(bench, tmp_path, {0: boot_image(code)})
    bench.execute("bootrom PowerOn")
    assert state(bench) == "Booted"
    assert bench.number("bootrom BootedHeaderOffset") == 0
    assert bench.number("cpu0 PC") == 0
    assert not bench.flag("cpu0 IsHalted")
    assert bench.read_bytes(0, len(code)) == code
    bench.run_us(RUN_US)
    assert bench.read(MARKER_ADDRESS) == 0xC0DE0001
    assert bench.number("cpu0 PC") == 0x0C  # in the loop
    assert bench.read(MULTIBOOT_ADDR) == 0
    assert bench.read(REBOOT_STATUS) == POR  # no error code
    assert "valid header" in bench.execute("bootrom BootLog")


def test_the_core_starts_at_the_start_of_execution(bench: Bench, tmp_path: Path) -> None:
    make(bench, tmp_path, {0: boot_image(program(0xC0DE0002, entry=0x40), start=0x40)})
    bench.execute("bootrom PowerOn")
    assert bench.number("cpu0 PC") == 0x40
    bench.run_us(RUN_US)
    assert bench.read(MARKER_ADDRESS) == 0xC0DE0002


def test_the_header_search_steps_in_32_kb(bench: Bench, tmp_path: Path) -> None:
    good = boot_image(program(0xC0DE0003))
    bad_checksum = boot_image(program(0xBAD), header_checksum=0)
    make(bench, tmp_path, {HEADER_SEARCH_STEP: bad_checksum, 2 * HEADER_SEARCH_STEP: good})
    bench.execute("bootrom PowerOn")
    assert state(bench) == "Booted"
    assert bench.number("bootrom BootedHeaderOffset") == 2 * HEADER_SEARCH_STEP
    assert bench.read(MULTIBOOT_ADDR) == 2  # the index of the header that booted
    bench.run_us(RUN_US)
    assert bench.read(MARKER_ADDRESS) == 0xC0DE0003


def test_multiboot_selects_the_image_after_a_system_reset(bench: Bench, tmp_path: Path) -> None:
    first = boot_image(program(0xC0DE0004))
    second = boot_image(program(0xC0DE0005))
    make(bench, tmp_path, {0: first, 3 * HEADER_SEARCH_STEP: second})
    bench.execute("bootrom PowerOn")
    bench.run_us(RUN_US)
    assert bench.read(MARKER_ADDRESS) == 0xC0DE0004
    # UG585 6.3.10: MULTIBOOT_ADDR, then a software system reset.
    bench.write(MULTIBOOT_ADDR, 3)
    bench.write(SLCR_UNLOCK, 0xDF0D)
    bench.write(PSS_RST_CTRL, 1)
    bench.run_us(RUN_US)
    assert bench.number("bootrom BootedHeaderOffset") == 3 * HEADER_SEARCH_STEP
    assert bench.read(MARKER_ADDRESS) == 0xC0DE0005
    assert bench.read(REBOOT_STATUS) == POR | SLC_RST
    # A power-on reset clears MULTIBOOT_ADDR: the first image boots again.
    bench.execute("slcr OnGPIO 0 true")
    bench.run_us(RUN_US)
    assert bench.read(MULTIBOOT_ADDR) == 0
    assert bench.read(MARKER_ADDRESS) == 0xC0DE0004


def test_register_initialization_runs_before_the_copy(bench: Bench, tmp_path: Path) -> None:
    image = boot_image(program(0xC0DE0006), register_init=[(ARM_CLK_CTRL, 0x1F000200)])
    make(bench, tmp_path, {0: image})
    bench.execute("bootrom PowerOn")
    assert state(bench) == "Booted"
    assert bench.read(ARM_CLK_CTRL) == 0x1F000200
    assert bench.read(LOCKSTA) == 1  # the SLCR is locked again afterwards


LOCKDOWNS = {
    "no valid header": ({}, 0x200C),
    "register init outside the allowed ranges": (
        {0: boot_image(program(1), register_init=[(PSS_RST_CTRL, 1)])},
        0x2111,
    ),
    "source offset inside the header": (
        {0: boot_image(program(1), source_offset=0x800)},
        0x2102,
    ),
    "source offset not 64-byte aligned": (
        {0: boot_image(program(1), source_offset=0x8E0)},
        0x2103,
    ),
    "image longer than 192 KB": ({0: boot_image(program(1), length=0x30040)}, 0x2106),
    "start of execution above OCM": ({0: boot_image(program(1), start=0x30000)}, 0x2108),
    "reserved word 0x038 not zero": ({0: boot_image(program(1), reserved_038=1)}, 0x2109),
    "encrypted image (not modelled)": ({0: boot_image(program(1), encryption=0xA5C3C5A3)}, 0),
    "execute in place (not modelled)": ({0: boot_image(program(1), length=0)}, 0),
}


@pytest.mark.parametrize("case", sorted(LOCKDOWNS))
def test_lockdown_codes(bench: Bench, tmp_path: Path, case: str) -> None:
    images, code = LOCKDOWNS[case]
    make(bench, tmp_path, images)
    bench.execute("bootrom PowerOn")
    assert state(bench) == "LockedDown"
    assert error_code(bench) == code
    assert bench.read(REBOOT_STATUS) == POR | code  # REBOOT_STATUS[15:0]
    assert bench.flag("cpu0 IsHalted")


def test_a_system_reset_clears_ocm_and_boots_again(bench: Bench, tmp_path: Path) -> None:
    # UG585 v1.7 26.2.2: a system reset erases all PS memory, OCM included
    # (documentation; not checked on hardware).
    make(bench, tmp_path, {0: boot_image(program(0xC0DE0007))})
    bench.execute("bootrom PowerOn")
    bench.execute("cpu0 IsHalted true")  # keep the program from storing its marker again
    bench.write(0x10000, 0x12345678)
    bench.write(SLCR_UNLOCK, 0xDF0D)
    bench.write(PSS_RST_CTRL, 1)
    bench.run_us(1)
    assert state(bench) == "Booted"
    assert bench.read(0x10000) == 0
    assert bench.read(MARKER_ADDRESS) in (0, 0xC0DE0007)  # the restarted program may have run


def test_ocm_clearing_is_the_boards_choice(bench: Bench, tmp_path: Path) -> None:
    make(bench, tmp_path, {0: boot_image(program(0xC0DE0008))}, clear_ocm=False)
    bench.execute("bootrom PowerOn")
    bench.write(0x10000, 0x12345678)
    bench.write(SLCR_UNLOCK, 0xDF0D)
    bench.write(PSS_RST_CTRL, 1)
    bench.run_us(1)
    assert bench.read(0x10000) == 0x12345678


def test_power_off_holds_the_core_until_power_returns(bench: Bench, tmp_path: Path) -> None:
    make(bench, tmp_path, {0: boot_image(program(0xC0DE0009))})
    bench.execute("bootrom PowerOn")
    bench.run_us(RUN_US)
    bench.execute("bootrom PowerOff")
    assert state(bench) == "Unpowered"
    assert bench.flag("cpu0 IsHalted")
    bench.execute("slcr OnGPIO 0 true")  # the harness takes the board down
    bench.run_us(RUN_US)
    assert bench.flag("cpu0 IsHalted")
    assert not bench.flag("bootrom Powered")
    bench.execute("bootrom PowerOn")
    assert state(bench) == "Booted"
    assert bench.read(0x10000) == 0  # OCM held no charge
    assert bench.read(REBOOT_STATUS) == POR
    bench.run_us(RUN_US)
    assert bench.read(MARKER_ADDRESS) == 0xC0DE0009


def test_a_debugger_can_hold_the_core(bench: Bench, tmp_path: Path) -> None:
    make(bench, tmp_path, {0: boot_image(program(1))})
    bench.execute("bootrom HoldCoreForDebugger true")
    bench.execute("bootrom PowerOn")
    assert state(bench) == "HeldForDebugger"
    assert bench.flag("cpu0 IsHalted")


def test_the_system_watchdog_is_left_disabled(bench: Bench, tmp_path: Path) -> None:
    make(bench, tmp_path, {0: boot_image(program(1))})
    bench.execute("bootrom PowerOn")
    assert bench.read(SWDT_MODE) == 0x1C0  # UG585 B.31 reset value: WDEN clear
