"""The Zynq-7000 driver without Renode: board checks against the XSA, generated
artifacts, the platform overlay and the image builder hook."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from jig.errors import ConfigError
from jig.ext.zynq7000.driver import STRAPS, ImageBuilder, Zynq7000Driver
from jig.system import boards
from jig.system.socs import MachineConfig, driver_for

from .conftest import BITSTREAM


def _config(tmp_path: Path, options: dict[str, str] | None = None) -> MachineConfig:
    board = boards.find("example-zynq")
    return MachineConfig(
        name="dut",
        board=board,
        boot="jtag",
        performance_mips=board.performance_mips,
        workdir=tmp_path / "dut",
        options=board.resolve_options(options),
    )


def test_the_registry_finds_the_driver() -> None:
    driver = driver_for("zynq7000")
    assert isinstance(driver, Zynq7000Driver)
    assert driver.boot_modes == ("jtag", "qspi")
    assert STRAPS["qspi"] == 1
    with pytest.raises(ConfigError, match="no SoC driver for 'z80'"):
        driver_for("z80")


def test_board_clocks_match_the_xsa(project: Path) -> None:
    board = boards.find("example-zynq")
    problems = driver_for(board.soc).check(board)
    # The fixture ships no firmware, so only that is reported.
    assert [p for p in problems if not p.startswith("firmware ")] == []
    assert any(p.startswith("firmware app:") for p in problems)


def test_a_clock_that_differs_from_the_xsa_is_reported(project: Path) -> None:
    repl = project / "boards" / "example-zynq" / "example-zynq.repl"
    repl.write_text(
        repl.read_text(encoding="utf-8").replace("50000000", "48000000"), encoding="utf-8"
    )
    board = boards.find("example-zynq")
    problems = driver_for(board.soc).check(board)
    assert any(p.startswith("uart1.clockFrequency: 48000000") for p in problems)


def test_a_missing_xsa_is_reported() -> None:
    board = boards.find("example-zynq")  # tests/fixtures holds no build/example.xsa
    (problem,) = driver_for(board.soc).check(board)
    assert "XSA" in problem and "not found" in problem


def test_register_program_and_bitstream_come_from_the_xsa(project: Path, tmp_path: Path) -> None:
    driver = Zynq7000Driver()
    config = _config(tmp_path)
    program = driver.register_program(config).read_text(encoding="utf-8").splitlines()
    assert program[0] == "# ps7_mio_init_data_3_0"
    assert "P 0xF800010C 0x00000001" in program
    assert driver.bitstream(config).read_bytes() == BITSTREAM


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
