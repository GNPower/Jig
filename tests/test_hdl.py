"""Artifacts derived from the hardware design: airhdl register maps, ps7_init, clocks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jig.errors import ConfigError
from jig.ext import airhdl
from jig.ext.zynq7000.clocks import SlcrState, ZynqClocks
from jig.ext.zynq7000.ps7init import Operation, Ps7Init, write_program

from .conftest import FIXTURES, PS7_INIT, make_xsa

REGS = FIXTURES / "hdl"


def test_register_map_from_rtl() -> None:
    regs = airhdl.parse(REGS / "example_regs_pkg.sv", REGS / "example_regs.sv")
    assert regs.name == "example_regs"
    assert [r.name for r in regs.registers] == ["CONTROL", "STATUS", "EVENTS", "EVENTS_MASK"]
    control = regs.register("control")
    assert (control.offset, control.access, control.reset) == (0x0, "rw", 0x50)
    assert regs.register("STATUS").access == "ro"
    events = {f.name: f.kind for f in regs.register("EVENTS").fields}
    assert events == {"done": "w1c", "overflow": "value"}
    # EVENTS_MASK_DONE belongs to the longer register name, not to EVENTS.
    mask = regs.register("EVENTS_MASK")
    assert [(f.name, f.reset) for f in mask.fields] == [("done", 1)]
    memories = {m.name: (m.offset, m.depth, m.readable) for m in regs.memories}
    assert memories == {"BUFFER": (0x40, 16, False), "TABLE": (0x80, 8, True)}
    document = json.loads(regs.to_json())
    assert {r["name"]: r["reset"] for r in document["registers"]}["CONTROL"] == 0x50


def test_register_map_written_as_json(tmp_path: Path) -> None:
    regs = airhdl.parse(REGS / "example_regs_pkg.sv", REGS / "example_regs.sv")
    path = airhdl.write_json(regs, tmp_path / "maps" / "map.json")
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "example_regs"


def test_register_map_needs_registers(tmp_path: Path) -> None:
    package = tmp_path / "empty_regs_pkg.sv"
    package.write_text("package empty_regs_pkg;\nendpackage\n")
    with pytest.raises(ConfigError, match="no registers found"):
        airhdl.parse(package)


def test_ps7_init_from_xsa(tmp_path: Path) -> None:
    ps7 = Ps7Init.from_xsa(make_xsa(tmp_path / "design.xsa"), ps_version=3)
    polls = [(op.address, op.mask) for op in ps7.init if op.kind == "P"]
    # PLL lock (ARM, DDR, IO), DCI done, DDRC operating mode.
    assert polls == [
        (0xF800010C, 0x1),
        (0xF800010C, 0x2),
        (0xF800010C, 0x4),
        (0xF8000B74, 0x2000),
        (0xF8006054, 0x7),
    ]
    writes = [(op.address, op.value) for op in ps7.post_config if op.kind in "WM"]
    assert (0xF8000240, 0x0) in writes  # FPGA_RST_CTRL released
    assert Operation("W", 0xE0001034, value=0x6) in ps7.init  # mwr -force
    assert any(op.kind == "#" and op.text.startswith("mask_delay") for op in ps7.init)


def test_ps7_init_needs_the_silicon_version() -> None:
    with pytest.raises(ConfigError, match="has no ps7_mio_init_data_1_0"):
        Ps7Init.from_tcl(PS7_INIT.read_text(encoding="utf-8"), ps_version=0)


def test_ps7_init_rejects_unknown_statements() -> None:
    tcl = """
proc ps7_mio_init_data_3_0 {} {
    mask_write 0XF8000008 0x0000FFFF 0x0000DF0D
    something_else 1 2
}
"""
    with pytest.raises(ConfigError, match="unsupported statement"):
        Ps7Init.from_tcl(tcl, ps_version=3)


def test_register_program_format(tmp_path: Path) -> None:
    program = write_program(
        [
            Operation("#", text="stage"),
            Operation("W", 0xF8000008, value=0xDF0D),
            Operation("M", 0xF8000100, 0x7F000, 0x1B000),
            Operation("P", 0xF800010C, 0x1),
        ],
        tmp_path / "p.prog",
    )
    assert program.read_text().splitlines() == [
        "# stage",
        "W 0xF8000008 0x0000DF0D",
        "M 0xF8000100 0x0007F000 0x0001B000",
        "P 0xF800010C 0x00000001",
    ]


def test_clock_tree() -> None:
    ps7 = Ps7Init.from_tcl(PS7_INIT.read_text(encoding="utf-8"), ps_version=3)
    clocks = ZynqClocks.from_operations(ps7.init + ps7.post_config, ps_clk=50e6)
    assert (clocks.arm_pll, clocks.ddr_pll, clocks.io_pll) == pytest.approx(
        (1300e6, 1050e6, 1000e6)
    )
    assert clocks.cpu_6x4x == pytest.approx(650e6)
    assert clocks.cpu_3x2x == pytest.approx(325e6)
    assert clocks.cpu_2x == pytest.approx(650e6 / 3)
    assert clocks.cpu_1x == pytest.approx(650e6 / 6)
    assert clocks.uart_ref == pytest.approx(50e6)
    assert clocks.lqspi_ref == pytest.approx(200e6)
    assert clocks.spi_ref == pytest.approx(1000e6 / 6)
    assert clocks.can_ref == pytest.approx(100e6)
    assert clocks.pcap == pytest.approx(200e6)
    assert clocks.fclk[:2] == pytest.approx((100e6, 50e6))
    assert clocks.fclk[2:] == (None, None)


def test_clock_needs_every_field_it_uses() -> None:
    state = SlcrState()
    del state.written[0x120]  # ARM_CLK_CTRL unknown
    with pytest.raises(ConfigError, match="0x120"):
        ZynqClocks.from_state(state, ps_clk=33.333e6)


def test_pll_bypass_uses_ps_clk() -> None:
    state = SlcrState().apply([Operation("M", 0xF8000100, 0x10, 0x10)])  # ARM PLL_BYPASS_FORCE
    clocks = ZynqClocks.from_state(state, ps_clk=50e6)
    assert clocks.arm_pll == pytest.approx(50e6)
