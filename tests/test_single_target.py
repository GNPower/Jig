"""SPI controllers that take one peripheral: Renode's STM32 SPI models are
NullRegistrationPointPeripheralContainers, so the platform description Jig
writes registers the peripheral at the controller with no address (``@
spi1``), and a second target or a chip select other than 0 is an error."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig.errors import ConfigError
from jig.system import boards, checks, generate
from jig.system.socs import driver_for
from jig.system.socs.stm32 import Stm32Driver

from .storage_doubles import make_elf
from .test_partitions import STM32, write_board


def description(path: Path) -> str:
    board = boards.load(path)
    return generate.description(board, driver_for(board))


def stm32_board(tmp_path: Path, link: str, part: str = "STM32F407VG") -> Path:
    braced = link.replace("{", "{{").replace("}", "}}")  # STM32 is a format template
    template = STM32.replace(
        "    link spi: spi(controller: mcu.spi1, targets: {{0: flash0}})\n", f"    {braced}\n"
    ).replace("STM32F407VG", part)
    make_elf(tmp_path / "store" / "build" / "app.elf", [(0x08000000, bytes(16))])
    return write_board(tmp_path, template=template)


@pytest.mark.parametrize("part", ["STM32F407VG", "STM32U575ZI", "STM32U595ZJ"])
def test_an_stm32_spi_peripheral_has_no_address(tmp_path: Path, part: str) -> None:
    path = stm32_board(tmp_path, "link spi: spi(controller: mcu.spi1, targets: {0: flash0})", part)
    text = description(path)
    assert "flash0: JigFixtures.SpiNorFlash @ spi1\n" in text
    board = boards.load(path)
    driver = driver_for(board)
    assert isinstance(driver, Stm32Driver)
    assert driver.single_target(board, board.device, "spi1")
    assert not driver.single_target(board, board.device, "usart2")


@pytest.mark.parametrize(
    "link",
    [
        "link spi: spi(controller: mcu.spi1, targets: {1: flash0})",
        "link spi: spi(controller: mcu.spi1, targets: {0: flash0, 1: flash1})",
    ],
)
def test_an_stm32_spi_link_has_one_target_at_chip_select_0(tmp_path: Path, link: str) -> None:
    path = stm32_board(tmp_path, link)
    if "flash1" in link:
        text = path.read_text(encoding="utf-8").replace(
            "    peripheral flash0:",
            '    peripheral flash1: "JigFixtures.SpiNorFlash" (size: 1 MiB)\n'
            "    peripheral flash0:",
        )
        path.write_text(text, encoding="utf-8", newline="\n")
    with pytest.raises(ConfigError, match=r"mcu.spi1 takes one peripheral, which Renode registers"):
        description(path)
    (problem,) = checks.problems(boards.load(path))
    assert problem.startswith("platform description: ") and "chip select 0" in problem


def test_a_zynq_spi_peripheral_keeps_its_chip_select(tmp_path: Path) -> None:
    from .conftest import FIXTURES

    board = boards.load(FIXTURES / "boards" / "example-zynq" / "board.jig")
    driver = driver_for(board)
    assert not generate.single_target(board, board.device, driver, "spi0")


def test_a_platform_file_of_the_board_names_its_spi_models(tmp_path: Path) -> None:
    (tmp_path / "store").mkdir(parents=True)
    (tmp_path / "store" / "part.repl").write_text(
        "spi1: SPI.STM32SPI @ sysbus 0x40013000\n"
        "spi2: SPI.Other @ sysbus 0x40003800\n"
        "flash: Memory.MappedMemory @ sysbus 0x08000000\n"
        "    size: 0x100000\n",
        encoding="utf-8",
        newline="\n",
    )
    path = stm32_board(tmp_path, "link spi: spi(controller: mcu.spi1, targets: {0: flash0})")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'stm32(part: "STM32F407VG")', 'stm32(part: "STM32L4R5", platform: "part.repl")'
        ),
        encoding="utf-8",
        newline="\n",
    )
    board = boards.load(path)
    driver = driver_for(board)
    assert isinstance(driver, Stm32Driver)
    assert driver.single_target(board, board.device, "spi1")
    assert not driver.single_target(board, board.device, "spi2")
    (flash,) = driver.memories(board).values()
    assert (flash.name, flash.size, flash.address) == ("flash", 0x100000, 0x08000000)
