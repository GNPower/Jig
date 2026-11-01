"""The Zynq-7000 family without Renode: device arguments, the platform
description it writes (clocks from ps7_init or the clocks argument, straps,
DDR, ip lines, pins), the names a block design gives the device, generated
artifacts, the overlay and the image builder hook."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

from jig import repl
from jig.errors import ConfigError
from jig.ext.zynq7000 import blockdesign
from jig.ext.zynq7000.blockdesign import AddressRange, Cosim, IpOverride, Literal, Memory, Reference
from jig.ext.zynq7000.driver import CLOCK_TABLE, STRAPS, ImageBuilder, Zynq7000Driver
from jig.system import boards, checks, generate
from jig.system.model import InternalRef, PinRef
from jig.system.socs import MachineConfig, driver_for

from .conftest import BITSTREAM, FIXTURES, PS7_INIT


def _config(tmp_path: Path, options: dict[str, str] | None = None) -> MachineConfig:
    board = boards.find("example-zynq", options=options)
    return MachineConfig(
        name="dut",
        board=board,
        boot="jtag",
        performance_mips=board.performance_mips,
        workdir=tmp_path / "dut",
        options=dict(board.option_values),
    )


def _description(board: boards.Board) -> dict[str, dict[str, Any]]:
    """The generated description's properties by peripheral."""
    found: dict[str, dict[str, Any]] = repl.properties_of(
        generate.description(board, Zynq7000Driver())
    )
    return found


def test_the_registry_finds_the_driver() -> None:
    driver = driver_for("zynq7000")
    assert isinstance(driver, Zynq7000Driver)
    assert driver.boot_modes == ("jtag", "qspi")
    assert STRAPS["qspi"] == 1
    with pytest.raises(ConfigError, match="no device family 'z80'"):
        driver_for("z80")


def test_clocks_come_from_the_xsas_ps7_init(project: Path) -> None:
    board = boards.find("example-zynq")
    clocks, source = Zynq7000Driver().clocks(board)
    assert source == "the XSA's ps7_init at PS_CLK 50 MHz"
    found = _description(board)
    # The fixture's ps7_init: CPU_3x2x 325 MHz, UART_REF 50 MHz, CAN_REF 100 MHz,
    # LQSPI_REF 200 MHz, the clocks the board's hand-written repl stated before.
    assert found["globalTimer"]["frequency"] == found["privateTimer"]["frequency"] == 325_000_000
    assert found["uart1"]["clockFrequency"] == 50_000_000
    assert found["can0"]["referenceClockFrequency"] == 100_000_000
    assert found["qspi"]["referenceClockFrequency"] == 200_000_000
    for clock, targets in CLOCK_TABLE.items():
        for peripheral, prop in targets:
            if clock in clocks:
                assert found[peripheral][prop] == round(clocks[clock]), (clock, peripheral)
    assert found["slcr"]["bootMode"] == 0
    assert found["ddr"]["size"] == 0x1FF00000


