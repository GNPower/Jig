"""Board definitions, the artifact cache, transports' text formats and reports."""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from jig import artifacts, project
from jig.errors import ConfigError
from jig.ext import cosim
from jig.report.report import CaseReport, SuiteReport, write_junit
from jig.runtime.scheduler import seconds_to_ns
from jig.system import boards
from jig.transports.can import parse_frames

from .conftest import FIXTURES


def test_fixture_board_definition() -> None:
    board = boards.find("example-zynq")
    assert board.soc == "zynq7000"
    assert board.platform.name == "example-zynq.repl"
    assert board.performance_mips == 120
    assert board.default_boot == "jtag"
    assert board.interface("console").peripheral == "uart1"
    assert board.interface("console").settings == {"baud": 115200}
    assert board.interface("console").eol == "\r"
    assert board.interface("kiss").peripheral == "uart0"
    assert board.interface("can").kind == "can"
    assert {s.name: s.size for s in board.storage} == {"flash0": 0x1000000}
    assert board.section("csp").address == 5
    assert board.sensor("supply").channel == 2
    assert board.rtc == "rtc0"
    assert board.power_restart == ("flash0", "adc0")
    assert board.power_initial == "on"
    assert board.register_maps == ()
    assert board.source == FIXTURES / "boards" / "example-zynq" / "board.yaml"
    # Repository paths resolve against the board's root (root: ../..).
    assert board.root == FIXTURES
    assert board.firmware["app"] == (FIXTURES / "build" / "app.elf").resolve()
    pl = board.options["pl"]
    assert pl.default == "model"
    assert set(pl.choices) == {"model", "rtl"}
    targets = {source.target for source in pl.choice("model").register_maps}
    assert targets == {"block.registerMap"}
    rtl = pl.choice("rtl")
    assert rtl.cosim is not None and rtl.cosim.peripheral == "block"
    assert rtl.platform is not None and rtl.platform.name == "rtl.repl"
    assert rtl.commands == ("block Trace @{workdir}/block.vcd",)
    assert rtl.cosim.inputs == ((FIXTURES / "hdl").resolve(),)


def test_board_options_resolve_to_defaults_and_choices() -> None:
    board = boards.find("example-zynq")
    assert board.resolve_options(None) == {"pl": "model"}
    assert board.resolve_options({"pl": "rtl"}) == {"pl": "rtl"}
    with pytest.raises(ConfigError, match="not one of model, rtl"):
        board.resolve_options({"pl": "fpga"})
    with pytest.raises(ConfigError, match="no option boot_rom"):
        board.resolve_options({"boot_rom": "x"})


def test_board_option_default_must_be_a_choice(tmp_path: Path) -> None:
    (tmp_path / "x.repl").write_text("")
    path = tmp_path / "board.yaml"
    path.write_text(
        "schema: 2\nname: x\nsoc: zynq7000\nplatform: x.repl\n"
        "options:\n  pl:\n    default: rtl\n    model: {platform: x.repl}\n"
    )
    with pytest.raises(ConfigError, match="default 'rtl' is not one of its choices"):
        boards.load(path)


def test_cosim_clock_and_sources(tmp_path: Path) -> None:
    board = boards.find("example-zynq")
    rtl = board.options["pl"].choice("rtl")
    assert rtl.cosim is not None and rtl.platform is not None
    assert cosim.clock_hz(rtl.cosim, rtl.platform) == 20_000_000
    listed = cosim.listed_sources(rtl.cosim.sources)
    assert [path.name for path in listed] == ["example_block.sv", "example_top.sv"]
    description = tmp_path / "odd.repl"
    description.write_text(
        "block: CoSimulated.CoSimulatedPeripheral @ sysbus 0x0\n    frequency: 1500\n"
    )
    with pytest.raises(ConfigError, match="whole number of MHz"):
        cosim.clock_hz(rtl.cosim, description)


def test_unknown_board_lists_the_known_ones() -> None:
    with pytest.raises(ConfigError, match="example-stm32, example-zynq"):
        boards.find("no-such-board")


def test_board_name_must_match_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "boards" / "alpha"
    directory.mkdir(parents=True)
    (directory / "a.repl").write_text("")
    (directory / "board.yaml").write_text("schema: 2\nname: beta\nsoc: stm32\nplatform: a.repl\n")
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    assert boards.available() == ["alpha"]
    with pytest.raises(ConfigError, match="differs from its directory"):
        boards.find("alpha")


def test_board_schema_is_checked(tmp_path: Path) -> None:
    (tmp_path / "x.repl").write_text("")
    path = tmp_path / "board.yaml"
    path.write_text("schema: 99\nname: x\nsoc: zynq7000\nplatform: x.repl\n")
    with pytest.raises(ConfigError, match="schema"):
        boards.load(path)


def test_board_interface_kind_is_checked(tmp_path: Path) -> None:
    (tmp_path / "x.repl").write_text("")
    path = tmp_path / "board.yaml"
    path.write_text(
        "schema: 2\nname: x\nsoc: zynq7000\nplatform: x.repl\n"
        "interfaces:\n  radio: {kind: spacewire, peripheral: sw0}\n"
    )
    with pytest.raises(ConfigError, match="spacewire"):
        boards.load(path)


