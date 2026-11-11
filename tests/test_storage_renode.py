"""Storage on Renode: a composed image attached to a storage model, a
Zynq-7000's OCM loaded at every power-on, its PL block RAM reloaded each
time the PL is configured, an STM32's flash and SRAM partitions with the
firmware running, the ELF-overlap error, and what reading a megabyte of a
device memory costs.

Needs Renode (``pytest -m renode``); the STM32 tests also need an ARM GCC
(ARM_NONE_EABI_GCC or arm-none-eabi-gcc on PATH) for the fixture firmware."""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest

from jig.errors import ConfigError, ToolchainError
from jig.system import boards
from jig.system.socs.stm32 import Stm32Driver
from jig.system.system import Machine, Simulation

from .renode_support import real_renode
from .storage_doubles import make_elf
from .test_blockdesign import SCOPED, SYSDEF, handoff, write_xsa

pytestmark = pytest.mark.renode

KIB = 1024
MIB = 1024 * KIB
FIXTURES = Path(__file__).resolve().parent / "fixtures"
HELLO = FIXTURES / "firmware" / "stm32f4-hello" / "firmware"
FLASH_MODEL = FIXTURES / "models" / "SpiNorFlash.cs"
BRANCH_TO_SELF = b"\xfe\xff\xff\xea"  # ARM: b .


def board_file(directory: Path, text: str) -> Path:
    path = directory / "board" / "board.jig"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def blob(path: Path, data: bytes) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def word(machine: Machine, address: int) -> int:
    return int(machine.execute(f"sysbus ReadDoubleWord {address:#x}").strip(), 0)


def simulation(tmp_path: Path) -> Simulation:
    real_renode()  # skips without one
    return Simulation(name="storage", workdir=tmp_path / "run", step_wall_limit=120.0)


# ------------------------------------------------------ a storage device


def test_a_composed_image_reaches_the_storage_model(tmp_path: Path) -> None:
    path = board_file(
        tmp_path,
        'board "board" {\n'
        '    root: "."\n'
        f'    models: ["{FLASH_MODEL.as_posix()}"]\n'
        "    storage: [flash0]\n"
        '    device zynq: zynq7000(part: "XC7Z020-CLG484", straps: "jtag",\n'
        "                          clocks: {cpu_3x2x: 333.333333 MHz, cpu_1x: 111.111111 MHz}) {\n"
        '        firmware: {app: "build/app.elf"}\n'
        "    }\n"
        '    peripheral flash0: "JigFixtures.SpiNorFlash" (size: 1 MiB)\n'
        "    link boot_flash: qspi(controller: zynq.qspi, targets: {0: flash0})\n"
        "    partition config: raw(on: flash0, at: 64 KiB, size: 4 KiB)\n"
        "}\n",
    )
    config = blob(tmp_path / "config.bin", b"CONFIG")
    with simulation(tmp_path) as sim:
        machine = sim.add_board(
            "dut", boards.load(path), storage={"config": tmp_path / "config.bin"}
        )
        sim.start(boot=False)
        assert machine.execute("qspi.flash0 Peek 0x10000").strip() == "0x43"  # "C"
        assert machine.execute("qspi.flash0 Peek 0x10006").strip() == "0xFF"  # erased
        assert machine.partition_bytes("config") == config + b"\xff" * (4 * KIB - 6)
    image = (tmp_path / "run" / "dut" / "flash0.img").read_bytes()
    assert len(image) == MIB and image[64 * KIB : 64 * KIB + 6] == config


def test_an_stm32_board_attaches_its_storage_on_spi(tmp_path: Path) -> None:
    path = board_file(
        tmp_path,
        'board "board" {\n'
        '    root: "."\n'
        f'    models: ["{FLASH_MODEL.as_posix()}"]\n'
        "    storage: [flash0]\n"
        '    device mcu: stm32(part: "STM32F407VG") {\n'
        '        firmware: "build/app.elf"\n'
        "    }\n"
        '    peripheral flash0: "JigFixtures.SpiNorFlash" (size: 1 MiB)\n'
        "    link spi: spi(controller: mcu.spi1, targets: {0: flash0})\n"
        '    partition config: raw(on: flash0, at: 4 KiB, size: 4 KiB, contents: "c.bin")\n'
        "}\n",
    )
    blob(tmp_path / "board" / "c.bin", b"STM")
    with simulation(tmp_path) as sim:
        machine = sim.add_board("dut", boards.load(path))
        assert "flash0: JigFixtures.SpiNorFlash @ spi1\n" in machine.description()
        sim.start(boot=False)
        assert machine.execute("spi1.flash0 Peek 0x1002").strip() == "0x4D"  # "M"
        assert machine.partition_bytes("config")[:4] == b"STM\xff"


