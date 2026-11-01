"""Board files: the search path, reading board.jig into a board, options,
forward references, model sets and the CSP layer's settings, on the fixture
project."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from jig.errors import ConfigError
from jig.ext.csp.settings import CspSettings
from jig.system import boards
from jig.system.model import InternalRef, PeripheralRef

from .conftest import FIXTURES

BOARDS = FIXTURES / "boards"
EXTRA = FIXTURES / "extra" / "boards"
SHADOW = FIXTURES / "shadow" / "boards"


def board_file(directory: Path, text: str, *files: str) -> Path:
    """A board.jig in ``directory`` (plus empty ``files`` beside it)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in files:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    path = directory / "board.jig"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


DEVICE = 'device mcu: stm32(part: "STM32F407VG") { firmware: "build/x.elf" }'


def minimal(*lines: str, name: str = "x", device: str = DEVICE) -> str:
    body = "\n".join(f"    {line}" for line in (*lines, device) if line)
    return f'board "{name}" {{\n{body}\n}}\n'


def load(tmp_path: Path, *lines: str, device: str = DEVICE, **options: Any) -> boards.Board:
    path = board_file(tmp_path / "boards" / "x", minimal(*lines, device=device))
    return boards.load(path, options)


# ---------------------------------------------------------------- search path


def test_the_project_names_the_board_roots() -> None:
    (root,) = boards.search_path()
    assert root == boards.BoardRoot(BOARDS, "jig.toml [paths] boards")
    assert boards.available() == [
        "example-node",
        "example-package",
        "example-stm32",
        "example-zynq",
    ]
    found = boards.catalog()["example-node"]
    assert found.path == BOARDS / "example-node" / "board.jig"
    assert found.origin == "jig.toml [paths] boards"


def test_jig_board_path_adds_roots_after_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIG_BOARD_PATH", os.pathsep.join(["", str(EXTRA), " "]))
    roots = boards.search_path()
    assert [(r.path, r.origin) for r in roots] == [
        (BOARDS, "jig.toml [paths] boards"),
        (EXTRA, "JIG_BOARD_PATH"),
    ]
    found = boards.catalog()["example-extra"]
    assert found.origin == "JIG_BOARD_PATH"
    assert boards.find("example-extra").root == FIXTURES


def test_a_board_defined_twice_names_both_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIG_BOARD_PATH", str(SHADOW))
    with pytest.raises(ConfigError) as error:
        boards.catalog()
    message = str(error.value)
    assert "board 'example-stm32' is defined twice" in message
    assert str(BOARDS / "example-stm32" / "board.jig") in message
    assert str(SHADOW / "example-stm32" / "board.jig") in message
    assert "(jig.toml [paths] boards)" in message and "(JIG_BOARD_PATH)" in message


def test_one_root_named_twice_is_not_a_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIG_BOARD_PATH", str(BOARDS))
    assert len(boards.search_path()) == 2
    assert "example-zynq" in boards.catalog()


def test_a_missing_root_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIG_BOARD_PATH", str(tmp_path / "absent"))
    with pytest.raises(ConfigError, match=r"JIG_BOARD_PATH: .*absent is not a directory"):
        boards.search_path()


class _EntryPoint:
    def __init__(self, name: str, value: str, loaded: Any) -> None:
        self.name = name
        self.value = value
        self._loaded = loaded

    def load(self) -> Any:
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


def test_the_jig_boards_entry_points_come_last(monkeypatch: pytest.MonkeyPatch) -> None:
    points = [
        _EntryPoint("extra", "pkg:root", lambda: EXTRA),
        _EntryPoint("more", "pkg:ROOTS", [str(EXTRA)]),
    ]
    monkeypatch.setattr(boards.metadata, "entry_points", lambda group: points)
    roots = boards.search_path()
    assert [r.origin for r in roots] == [
        "jig.toml [paths] boards",
        "entry point extra (pkg:root)",
        "entry point more (pkg:ROOTS)",
    ]
    assert boards.catalog()["example-extra"].origin == "entry point extra (pkg:root)"
    broken = [_EntryPoint("broken", "pkg:gone", ImportError("no module named pkg"))]
    monkeypatch.setattr(boards.metadata, "entry_points", lambda group: broken)
    with pytest.raises(ConfigError, match=r"entry point broken \(pkg:gone\): ImportError"):
        boards.search_path()
    wrong = [_EntryPoint("wrong", "pkg:NUMBER", 42)]
    monkeypatch.setattr(boards.metadata, "entry_points", lambda group: wrong)
    with pytest.raises(ConfigError, match="expected a directory or a list of them"):
        boards.search_path()


