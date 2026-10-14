"""Pieces for several boards and long runs, without Renode: the STM32 board
and driver, UART stamps from several machines, bus decoding, telemetry against
a profile, and the report's recordings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jig.errors import ToolchainError
from jig.ext.csp import protocol as csp
from jig.ext.csp.observer import BusObserver
from jig.lang.library import BoardHandle, DslError, builtin_functions
from jig.lang.values import Record
from jig.report.html import render
from jig.report.report import CaseReport, SuiteReport
from jig.runtime import environment
from jig.system import boards
from jig.system.socs import MachineConfig, driver_for
from jig.system.socs.stm32 import Stm32Driver
from jig.transports.can import CanBusPort, CanFrame
from jig.transports.uart import parse_collected


def test_stm32_board_definition() -> None:
    board = boards.find("example-stm32")
    assert board.soc == "stm32"
    assert board.section("csp").address == 1
    assert board.interface("console").peripheral == "usart2"
    assert board.interface("can").kind == "can"
    driver = driver_for("stm32")
    assert isinstance(driver, Stm32Driver)
    assert driver.boot_modes == ("flash",)
    assert driver._vector_table(board) == 0x08000000
    assert driver._settings(board)["output"] == "firmware.elf"
    config = MachineConfig(
        name="dut", board=board, boot="flash", performance_mips=168, workdir=Path("unused")
    )
    assert driver.overlay(config) == "cpu:\n    PerformanceInMips: 168\n"


def test_stm32_driver_names_a_missing_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    board = boards.find("example-stm32")
    monkeypatch.delenv("ARM_NONE_EABI_GCC", raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ToolchainError, match="arm-none-eabi-gcc is not on PATH"):
        Stm32Driver.compiler()
    monkeypatch.setenv("ARM_NONE_EABI_GCC", "/nowhere/arm-none-eabi-gcc")
    with pytest.raises(ToolchainError, match="does not exist"):
        Stm32Driver.compiler()
    problems = Stm32Driver().check(board)
    assert any("ARM_NONE_EABI_GCC" in problem for problem in problems)


def test_stm32_board_without_a_firmware_build_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x.repl").write_text("")
    path = tmp_path / "board.yaml"
    path.write_text("schema: 2\nname: x\nsoc: stm32\nplatform: x.repl\nstm32: {}\n")
    board = boards.load(path)
    (problem,) = Stm32Driver().check(board)
    assert "stm32.firmware must give elf, command and output" in problem


def test_uart_stamps_never_go_back() -> None:
    # An unsigned gap of -10 µs arrives as 2**64 - 10: the time holds instead.
    name, data, stamps = parse_collected(f"U node1_uart1 100,{2**64 - 10},5 414243")
    assert (name, data) == ("node1_uart1", b"ABC")
    assert stamps == [100, 100, 105]


def _frames(packet: csp.Packet, ident: int, start_us: int) -> list[CanFrame]:
    return [
        CanFrame(frame_id, data, time_us=start_us + 130 * index)
        for index, (frame_id, data) in enumerate(csp.cfp_fragments(packet, ident))
    ]


def test_observer_counts_packets_with_a_bad_crc() -> None:
    port = CanBusPort(None, "can0")  # type: ignore[arg-type]
    observer = BusObserver(port)
    good = csp.crc32_append(b"ping")
    bad = good[:-1] + bytes([good[-1] ^ 0xFF])
    flags = csp.FLAG_CRC32
    port.feed(
        _frames(csp.Packet(csp.CspId(src=8, dst=3, dport=1, sport=33, flags=flags), good), 1, 0)
    )
    port.feed(
        _frames(csp.Packet(csp.CspId(src=8, dst=3, dport=1, sport=34, flags=flags), bad), 2, 500)
    )
    assert observer.total == 2 and observer.crc_errors == 1
    assert [p.crc_ok for p in observer.packets] == [True, False]


class _Sensors:
    """The part of a Machine that off_profile asks: codes for volts."""

    def sensor_code(self, name: str, volts: float | None = None) -> int:
        assert volts is not None
        return round(volts * 1000)


def _off_profile() -> Any:
    return builtin_functions(Path(), lambda: 0.0)["off_profile"]


def _reply(time: float, code: int) -> Record:
    return Record(time=time, power=Record(rail_v=code))


def test_off_profile_accepts_codes_from_the_lag_window() -> None:
    board = BoardHandle(_Sensors())  # type: ignore[arg-type]
    steps = environment.Table((0.0, 3.0), (1.0, 2.0), hold=True)  # 1 V, 2 V from 3 s
    off_profile = _off_profile()
    replies = [
        _reply(10.5, 1000),  # profile started at 10 s: 1 V
        _reply(13.2, 1000),  # the 2 V update at 13 s is not sampled yet
        _reply(14.0, 1500),  # averaged across the update
        _reply(16.9, 2000),
        _reply(17.5, 1000),  # 1 V was last in force at 13 s, outside the 3 s lag
        _reply(9.0, 0),  # before the profile started: not judged
    ]
    off = off_profile(board, "rail_v", steps, replies, since=10.0)
    assert off == [Record(time=17.5, code=1000, expected=[2000])]
    with pytest.raises(DslError, match="profile is a profile"):
        off_profile(board, "rail_v", 1.0, replies, since=10.0)
    with pytest.raises(DslError, match="no number rail_i"):
        off_profile(board, "rail_i", steps, replies, since=10.0)


def test_report_lists_the_recordings_and_events(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    suite = SuiteReport(name="soak", source="plans/soak.jig")
    suite.cases.append(CaseReport(name="one hour", status="PASS"))
    suite.recordings = {"events.jsonl": (str(events), 1803)}
    suite.events = {"node1 sensor": 1800, "gs link": 4}
    page = render([suite])
    assert "<title>jig report</title>" in page and "<h1>jig run</h1>" in page
    assert f"href='{events.as_uri()}'>events.jsonl</a> (1803)" in page
    assert "<td>gs link</td><td>4</td>" in page
