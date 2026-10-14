"""Jig's stm32 SoC driver on Renode's stock STM32F4 platform with public
firmware built from source (tests/fixtures/firmware/stm32f4-hello): the driver
builds and caches the ELF, loads it, points the core at the vector table and
lets it run; the greeting arrives on the USART2 console and the frame on the
CAN bus. Needs Renode and an ARM GCC (ARM_NONE_EABI_GCC or arm-none-eabi-gcc
on PATH)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig.errors import ConfigError, ToolchainError
from jig.system import boards
from jig.system.socs import MachineConfig
from jig.system.socs.stm32 import Stm32Driver
from jig.system.system import Simulation
from jig.transports.can import CanFrame

pytestmark = pytest.mark.renode

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "firmware" / "stm32f4-hello"


@pytest.fixture
def arm_gcc() -> str:
    try:
        return Stm32Driver.compiler()
    except (ConfigError, ToolchainError) as exc:
        pytest.skip(f"no ARM GCC to build the STM32 firmware: {exc}")


def test_the_firmware_greets_and_sends_a_frame(arm_gcc: str, tmp_path: Path) -> None:
    board = boards.load(FIXTURE / "board.yaml")
    frames: list[CanFrame] = []
    with Simulation(name="stm32f4-hello", workdir=tmp_path / "run") as simulation:
        dut = simulation.add_board("dut", board)
        simulation.add_can_bus("can", members=[("dut", "can")])
        try:
            simulation.start()
        except ToolchainError as exc:
            pytest.skip(f"no Renode: {exc}")
        simulation.can_buses["can"].subscribe(frames.append)
        simulation.run(dut.console.expect(r"hello from stm32f4\r\n", timeout=0.1))
        simulation.run(dut.console.expect(r"can frame sent\r\n", timeout=0.1))
        simulation.run_for(0.01)
    assert [(frame.id, frame.data, frame.extended) for frame in frames] == [(0x123, b"JIG", False)]
    assert frames[0].sender.endswith("can1")


def test_the_build_is_cached_by_its_sources(arm_gcc: str) -> None:
    board = boards.load(FIXTURE / "board.yaml")
    driver = Stm32Driver()
    config = MachineConfig(
        name="dut", board=board, boot="flash", performance_mips=16, workdir=Path("unused")
    )
    first = driver.firmware(config)
    assert first.name == "firmware.elf" and first.stat().st_size > 0
    assert driver.firmware(config) == first  # the second call reuses the artifact