# ------------------------------------------------------ a Zynq-7000's OCM


ZYNQ = """\
board "board" {{
    root: "."
    device zynq: zynq7000(part: "XC7Z020-CLG484", straps: "jtag", ddr: 512 MiB, {design}) {{
        firmware: {{app: "build/app.elf"}}
    }}
    {partition}
}}
"""
NO_DESIGN = "clocks: {cpu_3x2x: 333.333333 MHz, cpu_1x: 111.111111 MHz}"


def zynq_board(tmp_path: Path, partition: str, design: str = NO_DESIGN) -> Path:
    # The application spins at the start of DDR, so the core stays out of OCM.
    make_elf(
        tmp_path / "board" / "build" / "app.elf", [(0x00100000, BRANCH_TO_SELF)], entry=0x00100000
    )
    return board_file(tmp_path, ZYNQ.format(design=design, partition=partition))


def test_ocm_holds_its_partition_at_every_power_on(tmp_path: Path) -> None:
    path = zynq_board(
        tmp_path,
        "partition handoff: raw(on: zynq.ocm, at: 0x2F000, size: 8 KiB,\n"
        '                       contents: "build/handoff.bin")',
    )
    # It crosses from ocmLow (the first 192 KiB) into ocmHigh.
    contents = blob(tmp_path / "board" / "build" / "handoff.bin", bytes(range(256)) * 32)
    with simulation(tmp_path) as sim:
        machine = sim.add_board("dut", boards.load(path))
        sim.start()
        sim.run_for(0.001)
        assert machine.partition_bytes("handoff") == contents
        assert word(machine, 0xFFFF1000) == 0  # past the partition: cleared at power-on
        machine.execute("sysbus WriteDoubleWord 0xFFFF0000 0xDEADBEEF")  # firmware writes
        assert machine.partition_bytes("handoff")[0x1000:0x1004] == bytes.fromhex("efbeadde")
        machine.reset()  # a system reset clears OCM and loads nothing: not a power-on
        sim.run_for(0.001)  # the machine resets when time next runs
        assert machine.partition_bytes("handoff") == bytes(8 * KIB)
        sim.run(machine.power_cycle(0.001))
        assert machine.partition_bytes("handoff") == contents
        log = machine.execute("bootrom BootLog")
        assert "OCM loaded at power-on: 1 region(s)" in log


# ------------------------------------------------- a Zynq-7000's block RAM


def test_block_ram_takes_its_partition_each_time_the_pl_is_configured(tmp_path: Path) -> None:
    write_xsa(
        tmp_path / "board" / "build" / "example.xsa",
        {"sysdef.xml": SYSDEF, "example_bd.hwh": handoff(), "scoped.hwh": SCOPED},
    )
    path = zynq_board(
        tmp_path,
        "partition table: raw(on: zynq.axi_bram_ctrl_0, at: 0x100, size: 256 B, "
        'contents: "build/table.bin")',
        design='xsa: "build/example.xsa", ps_clk: 50 MHz, ps_version: 3',
    )
    contents = blob(tmp_path / "board" / "build" / "table.bin", bytes(range(256)))
    with simulation(tmp_path) as sim:
        machine = sim.add_board("dut", boards.load(path))
        sim.start()  # the JTAG boot configures the PL with the XSA's bitstream
        assert machine.partition_bytes("table") == contents
        assert word(machine, 0x80000000) == 0  # before the partition: no initial value
        machine.execute("sysbus WriteDoubleWord 0x80000100 0xDEADBEEF")
        # PROG_B high, then low: the PL loses its configuration and block RAM with it.
        machine.execute("sysbus WriteDoubleWord 0xF8007000 0x4C006000")
        machine.execute("sysbus WriteDoubleWord 0xF8007000 0x0C006000")
        assert machine.partition_bytes("table") == bytes(256)
        bitstream = machine.driver.bitstream(machine.config)  # type: ignore[attr-defined]
        machine.execute(f"devc ConfigureFromJtag @{bitstream.as_posix()}")
        assert machine.partition_bytes("table") == contents