def test_without_paths_boards_the_project_boards_directory_is_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "jig.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    assert boards.search_path() == []
    board_file(tmp_path / "boards" / "x", minimal())
    (root,) = boards.search_path()
    assert root.origin == "<project>/boards" and boards.available() == ["x"]


def test_a_board_is_named_after_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "jig.toml").write_text('[paths]\nboards = ["boards"]\n', encoding="utf-8")
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    board_file(tmp_path / "boards" / "alpha", minimal(name="beta"))
    assert boards.available() == ["alpha"]
    with pytest.raises(ConfigError, match="board 'beta' differs from its directory 'alpha'"):
        boards.find("alpha")
    with pytest.raises(ConfigError, match=r"no board named 'gamma' \(available: alpha\)"):
        boards.find("gamma")


# ------------------------------------------------------------- reading boards


def test_a_plugin_board_resolves_its_paths() -> None:
    board = boards.find("example-node")
    directory = BOARDS / "example-node"
    assert board.source == directory / "board.jig"
    assert board.directory == directory
    assert board.root == FIXTURES  # root: "../.."
    assert board.soc == "zynq7000" and board.soc_driver is None
    assert board.firmware["app"] == FIXTURES / "build" / "node.elf"  # root-relative
    assert board.plugin == directory / "plugin.py"  # relative to the board file
    assert board.models == (directory / "models" / "ExampleBlock.cs",)
    assert board.power_initial == "off"
    assert board.default_boot == "jtag" and board.boot_modes == ("jtag",)
    assert board.interface("console").eol == "\r\n"  # the cli layer's eol
    assert board.interface("kiss").eol == "\r"
    assert board.interface("console").stack == ("cli",)
    assert board.interface("kiss").stack == ("csp", "kiss")
    assert board.soc_settings["straps"] == "jtag"
    assert board.performance_mips == 100
    settings = board.section("csp")
    assert isinstance(settings, CspSettings)
    assert settings.address == 1
    assert settings.housekeeping is not None
    assert settings.housekeeping.port == 10
    assert settings.housekeeping.request == b"\x01"
    assert settings.housekeeping.codec == "example-status"
    assert settings.housekeeping.options == {"label": "status"}
    assert settings.rdp == {"ack_timeout_ms": 100}  # rdp: {ack_timeout: 100 ms}
    package = boards.find("example-package")
    assert package.plugin == BOARDS / "example-package" / "plugin"
    assert package.section("csp").housekeeping.request == b""
    assert boards.find("example-zynq").section("csp").housekeeping is None
    assert boards.find("example-zynq").section("absent") is None


def test_the_zynq_fixture_keeps_board_yaml_fields() -> None:
    board = boards.find("example-zynq")
    assert board.performance_mips == 120
    assert board.default_boot == "jtag" and board.boot_modes == ("jtag", "qspi")
    assert board.boot.get("qspi") == {"storage": "flash0"}
    assert board.interface("console").peripheral == "uart1"
    assert board.interface("console").settings == {"baud": 115200, "frame": "8N1", "eol": "\r"}
    assert board.interface("kiss").peripheral == "uart0"
    assert board.interface("can").kind == "can"
    assert [(s.name, s.peripheral, s.size) for s in board.storage] == [
        ("flash0", "qspi.flash0", 0x1000000)
    ]
    assert board.section("csp").address == 5
    assert (board.sensor("supply").adc, board.sensor("supply").channel) == ("spi0.adc0", 2)
    assert board.rtc == "i2c0.rtc0"
    assert board.power_restart == ("qspi.flash0", "spi0.adc0")  # rtc0 is on the backup supply
    assert board.power_initial == "on"
    assert board.firmware["app"] == (FIXTURES / "build" / "app.elf").resolve()
    (source,) = board.register_maps  # the model choice's airhdl(...) property
    assert source.target == "block.registerMap"
    assert source.package == FIXTURES / "hdl" / "example_regs_pkg.sv"
    with pytest.raises(ConfigError, match="has no external link 'radio'"):
        board.interface("radio")
    with pytest.raises(ConfigError, match=r"has no sensor 'absent' \(has: supply\)"):
        board.sensor("absent")


