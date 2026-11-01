"""Board definitions, the artifact cache, transports' text formats and reports."""

from __future__ import annotations

import dataclasses
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


def test_the_fixture_options_pick_a_model_or_the_rtl() -> None:
    board = boards.find("example-zynq")
    assert board.soc == "zynq7000"
    assert board.source == FIXTURES / "boards" / "example-zynq" / "board.jig"
    pl = board.options["pl"]
    assert pl.default == "model"
    assert set(pl.choices or {}) == {"model", "rtl"}
    (block,) = board.device.ips
    assert (block.model, block.on, block.cosim) == (
        "Example.ExampleBlock",
        (0x43C00000, 0x43C00FFF),
        None,
    )
    assert {source.target for source in board.register_maps} == {"block.registerMap"}
    assert board.cosim == () and board.device.commands == ()
    rtl = boards.find("example-zynq", options={"pl": "rtl"}, workdir="W:/run/dut")
    (block,) = rtl.device.ips
    assert block.model == "CoSimulated.CoSimulatedPeripheral"
    assert block.properties == {"frequency": 20_000_000, "limitBuffer": 1000}
    (spec,) = rtl.cosim
    assert spec.peripheral == "block" and spec.frequency == 20_000_000
    assert spec.inputs == ((FIXTURES / "hdl").resolve(),)
    assert spec.context == "trace=W:/run/dut/block.vcd"
    assert rtl.device.commands == ("block Trace @W:/run/dut/block.vcd",)
    assert rtl.register_maps == ()


def test_board_options_resolve_to_defaults_and_choices() -> None:
    board = boards.find("example-zynq")
    assert board.resolve_options(None) == {"pl": "model"}
    assert board.resolve_options({"pl": "rtl"}) == {"pl": "rtl"}
    with pytest.raises(ConfigError, match="option pl is one of model, rtl, not fpga"):
        board.resolve_options({"pl": "fpga"})
    with pytest.raises(ConfigError, match="no option boot_rom"):
        board.resolve_options({"boot_rom": "x"})


def test_project_default_options_apply_to_boards_that_have_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "jig.toml").write_text(
        f'[paths]\nboards = ["{(FIXTURES / "boards").as_posix()}"]\n'
        '[defaults]\noptions = {pl = "rtl", node = 2}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    assert boards.find("example-zynq").option_values == {"pl": "rtl"}
    assert boards.find("example-zynq", options={"pl": "model"}).option_values == {"pl": "model"}
    assert boards.find("example-node").option_values == {}


def test_cosim_clock_and_sources() -> None:
    (spec,) = boards.find("example-zynq", options={"pl": "rtl"}).cosim
    assert cosim.clock_hz(spec) == 20_000_000
    listed = cosim.listed_sources(spec.sources)
    assert [path.name for path in listed] == ["example_block.sv", "example_top.sv"]
    with pytest.raises(ConfigError, match="whole number of MHz"):
        cosim.clock_hz(dataclasses.replace(spec, frequency=1500))


def test_unknown_board_lists_the_known_ones() -> None:
    with pytest.raises(ConfigError, match="example-stm32, example-zynq"):
        boards.find("no-such-board")


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