# ------------------------------------------------- an STM32's own memories


@pytest.fixture
def arm_gcc() -> str:
    try:
        return Stm32Driver.compiler()
    except (ConfigError, ToolchainError) as exc:
        pytest.skip(f"no ARM GCC to build the STM32 firmware: {exc}")


STM32 = """\
board "board" {{
    root: "."
    device mcu: stm32(part: "STM32F407VG", mips: 16) {{
        firmware: build("{firmware}/build.py", sources: "{firmware}", output: "firmware.elf")
        interface usart2: cli
    }}
    external link console: uart(baud: 115200, members: [mcu.usart2])
{partitions}
}}
"""


def stm32_board(tmp_path: Path, partitions: str) -> Path:
    return board_file(tmp_path, STM32.format(firmware=HELLO.as_posix(), partitions=partitions))


def read_seconds(machine: Machine, name: str, count: int = 9) -> float:
    """The median wall time of reading a partition, in seconds."""
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        machine.partition_bytes(name)
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def test_stm32_flash_keeps_and_sram_restores_its_partitions(arm_gcc: str, tmp_path: Path) -> None:
    path = stm32_board(
        tmp_path,
        "    partition config: raw(on: mcu.flash, at: 0x80000, size: 64 KiB,\n"
        '                           contents: "config.bin")\n'
        "    partition scratch: raw(on: mcu.sram, at: 0x10000, size: 4 KiB,\n"
        '                            contents: "scratch.bin")\n'
        "    partition big: raw(on: mcu.flash, at: 1 MiB, size: 1 MiB)\n",
    )
    config = blob(tmp_path / "board" / "config.bin", b"CONFIG")
    scratch = blob(tmp_path / "board" / "scratch.bin", b"SCRATCH")
    with simulation(tmp_path) as sim:
        machine = sim.add_board("dut", boards.load(path))
        sim.start()
        sim.run(machine.console.expect(r"hello from stm32f4\r\n", timeout=0.1))
        assert machine.partition_bytes("config") == config + b"\xff" * (64 * KIB - 6)
        assert machine.partition_bytes("scratch") == scratch + bytes(4 * KIB - 7)
        assert len(machine.partition_bytes("big")) == MIB
        seconds = read_seconds(machine, "big")
        print(f"reading 1 MiB of mcu.flash: {seconds * 1000:.1f} ms (median of 9)")  # pytest -s
        assert seconds < 2.0
        machine.execute("sysbus WriteDoubleWord 0x08080000 0x11111111")
        machine.execute("sysbus WriteDoubleWord 0x20010000 0x22222222")
        sim.run(machine.power_cycle(0.01))
        assert machine.partition_bytes("config")[:4] == b"\x11" * 4  # flash keeps it
        assert machine.partition_bytes("scratch")[:7] == scratch  # SRAM as at power-on
        machine.console.clear()
        sim.run(machine.console.expect(r"hello from stm32f4\r\n", timeout=0.1))


def test_an_elf_segment_over_a_flash_partition_stops_the_boot(arm_gcc: str, tmp_path: Path) -> None:
    path = stm32_board(tmp_path, "    partition vectors: raw(on: mcu.flash, at: 0, size: 1 KiB)\n")
    with simulation(tmp_path) as sim:
        sim.add_board("dut", boards.load(path))
        with pytest.raises(
            ConfigError,
            match=r"^dut: firmware.elf's segment at 0x08000000-0x[0-9A-F]{8} lands on partition "
            r"vectors on mcu.flash \(0x08000000-0x080003FF\)",
        ):
            sim.start()


# ------------------------------------------------- paths that hold spaces
#
# Renode's monitor ends an @path token at the first space, so Jig quotes each
# file it names (jig.renode.session.monitor_path). Here the run directory,
# Jig's cache, the board, its firmware and the partitions' contents all sit
# under a directory with spaces in its name.