def test_the_root_defaults_to_the_nearest_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    path = board_file(repo / "boards" / "x", minimal())
    board = boards.load(path)
    assert board.root == repo.resolve()
    assert board.firmware["app"] == repo.resolve() / "build" / "x.elf"
    # A submodule or worktree checkout has a .git file.
    sub = repo / "external" / "sub"
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/sub\n", encoding="utf-8")
    nested = board_file(sub / "boards" / "x", minimal())
    assert boards.load(nested).root == sub.resolve()
    # Without one, the board's directory.
    loose = board_file(tmp_path / "loose" / "x", minimal())
    assert boards.load(loose).root == loose.parent.resolve()


def test_an_explicit_root_wins(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "elsewhere").mkdir()
    path = board_file(tmp_path / "b", minimal('root: "../elsewhere"'))
    assert boards.load(path).root == (tmp_path / "elsewhere").resolve()
    path.write_text(minimal('root: "../missing"'), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"root .*missing is not a directory"):
        boards.load(path)


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (["colour: 3"], r"board setting colour is not one of description, root"),
        (['description: "a"', 'description: "b"'], "description is set twice"),
        (['plugin: "absent.py"'], r"plugin .*absent.py not found"),
        (['power: "dim"'], 'power is "on" or "off"'),
        (['models: ["models/*.cs"]'], r"models: 'models/\*.cs' matches no file"),
        (['models: "models/x.cs"'], "models is a list of glob patterns"),
        (["storage: flash0"], r"unknown name 'flash0'"),
        (["storage: [3]"], "storage is a list of peripherals"),
        (["rtc: 3"], "rtc is a peripheral or a device's own clock"),
        (['repl: ["extra.repl"]'], r"repl fragment .*extra.repl not found"),
        (["option boot: 1"], "boot is a setting every board takes from a system"),
        (["option a: 1", "option a: 2"], "option a is declared twice"),
        (["option a: [1]"], r"an option is choice\(\.\.\.\) or a plain value"),
        (["option a: choice()"], r"choice\(\) lists the values"),
        (["option a: choice(1, 2, default: 3)"], "the default 3 is not one of the values"),
        (["option a: choice(1, 1)"], "a value is listed twice"),
        (["if true { option a: 1 }"], "options are declared at the board's top level"),
        (['peripheral flash0: "SPI.Micron_MT25Q"'], "peripheral flash0 is on no link"),
        (["peripheral flash0: 3"], "a peripheral names its model"),
        (["link mcu: uart(members: [mcu.usart1])"], r"mcu is declared twice \(device and link\)"),
        (["link workdir: uart(members: [mcu.usart1])"], "workdir is the machine's directory"),
        (["link l: radio(members: [mcu.usart1])"], r"a link is declared as KIND\(\.\.\.\)"),
        (["link l: uart(speed: 3, members: [mcu.usart1])"], r"uart\(\) takes baud, frame"),
        (["link l: uart(members: [mcu])"], "members are device interfaces such as zynq.uart0"),
        (
            ["link l: uart(members: [mcu.usart1, mcu.usart2, mcu.usart3])"],
            "a UART joins two interfaces at most",
        ),
        (["link l: uart(baud: -1, members: [mcu.usart1])"], "baud is a positive number"),
        (['link l: uart(frame: "9X9", members: [mcu.usart1])'], "frame is data bits"),
        (
            ["link a: uart(members: [mcu.usart1])", "link b: uart(members: [mcu.usart1])"],
            "mcu.usart1 is on link a already",
        ),
        (
            ['peripheral f: "SPI.X"', "link l: spi(controller: mcu.spi1, targets: {0: mcu})"],
            "mcu is a device, and a device can't be an SPI target",
        ),
        (
            ['peripheral f: "SPI.X"', "link l: qspi(controller: mcu.spi1, targets: {0: mcu.spi2})"],
            "mcu.spi2 is a device, and a device can't be an QSPI target",
        ),
        (['peripheral f: "SPI.X"', "link l: spi(controller: f, targets: {0: f})"], "controller is"),
        (
            ['peripheral f: "I2C.X"', "link l: i2c(controller: mcu.i2c1, targets: {200: f})"],
            "7-bit",
        ),
        (
            [
                'peripheral f: "SPI.X"',
                "link a: spi(controller: mcu.spi1, targets: {0: f})",
                "link b: spi(controller: mcu.spi2, targets: {0: f})",
            ],
            "peripheral f is on link a already",
        ),
        (["link g: gpio(from: 3, to: mcu.pa[1])"], "from is one output"),
        (['link g: gpio(from: mcu.pa[0], to: mcu.pa[1], pull: "sideways")'], 'pull is "up"'),
        (["sensor s: mcu.adc1"], "sensor s is an analog input"),
        (['sensor s: mcu.adc1.channel[1] = "high"'], "sensor s starts at a voltage"),
        (["link l: uart(members: [other.usart1])"], r"unknown name 'other'"),
    ],
)
def test_board_file_errors(tmp_path: Path, lines: list[str], message: str) -> None:
    path = board_file(tmp_path / "boards" / "x", minimal(*lines), "x.repl")
    with pytest.raises(ConfigError, match=message):
        boards.load(path)