def test_artifact_cache_builds_once_per_input(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    source.write_text("one")
    builds: list[Path] = []

    def build(directory: Path) -> None:
        builds.append(directory)
        (directory / "result.txt").write_text(source.read_text().upper())

    first = artifacts.cached("demo", [source], build)
    again = artifacts.cached("demo", [source], build)
    assert first == again and len(builds) == 1
    assert first.parent == project.cache_dir() / "artifacts"
    assert (first / "result.txt").read_text() == "ONE"
    source.write_text("two")
    changed = artifacts.cached("demo", [source], build)
    assert changed != first and len(builds) == 2
    assert artifacts.cached("demo", [source], build, {"option": 1}) != changed


def test_artifact_cache_discards_failed_builds(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    source.write_text("x")

    def build(directory: Path) -> None:
        raise RuntimeError("tool failed")

    with pytest.raises(RuntimeError):
        artifacts.cached("broken", [source], build)
    assert not any(artifacts.cache_root().glob("broken-*"))


def test_can_bus_read_format() -> None:
    frames = parse_frames(
        "1200:18C0800:E@harness:80307000000F7069 1350:7FF:@node1.can0: 1400:123:ER@x.y:"
    )
    assert [(f.time_us, f.id, f.extended, f.remote, f.sender) for f in frames] == [
        (1200, 0x18C0800, True, False, "harness"),
        (1350, 0x7FF, False, False, "node1.can0"),
        (1400, 0x123, True, True, "x.y"),
    ]
    assert frames[0].data == bytes.fromhex("80307000000F7069")
    assert frames[1].data == b""


def test_simulated_time_conversion() -> None:
    assert seconds_to_ns(0.3) == 300_000_000
    assert seconds_to_ns(1e-9) == 1
    with pytest.raises(ValueError):
        seconds_to_ns(-1)


def test_junit_report(tmp_path: Path) -> None:
    suite = SuiteReport(name="plan", source="plans/x.jig", wall_seconds=2.5, simulated_seconds=1.25)
    suite.cases += [
        CaseReport("passes", "PASS", simulated_seconds=0.5),
        CaseReport("fails", "FAIL", "expected X"),
        CaseReport("breaks", "ERROR", "bad plan"),
    ]
    aborted = SuiteReport(name="other", source="plans/y.jig", aborted="never booted")
    path = write_junit([suite, aborted], tmp_path / "junit.xml")
    root = ET.parse(path).getroot()
    first, second = root.findall("testsuite")
    assert (first.get("tests"), first.get("failures"), first.get("errors")) == ("3", "1", "1")
    assert first.find("testcase[@name='fails']/failure") is not None
    error = second.find("testcase/error")
    assert error is not None and error.text == "never booted"
    assert not suite.ok and not aborted.ok


def test_prune_runs_keeps_the_newest(tmp_path: Path) -> None:
    from jig.system.system import prune_runs

    names = ["20261008-010000-a", "20261008-020000-b", "20261008-030000-c", "notes"]
    hour_ago = time.time() - 3600
    for name in names:
        (tmp_path / name).mkdir()
        os.utime(tmp_path / name, (hour_ago, hour_ago))
    removed = prune_runs(tmp_path, 1)
    assert sorted(p.name for p in removed) == ["20261008-010000-a", "20261008-020000-b"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["20261008-030000-c", "notes"]
    assert prune_runs(tmp_path, -1) == []


def test_prune_runs_spares_a_run_in_use(tmp_path: Path) -> None:
    from jig.system.system import prune_runs

    hour_ago = time.time() - 3600
    for name in ("20261008-010000-a", "20261008-020000-b", "20261008-030000-c"):
        (tmp_path / name).mkdir()
        os.utime(tmp_path / name, (hour_ago, hour_ago))
    log = tmp_path / "20261008-010000-a" / "events.jsonl"
    log.write_text("{}\n")  # still being written: the oldest name, yet in use
    os.utime(tmp_path / "20261008-010000-a", (hour_ago, hour_ago))
    removed = prune_runs(tmp_path, 1)
    assert [p.name for p in removed] == ["20261008-020000-b"]


def test_repl_properties_merge_entries(tmp_path: Path) -> None:
    from jig.repl import properties

    path = tmp_path / "x.repl"
    path.write_text(
        'using "base.repl"\n\n'
        "uart0: UART.X @ sysbus 0x1000 // comment: ignored\n"
        "    clockFrequency: 1000\n"
        "    -> gic@27\n\n"
        "uart0:\n"
        "    clockFrequency: 0x2000 // the later entry wins\n"
        '    name: "a // b"\n'
        "    init:\n"
        '        Tag <0x1, 0x2> "x"\n'
    )
    assert properties(path) == {"uart0": {"clockFrequency": 0x2000, "name": "a // b"}}