def test_a_zynq_run_whose_paths_hold_spaces_loads_every_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_renode()
    base = tmp_path / "a dir with spaces"
    monkeypatch.setenv("JIG_CACHE_DIR", str(base / "jig cache"))  # register program, bitstream
    write_xsa(
        base / "board" / "build" / "the design.xsa",
        {"sysdef.xml": SYSDEF, "example_bd.hwh": handoff(), "scoped.hwh": SCOPED},
    )
    make_elf(
        base / "board" / "build" / "the app.elf",
        [(0x00100000, BRANCH_TO_SELF)],
        entry=0x00100000,
    )
    models = base / "board" / "fixture models"
    models.mkdir()
    (models / FLASH_MODEL.name).write_bytes(FLASH_MODEL.read_bytes())
    path = board_file(
        base,
        'board "board" {\n'
        '    root: "."\n'
        '    models: ["fixture models/*.cs"]\n'
        "    storage: [flash0]\n"
        '    device zynq: zynq7000(part: "XC7Z020-CLG484", straps: "jtag", ddr: 512 MiB,\n'
        '                          xsa: "build/the design.xsa", ps_clk: 50 MHz, ps_version: 3) {\n'
        '        firmware: {app: "build/the app.elf"}\n'
        "    }\n"
        '    peripheral flash0: "JigFixtures.SpiNorFlash" (size: 1 MiB)\n'
        "    link boot_flash: qspi(controller: zynq.qspi, targets: {0: flash0})\n"
        "    partition config: raw(on: flash0, at: 64 KiB, size: 4 KiB,\n"
        '                          contents: "data/a config.bin")\n'
        "    partition handoff: raw(on: zynq.ocm, at: 0x2F000, size: 8 KiB,\n"
        '                           contents: "data/a handoff.bin")\n'
        "    partition table: raw(on: zynq.axi_bram_ctrl_0, at: 0x100, size: 256 B,\n"
        '                         contents: "data/a table.bin")\n'
        "}\n",
    )
    data = base / "board" / "data"
    config = blob(data / "a config.bin", b"CONFIG")
    handoff_bytes = blob(data / "a handoff.bin", bytes(range(256)) * 32)
    table = blob(data / "a table.bin", bytes(range(255, -1, -1)))
    with Simulation(name="spaces", workdir=base / "run dir", step_wall_limit=120.0) as sim:
        machine = sim.add_board("dut", boards.load(path))
        assert " " in machine.config.workdir.as_posix()
        sim.start()  # LoadPlatformDescription, AttachImage, OCM and block RAM, the JTAG boot
        sim.run_for(0.001)
        assert machine.execute("qspi.flash0 Peek 0x10000").strip() == "0x43"  # "C"
        assert machine.partition_bytes("handoff") == handoff_bytes
        assert machine.partition_bytes("table") == table
        assert word(machine, 0x00100000) == int.from_bytes(BRANCH_TO_SELF, "little")
    image = (base / "run dir" / "dut" / "flash0.img").read_bytes()
    assert image[64 * KIB : 64 * KIB + 6] == config


def test_an_stm32_run_whose_paths_hold_spaces_loads_its_elf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_renode()
    base = tmp_path / "a dir with spaces"
    monkeypatch.setenv("JIG_CACHE_DIR", str(base / "jig cache"))
    # The vector table (stack pointer, reset handler at 0x08000008 in Thumb
    # state), then the reset handler: b . (0xE7FE).
    make_elf(
        base / "board" / "build" / "the app.elf",
        [(0x08000000, (0x20001000).to_bytes(4, "little") + (0x08000009).to_bytes(4, "little"))],
    )
    blob(base / "board" / "build" / "spin.bin", b"\xfe\xe7")
    path = board_file(
        base,
        'board "board" {\n'
        '    root: "."\n'
        '    device mcu: stm32(part: "STM32F407VG") {\n'
        '        firmware: "build/the app.elf"\n'
        "    }\n"
        '    partition code: raw(on: mcu.flash, at: 8, size: 8, contents: "build/spin.bin")\n'
        "    partition scratch: raw(on: mcu.sram, at: 0x10000, size: 256 B,\n"
        '                           contents: "data/a scratch.bin")\n'
        "}\n",
    )
    scratch = blob(base / "board" / "data" / "a scratch.bin", b"SCRATCH")
    with Simulation(name="spaces", workdir=base / "run dir", step_wall_limit=120.0) as sim:
        machine = sim.add_board("dut", boards.load(path))
        sim.start()  # LoadELF ... false, then the core starts from the vector table
        sim.run_for(0.001)
        assert machine.partition_bytes("scratch") == scratch + bytes(256 - 7)
        assert word(machine, 0x08000000) == 0x20001000
        assert machine.partition_bytes("code")[:2] == b"\xfe\xe7"