@pytest.mark.parametrize(
    ("device", "message"),
    [
        ('device mcu: 3 { firmware: "x.elf" }', r"a device is declared as FAMILY\(arguments\)"),
        (
            'device mcu: z80(part: "Z84C00") { }',
            r"no device family 'z80' \(known: stm32, zynq7000\)",
        ),
        ('device mcu: stm32(part: "STM32F407VG", speed: 3) { }', r"stm32\(\) takes part"),
        ("device mcu: stm32() { }", r"stm32\(\) needs part"),
        ('device mcu: stm32(part: "STM32H743") { }', "Jig has no table entry for STM32H743"),
        ('device mcu: stm32(part: "STM32F407VG", vector_table: -1) { }', "vector_table is an"),
        ('device mcu: stm32(part: "STM32F407VG") { colour: 1 }', "device setting colour"),
        ('device mcu: stm32(part: "STM32F407VG") { boot flash\n boot flash }', "listed twice"),
        ('device mcu: stm32(part: "STM32F407VG") { driver: "NoColon" }', "driver is 'module:Cl"),
        ('device mcu: stm32(part: "STM32F407VG") { commands: "x" }', "commands is a list"),
        (
            'device mcu: stm32(part: "STM32F407VG") { interface u: csp(address: 1) over kiss\n'
            " protocol csp(address: 1) }",
            "the device's protocol line; a stack names it",
        ),
        (
            'device mcu: stm32(part: "STM32F407VG") { protocol csp(address: 1)\n'
            " protocol csp(address: 2) }",
            "declares protocol csp twice",
        ),
        ('device mcu: stm32(part: "STM32F407VG") { protocol csp(address: 40) }', "0-31"),
        ('device mcu: stm32(part: "STM32F407VG") { interface u: cli(eol: "x") }', "cli eol is"),
        ('device mcu: stm32(part: "STM32F407VG") { fdcan1.size += 3 }', "PERIPHERAL.PROPERTY ="),
        ('device mcu: stm32(part: "STM32F407VG") { unmodelled r: 10 .. 2 }', "ends below"),
        ('device z: zynq7000(part: "XC9572") { }', "is not a Zynq-7000"),
        ('device z: zynq7000(xsa: "d.xsa") { }', "with an xsa needs ps_clk"),
        ("device z: zynq7000(clocks: {cpu_9x: 1 MHz}) { }", "cpu_9x is not a Zynq-7000 PS clock"),
        ('device z: zynq7000(straps: "usb") { }', "straps is one of"),
        ("device z: zynq7000(ddr: 1 KiB) { }", "ddr is the DDR's size"),
        ("device z: zynq7000() { ip b: 3 }", "an ip line names a model"),
        ('device z: zynq7000() { ip b: "M" (irq: 99) }', "IRQ_F2P index 0-15"),
        ('device z: zynq7000() { ip b: "M" (on: "x") }', "on is a block-design port"),
    ],
)
def test_device_errors(tmp_path: Path, device: str, message: str) -> None:
    path = board_file(tmp_path / "boards" / "x", minimal(device=device))
    with pytest.raises(ConfigError, match=message):
        boards.load(path)


