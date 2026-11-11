"""The storage end-to-end fixture (tests/fixtures/firmware/stm32f4-storage) on
Renode's stock STM32F4 platform.

The fixture flash (tests/fixtures/models/SpiNorFlash.cs) hangs off Renode's
STM32SPI, which takes its peripheral without a chip-select number
(``@ spi1``) and never tells it that a command ended, so the flash takes chip
select from a GPIO pin. The first test drives that wiring through the
registers; the second builds the firmware and checks where it ends.

The third runs the fixture's plan, storage.jig: Jig composes four partitions
from tests/fixtures/storage, the firmware lists them on its console and writes
a file, and the plan reads the partitions back. It needs partitions in board
files and the littlefs and fat formats, and skips until jig.storage has
``compose`` and both formats. Needs Renode and an ARM GCC (ARM_NONE_EABI_GCC
or arm-none-eabi-gcc on PATH).
"""

from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from jig import cli
from jig.errors import ConfigError, ToolchainError
from jig.renode.discover import find_renode, load_project_config
from jig.storage import formats
from jig.system.socs.stm32 import Stm32Driver

from .bench import Bench

pytestmark = pytest.mark.renode

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "firmware" / "stm32f4-storage"
PARTITION_START = 0x08020000  # mcu_lfs, in the MCU's flash (board.jig, firmware/layout.h)
FLASH_START = 0x08000000

GPIOA_BSRR = 0x40020018
SPI1_CR1 = 0x40013000
SPI1_DR = 0x4001300C
SPI_CR1_ENABLED_MASTER = (1 << 2) | (1 << 6) | (1 << 8) | (1 << 9)  # MSTR, SPE, SSI, SSM
CHIP_SELECT = 4  # PA4

MACHINE = """\
spi1: SPI.STM32SPI @ sysbus 0x40013000
    series: STM32Series.F4

flash: JigFixtures.SpiNorFlash @ spi1
    size: 0x10000

gpioPortA: GPIOPort.STM32_GPIOPort @ sysbus <0x40020000, +0x400>
    numberOfAFs: 16
    4 -> flash@0
"""

WEL = 0x02


@pytest.fixture
def arm_gcc() -> str:
    try:
        return Stm32Driver.compiler()
    except (ConfigError, ToolchainError) as exc:
        pytest.skip(f"no ARM GCC to build the firmware: {exc}")


def missing_storage() -> str | None:
    """Why the plan can't run yet, or None once Jig composes partitions and
    builds the littlefs and fat formats."""
    if importlib.util.find_spec("jig.storage.compose") is None:
        return "jig.storage composes no partitions yet (no jig.storage.compose)"
    missing = sorted({"littlefs", "fat"} - set(formats.builtin()))
    if missing:
        return f"jig.storage.formats has no {' or '.join(missing)} format yet"
    return None


STORAGE_MISSING = missing_storage()


def chip_select(bench: Bench, high: bool) -> None:
    bench.write(GPIOA_BSRR, 1 << CHIP_SELECT if high else 1 << (CHIP_SELECT + 16))


def transfer(bench: Bench, byte: int) -> int:
    bench.write(SPI1_DR, byte)
    return bench.read(SPI1_DR) & 0xFF


def command(bench: Bench, *data: int) -> list[int]:
    """One command between chip select falling and rising; the replies."""
    chip_select(bench, high=False)
    replies = [transfer(bench, byte) for byte in data]
    chip_select(bench, high=True)
    return replies


def test_the_flash_takes_chip_select_from_a_gpio_pin(bench: Bench) -> None:
    bench.load(MACHINE)
    bench.write(SPI1_CR1, SPI_CR1_ENABLED_MASTER)
    chip_select(bench, high=True)
    assert command(bench, 0x9F, 0, 0, 0) == [0xFF, 0xA5, 0x5A, 16]  # READ ID: 64 KiB
    # WRITE ENABLE, then READ STATUS: the rising edge between them ended the
    # first command, or the second would read as part of it.
    command(bench, 0x06)
    assert command(bench, 0x05, 0) == [0xFF, WEL]
    # WRITE DISABLE with chip select high reaches no command.
    assert transfer(bench, 0x04) == 0xFF
    assert command(bench, 0x05, 0) == [0xFF, WEL]
    # A page program starts when chip select rises.
    command(bench, 0x02, 0x00, 0x01, 0x00, 0x12, 0x34)
    bench.run_us(600)  # the model's page program takes 500 us
    assert command(bench, 0x05, 0) == [0xFF, 0]
    assert command(bench, 0x03, 0x00, 0x01, 0x00, 0, 0)[-2:] == [0x12, 0x34]


def test_the_firmware_builds_and_ends_below_the_partition(arm_gcc: str, tmp_path: Path) -> None:
    script = FIXTURE / "firmware" / "build.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), "--cc", arm_gcc],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    elf = (tmp_path / "firmware.elf").read_bytes()
    # ELF32 program headers: each PT_LOAD segment's physical address and file size.
    (phoff,) = struct.unpack_from("<I", elf, 0x1C)
    phentsize, phnum = struct.unpack_from("<HH", elf, 0x2A)
    ends = []
    for index in range(phnum):
        kind, _offset, _vaddr, paddr, filesz = struct.unpack_from(
            "<IIIII", elf, phoff + index * phentsize
        )
        if kind == 1 and filesz > 0:
            ends.append(paddr + filesz)
    assert ends and all(FLASH_START < end <= PARTITION_START for end in ends)


@pytest.mark.skipif(STORAGE_MISSING is not None, reason=STORAGE_MISSING or "")
def test_the_storage_plan_passes(
    arm_gcc: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        install = find_renode(explicit=None, config=load_project_config())
    except ToolchainError as exc:
        pytest.skip(f"no Renode: {exc}")
    monkeypatch.setenv("JIG_RENODE", str(install.executable))
    # The board's directory is named after it, so its parent is a board root.
    monkeypatch.setenv("JIG_BOARD_PATH", str(FIXTURE.parent))
    summary = tmp_path / "summary.json"
    code = cli.main(["-q", "run", str(FIXTURE / "storage.jig"), "--json", str(summary)])
    (run,) = json.loads(summary.read_text(encoding="utf-8"))["plans"]
    failed = [case for case in run["cases"] if case["status"] != "PASS"]
    assert code == 0 and run["status"] == "PASS", json.dumps(failed, indent=2)
    assert run["counts"]["PASS"] == 7
