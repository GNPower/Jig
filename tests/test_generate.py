"""The platform description Jig writes (jig.system.generate) for STM32 parts
and the parts every family shares, what jig check finds without Renode, and
jig platform."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig import cli
from jig.errors import ConfigError
from jig.system import boards, checks, generate
from jig.system.model import PinRef
from jig.system.socs.stm32 import Stm32Driver, part_entry


def board_at(
    tmp_path: Path, body: str, name: str = "b", files: dict[str, str] | None = None
) -> boards.Board:
    directory = tmp_path / "boards" / name
    directory.mkdir(parents=True, exist_ok=True)
    for file, text in (files or {}).items():
        (directory / file).write_text(text, encoding="utf-8")
    path = directory / "board.jig"
    path.write_text(f'board "{name}" {{\n{body}\n}}\n', encoding="utf-8")
    return boards.load(path)


def test_an_stm32f407_loads_renodes_platform_with_its_sram() -> None:
    text = generate.description(boards.find("example-stm32"), Stm32Driver())
    lines = text.splitlines()
    assert 'using "platforms/cpus/stm32f4.repl"' in lines
    assert "sram:\n    size: 0x20000" in text  # 128 KiB, not the stock 256 KiB
    assert "sysbus:\n    init:\n" in text and "ApplySVD" not in text
    assert 'Tag <0x50000010, 0x5000003f> "USB:RESET" 0x80000000' in text
    assert "nvic:\n    systickFrequency: 168000000" in text  # clock: 168 MHz


def test_the_part_picks_the_platform_and_the_core() -> None:
    assert part_entry("STM32U595VJT6").platform == "st/stm32u595.repl"
    assert part_entry("stm32u575zit6").platform == "st/stm32u575.repl"
    assert part_entry("STM32F407VGT6").platform == "platforms/cpus/stm32f4.repl"
    assert part_entry("STM32H743ZI") is None
    package = boards.find("example-package")
    text = generate.description(package, Stm32Driver())
    assert 'using "st/stm32u575.repl"' in text
    assert Stm32Driver()._cpu(package) == "cpu"  # from the platform, not the board


def test_a_platform_argument_names_the_core(tmp_path: Path) -> None:
    platform = 'core0: CPU.CortexM @ sysbus\n    cpuType: "cortex-m7"\n'
    board = board_at(
        tmp_path,
        '    device mcu: stm32(part: "STM32H743ZI", platform: "h7.repl", clock: 480 MHz) {\n'
        '        firmware: "app.elf"\n'
        "    }",
        files={"h7.repl": platform},
    )
    driver = Stm32Driver()
    assert driver._cpu(board) == "core0"
    text = generate.description(board, driver)
    assert f'using "{(board.directory / "h7.repl").as_posix()}"' in text
    assert "clock: 480000000 Hz; Jig has no table entry for this part" in text


def test_stm32_pins_sensors_and_assignments(tmp_path: Path) -> None:
    board = board_at(
        tmp_path,
        '    device mcu: stm32(part: "STM32U575ZIT6") {\n'
        '        firmware: "app.elf"\n'
        "        fdcan1.kernelFrequency = 16 MHz\n"
        "    }\n"
        '    peripheral led: "Miscellaneous.LED"\n'
        '    link light: gpio(from: mcu.pc[7], to: led.gpio[0], pull: "up")\n'
        "    sensor temperature: mcu.adc4.channel[13] = 736 mV\n"
        "    sensor rail: mcu.adc1.channel[2] = 1.2 V",
    )
    text = generate.description(board, Stm32Driver())
    assert "gpioPortC:\n    7 -> led@0" in text
    assert "gpioPortC:\n    init add:\n        OnGPIO 7 true" in text
    assert "adc4:\n    init add:\n        SetChannelVoltage 13 0.736" in text  # IAnalogInputs
    assert (
        "rail_input: Analog.ADCChannelSource @ adc1 2\n    defaultValueMicroVolts: 1200000" in text
    )
    assert "fdcan1:\n    kernelFrequency: 16000000" in text
    with pytest.raises(ConfigError, match=r"is not an STM32 pin"):
        Stm32Driver().pin(board.device, PinRef("mcu", "mio", 1))
    with pytest.raises(ConfigError, match="a GPIO port has pins 0-15"):
        Stm32Driver().pin(board.device, PinRef("mcu", "pa", 16))


def test_an_entry_gets_one_init_block_for_all_its_commands(tmp_path: Path) -> None:
    board = board_at(
        tmp_path,
        '    device mcu: stm32(part: "STM32U575ZIT6") { firmware: "app.elf" }\n'
        '    peripheral led: "Miscellaneous.LED"\n'
        '    peripheral fan: "Miscellaneous.LED"\n'
        '    link light: gpio(from: mcu.pc[7], to: led.gpio[0], pull: "up")\n'
        '    link cooling: gpio(from: mcu.pc[8], to: fan.gpio[0], pull: "up")\n'
        "    sensor temperature: mcu.adc4.channel[13] = 736 mV\n"
        "    sensor rail: mcu.adc1.channel[2] = 1.2 V\n"
        "    sensor supply: mcu.adc4.channel[0] = 3.3 V\n"
        "    sensor battery: mcu.adc4.channel[1] = 3.7 V",
    )
    text = generate.description(board, Stm32Driver())
    assert text.count("init add:") == 2
    assert (
        "adc4:\n    init add:\n        SetChannelVoltage 13 0.736\n"
        "        SetChannelVoltage 0 3.3\n        SetChannelVoltage 1 3.7\n\n"
        "rail_input: Analog.ADCChannelSource @ adc1 2\n"
    ) in text
    assert "gpioPortC:\n    init add:\n        OnGPIO 7 true\n        OnGPIO 8 true\n" in text


def test_the_writer_adds_to_the_init_block_an_entry_has() -> None:
    writer = generate.Writer()
    writer.init("a", ["one"])
    writer.entry("x: Example.Block @ sysbus 0x0", ["size: 0x10"])
    writer.init("b", ["two"])
    writer.init("a", ["three", "four"])
    writer.init("b", ["five"])
    writer.init("c", [])
    assert writer.text() == (
        "a:\n    init add:\n        one\n        three\n        four\n\n"
        "x: Example.Block @ sysbus 0x0\n    size: 0x10\n\n"
        "b:\n    init add:\n        two\n        five\n"
    )


def test_a_renode_converter_gets_a_channel_source_the_machine_drives(tmp_path: Path) -> None:
    board = board_at(
        tmp_path,
        '    device mcu: stm32(part: "STM32L552ZE", platform: "platforms/cpus/stm32l552.repl") {\n'
        '        firmware: "app.elf"\n'
        "    }\n"
        "    sensor rail: mcu.adc1.channel[2] = 1.2 V",
    )
    driver = Stm32Driver()
    text = generate.description(board, driver)
    assert (
        "rail_input: Analog.ADCChannelSource @ adc1 2\n    defaultValueMicroVolts: 1200000" in text
    )
    assert board.sensor("rail").source is None  # until a machine's driver decides
    assert generate.with_channel_sources(board, driver).sensor("rail").source == "adc1.rail_input"
    u5 = board_at(
        tmp_path,
        '    device mcu: stm32(part: "STM32U575ZIT6") { firmware: "app.elf" }\n'
        "    sensor temperature: mcu.adc4.channel[13]",
        "u5",
    )
    assert generate.with_channel_sources(u5, driver) is u5  # ADC4 implements IAnalogInputs


def test_values_as_a_platform_description_writes_them() -> None:
    assert generate.repl_value(32 * 1024 * 1024, "size") == "0x2000000"
    assert generate.repl_value(333_333_333.00000006, "frequency") == "333333333"
    assert generate.repl_value(0.002) == "0.002"
    assert generate.repl_value(True) == "true"
    assert generate.repl_value('a "quoted" path') == '"a \\"quoted\\" path"'
    assert generate.repl_value(".GICv1") == ".GICv1"
    assert generate.repl_value([1, 2]) == "[1, 2]"
    with pytest.raises(ConfigError, match="cannot write"):
        generate.repl_value(object(), "x")


def test_ip_lines_need_a_family_with_programmable_logic(tmp_path: Path) -> None:
    board = board_at(
        tmp_path,
        '    device mcu: stm32(part: "STM32F407VG") {\n'
        '        firmware: "app.elf"\n'
        '        ip block: "Example.Block" (on: 0x60000000 .. 0x60000FFF)\n'
        "    }",
    )
    with pytest.raises(
        ConfigError, match=r"b/board.jig:4:9: device family stm32 has no programmable logic"
    ):
        generate.description(board, Stm32Driver())


def test_repl_fragments_come_after_the_generated_entries(tmp_path: Path) -> None:
    fragment = 'using "extra/base.repl"\n\nuart1:\n    clockFrequency: 1\n'
    board = board_at(
        tmp_path,
        '    repl: ["fix.repl"]\n'
        '    device mcu: stm32(part: "STM32F407VG") { firmware: "app.elf" }',
        files={"fix.repl": fragment},
    )
    text = generate.description(board, Stm32Driver())
    lines = text.splitlines()
    assert lines.index('using "extra/base.repl"') < lines.index("sram:")
    assert text.index("// From fix.repl") > text.index("sram:")
    assert "uart1:\n    clockFrequency: 1" in text


# ------------------------------------------------------------------ jig check


def test_jig_check_finds_stacks_that_do_not_fit(tmp_path: Path) -> None:
    board = board_at(
        tmp_path,
        '    device mcu: stm32(part: "STM32F407VG") {\n'
        '        firmware: "app.elf"\n'
        "        protocol csp(address: 4)\n"
        "        interface usart1: csp\n"
        "        interface can1: csp over kiss\n"
        "        interface spi1: cli\n"
        "        interface usart3: morse over kiss\n"
        "    }\n"
        "    external link debug: uart(members: [mcu.usart1])\n"
        "    external link can: can(members: [mcu.can1])",
    )
    problems = checks.stack_problems(board)
    assert any(
        "csp needs packets and usart1 gives a byte stream: put kiss between them" in p
        for p in problems
    ), problems
    assert any("kiss needs a byte stream and can1 gives CAN frames" in p for p in problems)
    assert any(
        "interface spi1: a protocol stack runs on a UART or CAN interface" in p for p in problems
    )
    assert any("no protocol layer 'morse'" in p for p in problems)


def test_jig_check_reports_each_boards_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "jig.toml").write_text('[paths]\nboards = ["boards"]\n', encoding="utf-8")
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    board_at(tmp_path, '    device mcu: stm32(part: "STM32F407VG") { firmware: "app.elf" }', "good")
    (tmp_path / "boards" / "good" / "app.elf").write_bytes(b"\x7fELF")
    board_at(
        tmp_path,
        '    device z: zynq7000() { firmware: {app: "app.elf"} }',
        "noclocks",
    )
    (tmp_path / "boards" / "broken").mkdir()
    (tmp_path / "boards" / "broken" / "board.jig").write_text(
        'board "broken" { peripheral f: "SPI.X" device d: stm32(part: "STM32F407VG") { } }\n',
        encoding="utf-8",
    )
    assert cli.main(["-q", "check"]) == 1
    out = capsys.readouterr().out
    assert "broken: 1 problem(s)" in out and "peripheral f is on no link" in out
    assert "good: ok" in out
    assert "noclocks: 2 problem(s)" in out and "needs the cpu_3x2x and cpu_1x clock" in out


def test_jig_check_gives_an_option_to_the_boards_that_have_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "jig.toml").write_text('[paths]\nboards = ["boards"]\n', encoding="utf-8")
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    board_at(
        tmp_path,
        "    option node: choice(1, 2)\n"
        '    device mcu: stm32(part: "STM32F407VG") { firmware: "app-{node}.elf" }',
        "pair",
    )
    (tmp_path / "boards" / "pair" / "app-2.elf").write_bytes(b"\x7fELF")
    plain = '    device mcu: stm32(part: "STM32F407VG") { firmware: "app.elf" }'
    board_at(tmp_path, plain, "plain")
    (tmp_path / "boards" / "plain" / "app.elf").write_bytes(b"\x7fELF")
    assert cli.main(["-q", "check", "--option", "node=2"]) == 0
    out = capsys.readouterr().out
    assert "pair: ok" in out and "plain: ok" in out
    assert cli.main(["-q", "check", "pair"]) == 1  # node 1's app-1.elf is not there
    assert "app-1.elf not found" in capsys.readouterr().out


# --------------------------------------------------------------- jig platform


def test_jig_platform_prints_or_writes_the_description(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["-q", "platform", "example-node"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("// Board example-node, device zynq (zynq7000)")
    assert "globalTimer:\n    frequency: 333333333" in printed
    target = tmp_path / "out" / "node.repl"
    assert cli.main(["-q", "platform", "example-node", "-o", str(target)]) == 0
    assert target.read_bytes() == printed.encode("utf-8").replace(b"\r\n", b"\n")
    assert cli.main(["-q", "platform", "example-zynq", "--option", "pl=fpga"]) == 2