def test_errors_name_the_board_file_and_line(tmp_path: Path) -> None:
    path = board_file(tmp_path / "boards" / "x", minimal("", "colour: 3"))
    with pytest.raises(ConfigError, match=r"^x/board\.jig:2:5: board setting colour"):
        boards.load(path)
    path.write_text('board "x" {\n    device d: stm32(\n}\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"^x/board\.jig:3:1: syntax error"):
        boards.load(path)


def test_a_second_device_comes_in_a_later_release(tmp_path: Path) -> None:
    second = 'device aux: stm32(part: "STM32F407VG") { firmware: "build/aux.elf" }'
    with pytest.raises(
        ConfigError, match=r"declares 2 devices \(aux, mcu\); boards with several devices come"
    ):
        load(tmp_path, second)
    path = board_file(tmp_path / "none" / "x", 'board "x" { description: "no device" }\n')
    with pytest.raises(ConfigError, match="declares no device"):
        boards.load(path)


# -------------------------------------------------------------------- options


def test_options_take_choices_plain_values_and_text(tmp_path: Path) -> None:
    lines = (
        "option node: choice(1, 2)",
        'option pl: choice({model: "a C# model", rtl: "the RTL"}, default: "rtl")',
        "option address: 10",
        "option fast: false",
    )
    board = load(tmp_path, *lines)
    assert board.option_values == {"node": 1, "pl": "rtl", "address": 10, "fast": False}
    assert board.options["pl"].choices == {"model": "a C# model", "rtl": "the RTL"}
    assert board.options["node"].choices == {1: "", 2: ""}
    assert board.options["address"].choices is None
    # Values from jig.toml and --option arrive as text.
    chosen = load(tmp_path, *lines, node="2", pl="model", address="0x20", fast="true")
    assert chosen.option_values == {"node": 2, "pl": "model", "address": 32, "fast": True}
    assert board.resolve_options({"node": 2}) == {
        "node": 2,
        "pl": "rtl",
        "address": 10,
        "fast": False,
    }
    for given, message in (
        ({"node": 3}, "option node is one of 1, 2, not 3"),
        ({"node": "three"}, "option node is one of 1, 2, not three"),
        ({"address": "ten"}, "option address takes a whole number, not ten"),
        ({"speed": 1}, r"has no option speed \(has: node, pl, address, fast\)"),
    ):
        with pytest.raises(ConfigError, match=message):
            load(tmp_path, *lines, **given)


def test_if_picks_declarations_in_boards_and_devices(tmp_path: Path) -> None:
    device = (
        'device mcu: stm32(part: "STM32F407VG") {\n'
        '        firmware: "build/node-{node}.elf"\n'
        "        if node == 1 { interface usart1: cli }\n"
        '        elif node == 2 { interface usart1: cli(eol: "\\n") }\n'
        "        else { interface usart2: cli }\n"
        "    }"
    )
    lines = (
        "option node: choice(1, 2, 3)",
        "if node == 3 {",
        "    external link console: uart(baud: 9600, members: [mcu.usart2])",
        "} else {",
        "    external link console: uart(baud: 115200, members: [mcu.usart1])",
        "}",
    )
    first = load(tmp_path, *lines, device=device)
    assert first.firmware["app"].name == "node-1.elf"
    assert first.interface("console").settings == {"baud": 115200, "frame": "8N1", "eol": "\r"}
    second = load(tmp_path, *lines, device=device, node=2)
    assert second.interface("console").eol == "\n"
    third = load(tmp_path, *lines, device=device, node=3)
    assert (third.interface("console").peripheral, third.interface("console").settings["baud"]) == (
        "usart2",
        9600,
    )
    assert second.evaluate({"node": 3}).interface("console").peripheral == "usart2"


def test_names_resolve_before_their_declarations(tmp_path: Path) -> None:
    device = (
        'device zynq: zynq7000(part: "XC7Z020-CLG484",\n'
        "                      clocks: {cpu_3x2x: 1 MHz, cpu_1x: 1 MHz}) {\n"
        '        firmware: {app: "app.elf"}\n'
        "    }"
    )
    lines = (
        "storage: [flash0]",  # a setting before the peripheral it names
        "link boot_flash: qspi(controller: zynq.qspi, targets: {0: flash0})",
        'peripheral flash0: "SPI.Micron_MT25Q" (size: 32 MiB)',
        'link irq: gpio(from: chip.IRQ, to: [zynq.mio[7], zynq.ps_srst_b], pull: "up")',
        'peripheral chip: "Sensors.TI_TMP108"',
        "link sensors: i2c(controller: zynq.i2c0, targets: {0x48: chip})",
    )
    board = load(tmp_path, *lines, device=device)
    flash = board.peripheral("flash0")
    assert flash.bus == ("zynq", "qspi", 0) and flash.path == "qspi.flash0"
    assert flash.properties == {"size": 0x2000000}
    assert board.storage[0].size == 0x2000000
    irq = board.link("irq")
    assert repr(irq.source) == "chip.IRQ"
    assert [repr(sink) for sink in irq.sinks] == ["zynq.mio[7]", "zynq.ps_srst_b"]
    assert board.peripheral("chip").path == "i2c0.chip"


def test_strings_see_options_and_the_machine_directory(tmp_path: Path) -> None:
    device = (
        'device mcu: stm32(part: "STM32F407VG") {\n'
        '        firmware: "build/{name}.elf"\n'
        '        commands: ["sysbus LogFile @{workdir}/log.txt"]\n'
        "    }"
    )
    path = board_file(tmp_path / "boards" / "x", 'const name = "app"\n' + minimal(device=device))
    board = boards.load(path)
    assert board.device.commands == ("sysbus LogFile @<workdir>/log.txt",)
    assert board.firmware["app"].name == "app.elf"
    assert boards.load(path, workdir="D:/runs/dut").device.commands == (
        "sysbus LogFile @D:/runs/dut/log.txt",
    )


def test_references_name_what_they_point_at(tmp_path: Path) -> None:
    board = load(
        tmp_path,
        'peripheral adc: "Analog.X"',
        "link bus: spi(controller: mcu.spi1, targets: {0: adc})",
        "sensor rail: adc.channel[3] = 1.2 V",
        "sensor core: mcu.adc1.channel[16]",
        "rtc: mcu.rtc",
    )
    rail, core = board.sensor_specs
    assert rail.channel.owner == PeripheralRef("adc") and rail.channel.channel == 3
    assert rail.initial == 1.2
    assert core.channel.owner == InternalRef("mcu", "adc1") and core.initial is None
    assert board.rtc == "rtc"
    assert board.sensor("rail").adc == "spi1.adc"
    assert board.sensor("core").adc == "adc1"


def test_housekeeping_requests_in_every_form(tmp_path: Path) -> None:
    for written, wanted in (
        ('"01"', b"\x01"),
        ('"0x0102"', b"\x01\x02"),
        ('"01 02 ff"', b"\x01\x02\xff"),
        ("0x01", b"\x01"),
        ("[1, 2]", b"\x01\x02"),
        ("[]", b""),
        ('x"01 02"', b"\x01\x02"),
    ):
        device = (
            'device mcu: stm32(part: "STM32F407VG") {\n'
            f'        protocol csp(housekeeping: {{port: 7, codec: "c", request: {written}}})\n'
            "    }"
        )
        assert load(tmp_path, device=device).section("csp").housekeeping.request == wanted


def test_models_globs_resolve_against_the_board_directory(tmp_path: Path) -> None:
    for name in ("models/a/One.cs", "models/b/Two.cs", "models/b/notes.txt", "shared/Three.cs"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    path = board_file(
        tmp_path / "boards" / "x",
        minimal('models: ["../../models/**/*.cs", "../../shared/Three.cs"]'),
    )
    board = boards.load(path)
    assert [p.relative_to(tmp_path.resolve()).as_posix() for p in board.models] == [
        "models/a/One.cs",
        "models/b/Two.cs",
        "shared/Three.cs",
    ]
    path.write_text(minimal('models: ["../../models/b/*"]'), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"matches notes.txt, not a .cs file"):
        boards.load(path)


def test_model_sets_hold_each_file_once_per_repository(tmp_path: Path) -> None:
    node = boards.find("example-node")
    package = boards.find("example-package")
    (only,) = boards.model_sets([node, package, node])
    assert only.root == FIXTURES
    assert only.name == "fixtures"
    assert only.files == node.models
    other_root = tmp_path / "other"
    (other_root / ".git").mkdir(parents=True)
    (other_root / "m").mkdir()
    (other_root / "m" / "Four.cs").write_text("", encoding="utf-8")
    other = boards.load(board_file(other_root / "b", minimal('models: ["../m/*.cs"]')))
    first, second = boards.model_sets([package, other])
    assert (first.root, second.root) == (FIXTURES, other_root.resolve())
    assert second.files == (other_root.resolve() / "m" / "Four.cs",)
    assert boards.model_sets([boards.find("example-zynq")]) == []


def test_library_directories_belong_to_boards() -> None:
    assert boards.library_dir("example-node") == BOARDS / "example-node" / "lib"