def test_the_clocks_argument_goes_through_the_family_table(tmp_path: Path) -> None:
    path = tmp_path / "boards" / "z" / "board.jig"
    path.parent.mkdir(parents=True)
    path.write_text(
        'board "z" {\n'
        "    device zynq: zynq7000(clocks: {cpu_3x2x: 333.333333 MHz, cpu_1x: 111.111111 MHz,\n"
        "        uart_ref: 50 MHz, spi_ref: 166.666667 MHz, can_ref: 100 MHz,\n"
        "        lqspi_ref: 200 MHz, pcap: 100 MHz}) {\n"
        '        firmware: {app: "app.elf"}\n'
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    found = _description(boards.load(path))
    assert found["globalTimer"]["frequency"] == 333_333_333
    assert found["privateTimer"]["frequency"] == 333_333_333
    assert found["swdt"]["frequency"] == found["ttc0"]["frequency"] == 111_111_111
    assert found["ttc1"]["frequency"] == 111_111_111
    assert found["uart0"]["clockFrequency"] == found["uart1"]["clockFrequency"] == 50_000_000
    assert found["spi0"]["referenceClockFrequency"] == 166_666_667
    assert found["spi1"]["referenceClockFrequency"] == 166_666_667
    assert found["can1"]["referenceClockFrequency"] == 100_000_000
    assert found["qspi"]["referenceClockFrequency"] == 200_000_000
    assert found["devc"]["pcapClockFrequency"] == 100_000_000
    assert "slcr" not in found and "ddr" not in found  # no straps, no ddr given
    path.write_text(
        path.read_text(encoding="utf-8").replace("cpu_1x: 111.111111 MHz,", ""), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="needs the cpu_1x clock"):
        generate.description(boards.load(path), Zynq7000Driver())


def test_a_missing_xsa_is_reported() -> None:
    board = boards.find("example-zynq")  # tests/fixtures holds no build/example.xsa
    problems = Zynq7000Driver().check(board)
    assert any("XSA" in problem and "not found" in problem for problem in problems)
    with pytest.raises(ConfigError, match=r"XSA .* not found"):
        generate.description(board, Zynq7000Driver())


def test_ip_lines_with_an_address_range(project: Path) -> None:
    text = generate.description(boards.find("example-zynq"), Zynq7000Driver())
    assert "block: Example.ExampleBlock @ sysbus <0x43C00000, +0x1000>" in text
    assert "registerMap" not in text  # the overlay fills it in per run
    assert "// example.xsa holds no block design: no PL instances." in text
    assert "// Sensor inputs" not in text  # the supply sensor starts at no voltage
    rtl = generate.description(boards.find("example-zynq", options={"pl": "rtl"}), Zynq7000Driver())
    assert "block: CoSimulated.CoSimulatedPeripheral @ sysbus <0x43C00000, +0x1000>" in rtl
    assert "    frequency: 20000000\n    limitBuffer: 1000" in rtl


@pytest.mark.parametrize(
    ("line", "gic"), [(0, "gic@29"), (7, "gic@36"), (8, "gic@52"), (15, "gic@59")]
)
def test_irq_f2p_lines_reach_the_gic(tmp_path: Path, line: int, gic: str) -> None:
    """IRQ_F2P[7:0] are interrupt IDs 61-68 and IRQ_F2P[15:8] IDs 84-91 (UG585
    v1.7 table 7-3); ID N is gic@(N-32)."""
    path = _board(
        tmp_path, f'ip block: "Example.Block" (on: 0x43C00000 .. 0x43C00FFF, irq: {line})'
    )
    text = generate.description(boards.load(path), Zynq7000Driver())
    assert f"block: Example.Block @ sysbus <0x43C00000, +0x1000>\n    IRQ -> {gic}" in text


def _board(tmp_path: Path, *lines: str, xsa: str | None = None, rest: str = "") -> Path:
    """A one-device Zynq-7000 board with ``lines`` in its device body."""
    path = tmp_path / "boards" / "z" / "board.jig"
    path.parent.mkdir(parents=True, exist_ok=True)
    arguments = (
        f'xsa: "{xsa}", ps_clk: 50 MHz, ps_version: 3'
        if xsa is not None
        else "clocks: {cpu_3x2x: 1 MHz, cpu_1x: 1 MHz}"
    )
    body = "".join(f"        {line}\n" for line in lines)
    path.write_text(
        'board "z" {\n'
        '    root: "."\n'
        f"    device zynq: zynq7000({arguments}) {{\n"
        '        firmware: {app: "app.elf"}\n'
        f"{body}"
        "    }\n"
        f"{rest}"
        "}\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_pins_and_ip_lines_on_a_board_without_an_xsa(tmp_path: Path) -> None:
    path = tmp_path / "boards" / "z" / "board.jig"
    path.parent.mkdir(parents=True)
    path.write_text(
        'board "z" {\n'
        "    device zynq: zynq7000(clocks: {cpu_3x2x: 1 MHz, cpu_1x: 1 MHz}) {\n"
        '        firmware: {app: "app.elf"}\n'
        '        ip dsp: "Example.Dsp" (on: 0x43C00000 .. 0x43C0FFFF, irq: 2, taps: 16)\n'
        "        ip bram: memory(on: 0x40000000 .. 0x4000FFFF)\n"
        "        unmodelled gem0: 0xE000B000 .. 0xE000BFFF\n"
        "        devc.psVersion = 2\n"
        "    }\n"
        '    peripheral chip: "Sensors.TI_TMP108"\n'
        "    link bus: i2c(controller: zynq.i2c0, targets: {0x48: chip})\n"
        '    link alert: gpio(from: chip.IRQ, to: [zynq.emio[3], zynq.ps_srst_b], pull: "up")\n'
        '    peripheral led: "Miscellaneous.LED"\n'
        "    link light: gpio(from: zynq.mio[7], to: led.gpio[0])\n"
        "}\n",
        encoding="utf-8",
    )
    text = generate.description(boards.load(path), Zynq7000Driver())
    assert (
        "dsp: Example.Dsp @ sysbus <0x43C00000, +0x10000>\n    taps: 16\n    IRQ -> gic@31" in text
    )
    assert "bram: Memory.MappedMemory @ sysbus 0x40000000\n    size: 0x10000" in text
    assert "pl:\n    init add:\n        AddMemory bram" in text
    assert 'Tag <0xE000B000, 0xE000BFFF> "gem0 (unmodelled)" 0 false true' in text
    assert "devc:\n    psVersion: 2" in text
    assert "chip: Sensors.TI_TMP108 @ i2c0 0x48\n    IRQ -> gpio@57 | slcr@1" in text
    assert "gpio:\n    init add:\n        SetPull 57 true" in text  # emio[3] is GPIO pin 57
    assert "led: Miscellaneous.LED @ none" in text
    assert "gpio:\n    7 -> led@0" in text


def test_pins_are_the_familys() -> None:
    driver = Zynq7000Driver()
    device: Any = None
    assert driver.pin(device, PinRef("z", "mio", 53)).input == "gpio@53"
    assert driver.pin(device, PinRef("z", "emio", 0)).output == ("gpio", 54)
    assert driver.pin(device, InternalRef("z", "ps_por_b")).input == "slcr@0"
    assert driver.pin(device, InternalRef("z", "ps_srst_b")).output is None
    with pytest.raises(ConfigError, match="mio has pins 0-53"):
        driver.pin(device, PinRef("z", "mio", 54))
    with pytest.raises(ConfigError, match="is not a Zynq-7000 pin"):
        driver.pin(device, PinRef("z", "pa", 1))


def test_the_block_design_writer_gets_every_ip_line_once_per_machine(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, dict[int, float], list[IpOverride]]] = []

    def pl_section(xsa: Path, fclk_hz: dict[int, float], overrides: list[IpOverride]) -> str:
        calls.append((xsa, dict(fclk_hz), list(overrides)))
        return "axi_gpio_0: Jig.Xilinx.XilinxAxiGpio @ sysbus 0x41200000\n"

    monkeypatch.setattr(blockdesign, "pl_section", pl_section)
    board_path = project / "boards" / "example-zynq" / "board.jig"
    board_path.write_text(
        board_path.read_text(encoding="utf-8").replace(
            "        protocol csp(address: 5)\n",
            "        protocol csp(address: 5)\n"
            '        ip filter: "Example.Dsp.Filter" (on: M00_AXI, irq: filter_intr, taps: 8,\n'
            '                                         mode: ".Fast", gain: 1.5, clock: 100 MHz)\n'
            "        ip window: memory(size: 4 KiB, on: zynq.M01_AXI)\n"
            "        ip dsp: cosim(on: {M02_AXI: 4 KiB, M03_AXI: 8 KiB}, irq: 3,\n"
            '                      build: "cosim/build.sh", sources: "cosim/sources.f",\n'
            '                      library: "libdsp.so", frequency: 20 MHz)\n',
        ),
        encoding="utf-8",
    )
    text = generate.description(boards.find("example-zynq"), Zynq7000Driver())
    assert "axi_gpio_0: Jig.Xilinx.XilinxAxiGpio" in text
    (call,) = calls
    xsa, fclk, overrides = call
    assert xsa == project / "build" / "example.xsa"
    assert set(fclk) <= {0, 1, 2, 3}
    assert overrides == [
        IpOverride(
            "filter",
            "Example.Dsp.Filter",
            {"taps": 8, "mode": Literal(".Fast"), "gain": 1.5, "clock": 100_000_000},
            "M00_AXI",
            "filter_intr",
        ),
        IpOverride("window", Memory(4096), {}, "M01_AXI", None),
        # The build settings stay with the co-simulation, out of the properties.
        IpOverride(
            "dsp", Cosim(), {"frequency": 20_000_000}, {"M02_AXI": 4096, "M03_AXI": 8192}, 3
        ),
        IpOverride("block", "Example.ExampleBlock", {}, AddressRange(0x43C00000, 0x43C00FFF), None),
    ]
    assert "\n// ip lines\n" not in text  # every ip line went to the block design writer


def _xsa_with_design(root: Path) -> Path:
    """An XSA holding ps7_init.tcl and the test block design's handoff."""
    xsa = root / "build" / "design.xsa"
    xsa.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(xsa, "w") as archive:
        archive.write(PS7_INIT, "ps7_init.tcl")
        archive.write(FIXTURES / "hdl" / "example_bd.hwh", "example_bd.hwh")
    return xsa


def test_block_design_names_are_names_of_the_device(tmp_path: Path) -> None:
    """An AXI Quad SPI the design holds is an SPI controller; ip lines name
    exported ports and instances; jig check accepts all of them."""
    _xsa_with_design(tmp_path / "boards" / "z")
    path = _board(
        tmp_path,
        "ip axi_bram_ctrl_0: memory(size: 16 KiB)",
        'ip filter: "Example.Filter" (on: zynq.user_axi, irq: user_irq, bram: axi_bram_ctrl_0,',
        "                             spi: zynq.axi_quad_spi_0, controller: zynq.gic)",
        xsa="build/design.xsa",
        rest='    peripheral clock: "Timers.Clock"\n'
        "    link clock_spi: spi(controller: zynq.axi_quad_spi_0, targets: {0: clock})\n",
    )
    (path.parent / "app.elf").write_bytes(b"\x7fELF")
    board = boards.load(path)
    text = generate.description(board, Zynq7000Driver())
    assert "clock: Timers.Clock @ axi_quad_spi_0 0" in text
    assert "axi_quad_spi_0: Jig.Xilinx.XilinxAxiQuadSpi @ sysbus 0x41E00000" in text
    assert (
        "filter: Example.Filter @ sysbus 0x43C00000\n"
        "    bram: axi_bram_ctrl_0\n"
        "    spi: axi_quad_spi_0\n"
        "    controller: gic\n"
        "    IRQ -> gic@52"
    ) in text
    assert "axi_bram_ctrl_0: Memory.MappedMemory @ sysbus 0x80000000\n    size: 0x4000" in text
    (filter_line,) = [
        o for o in Zynq7000Driver().overrides(board, board.device) if o.name == "filter"
    ]
    assert filter_line.properties == {
        "bram": Reference("axi_bram_ctrl_0"),
        "spi": Reference("axi_quad_spi_0"),
        "controller": Reference("gic"),  # a peripheral of the platform
    }
    assert checks.problems(board) == []
    driver = Zynq7000Driver()
    assert driver.interface_kind(board, board.device, "axi_quad_spi_0") == "spi"
    assert driver.interface_kind(board, board.device, "axi_gpio_0") is None  # no controller
    assert driver.interface_kind(board, board.device, "user_axi") is None  # an exported port
    assert driver.interface_kind(board, board.device, "uart1") == "uart"


def test_names_the_device_lacks_are_errors(tmp_path: Path) -> None:
    _xsa_with_design(tmp_path / "boards" / "z")
    path = _board(
        tmp_path,
        "interface axi_timer_0: cli",
        'ip filter: "Example.Filter" (on: user_axi, bram: axi_bram_ctrl_9)',
        xsa="build/design.xsa",
    )
    board = boards.load(path)
    found = checks.problems(board)
    assert any(
        "interface axi_timer_0: zynq.axi_timer_0 is an instance of design.xsa:example_bd.hwh "
        "(xilinx.com:ip:axi_timer:2.0) that Jig has no model for" in problem
        for problem in found
    ), found
    assert any(
        "ip filter: bram: axi_bram_ctrl_9 is no peripheral of the board or the platform, ip "
        "line or modelled instance of the block design (the board's: axi_bram_ctrl_0, "
        "axi_cdma_0, axi_gpio_0, axi_quad_spi_0, filter)" in p
        for p in found
    ), found
    spi = _board(
        tmp_path,
        xsa="build/design.xsa",
        rest='    peripheral clock: "Timers.Clock"\n'
        "    link clock_spi: spi(controller: zynq.axi_quad_spi_9, targets: {0: clock})\n",
    )
    with pytest.raises(
        ConfigError,
        match=r"link clock_spi: zynq has no interface axi_quad_spi_9 \(has: uart0, .*; the "
        r"block design's axi_bram_ctrl_0, axi_cdma_0, axi_gpio_0, axi_quad_spi_0\)",
    ):
        generate.description(boards.load(spi), Zynq7000Driver())


def test_without_an_xsa_on_takes_an_address_range(tmp_path: Path) -> None:
    path = _board(tmp_path, 'ip filter: "Example.Filter" (on: M00_AXI)')
    with pytest.raises(
        ConfigError,
        match=r"board.jig:\d+:\d+: ip filter: M00_AXI names no port or instance: a device "
        "without an xsa has no block design",
    ):
        generate.description(boards.load(path), Zynq7000Driver())
    sized = _board(tmp_path, "ip ram: memory(on: 0x40000000 .. 0x40000FFF, size: 8 KiB)")
    with pytest.raises(ConfigError, match="size 8192 differs from its range of 4096 bytes"):
        boards.load(sized)
    unsized = _board(tmp_path, "ip ram: memory()")
    with pytest.raises(ConfigError, match=r"memory\(\) needs a size"):
        boards.load(unsized)


def test_register_program_and_bitstream_come_from_the_xsa(project: Path, tmp_path: Path) -> None:
    driver = Zynq7000Driver()
    config = _config(tmp_path)
    program = driver.register_program(config)
    assert program is not None
    lines = program.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# ps7_mio_init_data_3_0"
    assert "P 0xF800010C 0x00000001" in lines
    bitstream = driver.bitstream(config)
    assert bitstream is not None and bitstream.read_bytes() == BITSTREAM


def test_an_xsa_with_two_bitstreams_is_refused(project: Path, tmp_path: Path) -> None:
    with zipfile.ZipFile(project / "build" / "example.xsa", "a") as archive:
        archive.writestr("second.bit", b"\x00")
    with pytest.raises(ConfigError, match="holds 2 bitstreams"):
        Zynq7000Driver().bitstream(_config(tmp_path))


def test_overlay_sets_performance_and_register_maps(project: Path, tmp_path: Path) -> None:
    overlay = Zynq7000Driver().overlay(_config(tmp_path))
    lines = overlay.splitlines()
    assert lines[:2] == ["cpu0:", "    PerformanceInMips: 120"]
    assert "block:" in lines
    (entry,) = [line for line in lines if line.startswith("    registerMap:")]
    assert entry.endswith('/map.json"')


def test_quad_spi_images_need_an_image_builder(tmp_path: Path) -> None:
    driver = Zynq7000Driver()
    config = _config(tmp_path)
    for call in (
        lambda: driver.qspi_image(config),
        lambda: driver.images(config),
        lambda: driver.boot_table(config, {}, {}),
    ):
        with pytest.raises(ConfigError, match="no image builder is registered"):
            call()


def test_an_image_builder_supplies_the_images(tmp_path: Path) -> None:
    flash = tmp_path / "flash.bin"
    app = tmp_path / "app.bin"

    class Builder:
        def qspi_image(self, config: MachineConfig) -> Path:
            return flash

        def images(self, config: MachineConfig) -> dict[str, Path]:
            return {"app": app}

        def boot_table(
            self,
            config: MachineConfig,
            slots: dict[str, dict[int, bytes]],
            first: dict[str, int],
        ) -> bytes:
            return bytes([len(slots), *first.values()])

    builder: ImageBuilder = Builder()
    driver = Zynq7000Driver(image_builder=builder)
    config = _config(tmp_path)
    assert driver.qspi_image(config) == flash
    assert driver.images(config) == {"app": app}
    assert driver.boot_table(config, {"app": {1: b"x"}}, {"app": 1}) == b"\x01\x01"
