"""Jig's stm32 family on a part it has no table entry for: Renode's
STM32L552 platform through the device's platform argument. The sensor on the
part's ADC1, Renode's STM32L5_ADC, gets an Analog.ADCChannelSource on its
channel, starting at the board file's value, and set_sensor moves it. The
board starts without power, so the firmware file is only checked, never
loaded."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig.errors import ConfigError
from jig.system import boards
from jig.system.system import Simulation

pytestmark = pytest.mark.renode


def test_a_renode_converter_takes_its_input_through_a_channel_source(tmp_path: Path) -> None:
    directory = tmp_path / "l5-board"
    directory.mkdir()
    (directory / "app.elf").write_bytes(b"\x7fELF")
    (directory / "board.jig").write_text(
        'board "l5-board" {\n'
        '    root: "."\n'
        '    power: "off"\n'
        '    device mcu: stm32(part: "STM32L552ZE", platform: "platforms/cpus/stm32l552.repl") {\n'
        '        firmware: "app.elf"\n'
        "    }\n"
        "    sensor rail: mcu.adc1.channel[2] = 1.2 V\n"
        "}\n",
        encoding="utf-8",
    )
    board = boards.load(directory / "board.jig")
    with Simulation(name="l5", workdir=tmp_path / "run") as simulation:
        dut = simulation.add_board("dut", board)
        simulation.start()
        assert "rail_input: Analog.ADCChannelSource @ adc1 2" in dut.description()
        assert dut.board.sensor("rail").source == "adc1.rail_input"
        assert dut.execute("adc1.rail_input Volts").strip().startswith("1.2")
        assert dut.set_sensor("rail", 0.75) is None
        assert dut.execute("adc1.rail_input Volts").strip().startswith("0.75")
        with pytest.raises(ConfigError, match="reports no codes to the harness"):
            dut.sensor_code("rail")
