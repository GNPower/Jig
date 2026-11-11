"""Storage in a run, without Renode: a recording session stands in for it.
Every family attaches its storage devices' composed images; the families
write what their own memories receive when the memory's kind says; an
STM32 ELF segment over a partition in flash stops the boot; a system's
storage map reaches the machine with its paths relative to the plan."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jig.errors import ConfigError
from jig.lang.parser import PlanError
from jig.storage.memory import PL_BLOCK_RAM, VOLATILE
from jig.system import system
from jig.system.renode_shim import RenodeInstall
from jig.system.system import Simulation
from jig.testing import FakeSimulation, run_source

from .storage_doubles import make_elf
from .test_blockdesign import SCOPED, SYSDEF, handoff, write_xsa
from .test_partitions import STM32, board_with, write_board
from .test_simulation import RecordingSession

KIB = 1024
FLASH_REPLY = "found JigFixtures.SpiNorFlash IStorage=1048576 ErasedValue=255 IPowerCycled\n"


class Session(RecordingSession):
    """A recording session that keeps each IronPython request whole."""

    contracts_reply = FLASH_REPLY

    def python(self, code: str) -> str:
        self.commands.append(f"(python) {code}")
        return ""


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Session]:
    opened: list[Session] = []

    def find(**_settings: Any) -> RenodeInstall:
        return RenodeInstall(tmp_path / "renode", tmp_path, "1.17.0", "test", "net8")

    def open_session(**settings: Any) -> Session:
        session = Session(**settings)
        opened.append(session)
        return session

    monkeypatch.setattr(system, "find_renode", find)
    monkeypatch.setattr(system, "open_session", open_session)
    return opened


def simulation(tmp_path: Path) -> Simulation:
    return Simulation(name="run", workdir=tmp_path / "run")


def written(file: str, commands: list[str]) -> list[str]:
    """The IronPython writes that load ``file`` (a region file's name)."""
    return [c for c in commands if c.startswith("(python)") and file in c]


# ------------------------------------------------------- storage devices


def test_an_stm32_board_attaches_its_storage(tmp_path: Path, sessions: list[Session]) -> None:
    with simulation(tmp_path) as sim:
        sim.add_board("dut", board_with(tmp_path))
        sim.start(boot=False)
    (session,) = sessions
    image = tmp_path / "run" / "dut" / "flash0.img"
    assert f'spi1.flash0 AttachImage "{image.as_posix()}"' in session.commands
    # Nothing writes the flash: its model keeps its contents and makes the file.
    assert not any("JigContracts" in command for command in session.commands)
    assert not image.exists()


def test_a_composed_image_takes_the_models_erased_value(
    tmp_path: Path, sessions: list[Session]
) -> None:
    board = board_with(tmp_path, "partition config: raw(on: flash0, at: 4 KiB, size: 4 KiB)")
    config = tmp_path / "c.bin"
    config.write_bytes(b"CFG")
    Session.contracts_reply = FLASH_REPLY.replace("ErasedValue=255", "ErasedValue=0")
    try:
        with simulation(tmp_path) as sim:
            sim.add_board("dut", board, storage={"config": config})
            sim.start(boot=False)
    finally:
        Session.contracts_reply = FLASH_REPLY
    (session,) = sessions
    image = (tmp_path / "run" / "dut" / "flash0.img").read_bytes()
    assert len(image) == 1024 * KIB
    assert image[4 * KIB : 4 * KIB + 4] == b"CFG\x00" and image.count(0) == len(image) - 3
    query = session.commands.index('machine JigContracts "spi1.flash0"')
    attach = next(i for i, c in enumerate(session.commands) if "AttachImage" in c)
    assert query < attach


def test_a_storage_model_without_the_contract_stops_the_build(
    tmp_path: Path, sessions: list[Session]
) -> None:
    board = board_with(tmp_path, "partition config: raw(on: flash0, at: 0, size: 4 KiB)")
    config = tmp_path / "c.bin"
    config.write_bytes(b"CFG")
    Session.contracts_reply = "found Memory.MappedMemory\n"
    try:
        with simulation(tmp_path) as sim:
            sim.add_board("dut", board, storage={"config": config})
            with pytest.raises(
                ConfigError,
                match=r"dut: storage flash0: Memory.MappedMemory does not implement IStorage",
            ):
                sim.start(boot=False)
    finally:
        Session.contracts_reply = FLASH_REPLY


def test_missing_sources_stop_the_run_before_renode_starts(
    tmp_path: Path, sessions: list[Session]
) -> None:
    board = board_with(
        tmp_path,
        'partition config: raw(on: flash0, at: 0, size: 4 KiB, contents: "build/config.bin")\n'
        "partition log: raw(on: flash0, at: 4 KiB, size: 4 KiB)\n",
    )
    missing = tmp_path / "log.bin"
    with simulation(tmp_path) as sim:
        sim.add_board("dut", board, storage={"log": missing, "flash0": {0x8000: missing}})
        with pytest.raises(ConfigError) as caught:
            sim.start(boot=False)
    assert sessions == []
    contents = (tmp_path / "store" / "build" / "config.bin").resolve()
    assert str(caught.value).splitlines()[1:] == [
        f"    dut: contents of partition log {missing} not found",
        f"    dut: file for flash0 at 0x8000 {missing} not found",
        f"    dut: contents of partition config {contents} not found",
    ]


def test_a_board_rejects_a_storage_map_it_has_no_place_for(
    tmp_path: Path, sessions: list[Session]
) -> None:
    with (
        simulation(tmp_path) as sim,
        pytest.raises(
            ConfigError,
            match=r"^dut: board store has no partition, storage device or device memory 'nope'",
        ),
    ):
        sim.add_board("dut", board_with(tmp_path), storage={"nope": tmp_path / "x.bin"})


# --------------------------------------------------- an STM32's memories


def stm32_with_firmware(
    tmp_path: Path, members: str, segment: int = 0x08000000, memory_size: int | None = None
) -> Any:
    sizes = None if memory_size is None else {segment: memory_size}
    make_elf(tmp_path / "store" / "build" / "app.elf", [(segment, bytes(8))], memory_sizes=sizes)
    return board_with(tmp_path, members)


def test_stm32_flash_is_written_once_and_sram_at_every_power_on(
    tmp_path: Path, sessions: list[Session]
) -> None:
    board = stm32_with_firmware(
        tmp_path,
        "partition table: raw(on: mcu.flash, at: 0x80000, size: 4 KiB)\n"
        "partition scratch: raw(on: mcu.sram, at: 0x1000, size: 1 KiB)\n",
    )
    table, scratch = tmp_path / "t.bin", tmp_path / "s.bin"
    table.write_bytes(b"TABLE")
    scratch.write_bytes(b"SCRATCH")
    with simulation(tmp_path) as sim:
        machine = sim.add_board("dut", board, storage={"table": table, "scratch": scratch})
        sim.start()
        (session,) = sessions
        commands = session.commands
        flash = written("mcu.flash-0.bin", commands)
        sram = written("mcu.sram-0.bin", commands)
        assert flash == [
            "(python) (lambda data: self.Machine['sysbus.flash'].WriteBytes(524288, data, 0, "
            "4096))(__import__('System').IO.File.ReadAllBytes("
            f"'{(tmp_path / 'run' / 'dut' / 'memories' / 'mcu.flash-0.bin').as_posix()}'))"
        ]
        load = next(i for i, c in enumerate(commands) if c.startswith("sysbus LoadELF"))
        assert commands.index(flash[0]) < commands.index(sram[0]) < load
        assert machine.config.memory_images["sram"].memory.kind == VOLATILE
        region = (tmp_path / "run" / "dut" / "memories" / "mcu.sram-0.bin").read_bytes()
        assert region == b"SCRATCH" + bytes(1024 - 7)  # SRAM's fill is 0
        machine.power_off()
        machine.power_on()
        assert len(written("mcu.sram-0.bin", commands)) == 2
        assert len(written("mcu.flash-0.bin", commands)) == 1
        reset = max(i for i, c in enumerate(commands) if c == "machine Reset")
        last = max(i for i, c in enumerate(commands) if c == sram[0])
        assert last > reset
        machine.boot()  # the boot flow again is not a power-on
        assert len(written("mcu.sram-0.bin", commands)) == 2


def test_an_elf_segment_over_a_partition_in_flash_stops_the_boot(
    tmp_path: Path, sessions: list[Session]
) -> None:
    board = stm32_with_firmware(tmp_path, "partition table: raw(on: mcu.flash, at: 0, size: 4 KiB)")
    with simulation(tmp_path) as sim:
        sim.add_board("dut", board)
        with pytest.raises(
            ConfigError,
            match=r"^dut: app.elf's segment at 0x08000000-0x08000007 lands on partition table "
            r"on mcu.flash \(0x08000000-0x08000FFF\)$",
        ):
            sim.start()


def test_an_elf_segment_over_a_file_placed_in_flash_stops_the_boot(
    tmp_path: Path, sessions: list[Session]
) -> None:
    board = stm32_with_firmware(tmp_path, "", segment=0x08010000)
    word = tmp_path / "word.bin"
    word.write_bytes(b"\x01\x02\x03\x04")
    with simulation(tmp_path) as sim:
        sim.add_board("dut", board, storage={"mcu.flash": {0x10002: word}})
        with pytest.raises(
            ConfigError,
            match=r"lands on word.bin at 0x10002 on mcu.flash \(0x08010002-0x08010005\)",
        ):
            sim.start()


def test_the_zeros_loadelf_writes_after_a_segment_count_as_written(
    tmp_path: Path, sessions: list[Session]
) -> None:
    # Renode's LoadELF writes p_memsz bytes: the file's 8, then zeros up to 8 KiB.
    board = stm32_with_firmware(
        tmp_path, "partition table: raw(on: mcu.flash, at: 4 KiB, size: 4 KiB)", memory_size=0x2000
    )
    with simulation(tmp_path) as sim:
        sim.add_board("dut", board)
        with pytest.raises(
            ConfigError,
            match=r"^dut: app.elf's segment at 0x08000000-0x08001FFF \(8 bytes from the file, "
            r"then 8184 zero bytes Renode's LoadELF writes\) lands on partition table on "
            r"mcu.flash \(0x08001000-0x08001FFF\)$",
        ):
            sim.start()


def test_load_segments_reads_elf32_and_elf64(tmp_path: Path) -> None:
    from jig.system.socs.stm32 import load_segments

    narrow = make_elf(
        tmp_path / "a.elf",
        [(0x08000000, b"ab"), (0x20000000, b""), (0x08000002, b"")],
        memory_sizes={0x08000002: 16},
    )
    # An empty segment writes nothing; one with no bytes in the file but a
    # size in memory, as a .bss has, writes that many zeros.
    assert load_segments(narrow) == [(0x08000000, 2, 2), (0x08000002, 0, 16)]
    wide = make_elf(
        tmp_path / "b.elf", [(0x1_0000_0000, b"abc")], wide=True, memory_sizes={0x1_0000_0000: 8}
    )
    assert load_segments(wide) == [(0x1_0000_0000, 3, 8)]
    (tmp_path / "c.elf").write_bytes(b"not an elf")
    with pytest.raises(ConfigError, match=r"is not an ELF file"):
        load_segments(tmp_path / "c.elf")


# -------------------------------------------------- a Zynq-7000's memories

ZYNQ = """\
board "store" {{
    root: "."
{settings}
    device zynq: zynq7000(part: "XC7Z020-CLG484", xsa: "build/example.xsa", ps_clk: 50 MHz,
                          ps_version: 3, straps: "jtag") {{
        firmware: {{app: "build/app.elf"}}
    }}
{members}
}}
"""


def test_zynq_ocm_and_block_ram_contents_go_to_their_models(
    tmp_path: Path, sessions: list[Session]
) -> None:
    write_xsa(
        tmp_path / "store" / "build" / "example.xsa",
        {"sysdef.xml": SYSDEF, "example_bd.hwh": handoff(), "scoped.hwh": SCOPED},
    )
    path = write_board(
        tmp_path,
        "partition handoff: raw(on: zynq.ocm, at: 0x2F000, size: 8 KiB)\n"
        "partition table: raw(on: zynq.axi_bram_ctrl_0, at: 0x100, size: 256 B)\n",
        template=ZYNQ,
    )
    from jig.system import boards

    board = boards.load(path)
    handoff_file, table = tmp_path / "h.bin", tmp_path / "t.bin"
    handoff_file.write_bytes(b"H" * 8 * KIB)
    table.write_bytes(b"T" * 16)
    with simulation(tmp_path) as sim:
        machine = sim.add_board("dut", board, storage={"handoff": handoff_file, "table": table})
        memories = machine.layout.targets
        assert memories["zynq.axi_bram_ctrl_0"].memory is not None
        assert memories["zynq.axi_bram_ctrl_0"].memory.kind == PL_BLOCK_RAM
        assert (memories["zynq.ocm"].size, memories["zynq.axi_bram_ctrl_0"].size) == (
            0x40000,
            0x2000,
        )
        sim.start(boot=False)
    (session,) = sessions
    regions = tmp_path / "run" / "dut" / "memories"
    assert (
        f'bootrom AddPowerOnContents 0x2F000 "{(regions / "zynq.ocm-0.bin").as_posix()}"'
        in session.commands
    )
    assert (
        "pl AddInitialContents sysbus.axi_bram_ctrl_0 0x100 "
        f'"{(regions / "zynq.axi_bram_ctrl_0-0.bin").as_posix()}"' in session.commands
    )


def test_a_qspi_boot_needs_no_hook_when_partitions_fill_the_flash(
    tmp_path: Path, sessions: list[Session]
) -> None:
    from jig.ext.zynq7000.driver import Zynq7000Driver

    boot = tmp_path / "store" / "build" / "BOOT.bin"
    boot.parent.mkdir(parents=True)
    boot.write_bytes(b"BOOTIMAGE")
    template = STM32.replace(
        'device mcu: stm32(part: "STM32F407VG") {{\n        firmware: "build/app.elf"\n    }}',
        'device mcu: zynq7000(part: "XC7Z020-CLG484", clocks: {{cpu_3x2x: 1 MHz, '
        "cpu_1x: 1 MHz}}) {{\n"
        '        firmware: {{app: "build/app.elf"}}\n        boot qspi: {{storage: flash0}}\n'
        "    }}",
    ).replace("mcu.spi1", "mcu.qspi")
    path = write_board(
        tmp_path,
        'partition boot: raw(on: flash0, at: 0, size: 64 KiB, contents: "build/BOOT.bin")',
        template=template,
    )
    from jig.system import boards

    board = boards.load(path)
    with simulation(tmp_path) as sim:
        machine = sim.add_board("dut", board, boot="qspi")
        assert isinstance(machine.driver, Zynq7000Driver)
        assert machine.driver.base_images(machine.config) == {}
        sim.start(boot=False)
    image = (tmp_path / "run" / "dut" / "flash0.img").read_bytes()
    assert image[:9] == b"BOOTIMAGE"


def test_every_file_a_run_names_reaches_the_monitor_in_quotes(
    tmp_path: Path, sessions: list[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The monitor ends an @path token at a space: each file goes in quotes."""
    base = tmp_path / "a dir with spaces"
    monkeypatch.setenv("JIG_CACHE_DIR", str(base / "jig cache"))  # the register program
    write_xsa(
        base / "store" / "build" / "example.xsa",
        {"sysdef.xml": SYSDEF, "example_bd.hwh": handoff(), "scoped.hwh": SCOPED},
    )
    make_elf(base / "store" / "build" / "app.elf", [(0x00100000, b"\xfe\xff\xff\xea")])
    path = write_board(
        base,
        'peripheral flash0: "JigFixtures.SpiNorFlash" (size: 1 MiB)\n'
        "link boot_flash: qspi(controller: zynq.qspi, targets: {0: flash0})\n"
        "partition config: raw(on: flash0, at: 64 KiB, size: 4 KiB)\n"
        "partition handoff: raw(on: zynq.ocm, at: 0x2F000, size: 8 KiB)\n"
        "partition table: raw(on: zynq.axi_bram_ctrl_0, at: 0x100, size: 256 B)\n",
        settings="storage: [flash0]",
        template=ZYNQ,
    )
    contents = base / "contents dir"
    contents.mkdir()
    for name in ("config", "handoff", "table"):
        (contents / f"the {name}.bin").write_bytes(name.encode())
    from jig.system import boards

    with Simulation(name="run", workdir=base / "run dir") as sim:
        sources = {name: contents / f"the {name}.bin" for name in ("config", "handoff", "table")}
        sim.add_board("dut", boards.load(path), storage=sources)
        sim.start()  # a JTAG boot: the register program, the bitstream, the ELF
    (session,) = sessions
    spaced = base.as_posix()
    named = [c for c in session.commands if spaced in c]
    assert {c.split()[1] for c in named} == {
        "LoadPlatformDescription",
        "AttachImage",
        "AddPowerOnContents",
        "AddInitialContents",
        "RunRegisterProgram",
        "ConfigureFromJtag",
        "LoadELF",
    }
    for command in named:
        assert f' "{spaced}/' in command and command.count('"') == 2, command


# --------------------------------------------- the system's storage map


def plan_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / "boards").mkdir(parents=True)
    (root / "jig.toml").write_text('[project]\nname = "p"\n', encoding="utf-8", newline="\n")
    write_board(root / "boards")
    monkeypatch.setenv("JIG_PROJECT", str(root))
    return root


def test_a_systems_storage_map_is_relative_to_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = plan_project(tmp_path, monkeypatch)
    plans = root / "plans"
    plans.mkdir()
    fake = FakeSimulation(workdir=tmp_path / "out")
    report = run_source(
        plans,
        "system {\n"
        '    board a: "store" (storage: {flash0: {0: "boot.bin", 0x100: "../app.bin"},\n'
        '                                "mcu.flash": "images/flash.bin"})\n'
        "}\n"
        'test "t" { check true }\n',
        simulation=fake,
    )
    assert report.cases[0].status == "PASS", report.cases[0].detail
    config = fake.machines["a"].config
    assert config.storage_sources == {
        "flash0": {0: plans / "boot.bin", 0x100: root / "app.bin"},
        "mcu.flash": plans / "images" / "flash.bin",
    }
    assert config.storage_images == {}  # no file for a whole storage device


@pytest.mark.parametrize(
    ("storage", "message"),
    [
        ("3", r"storage is a map, such as"),
        ("{flash0: 3}", r"storage flash0: a file or folder, or a map from offset to file, not 3"),
        ('{flash0: {"a": "x.bin"}}', r"storage flash0: 'a' is not an offset in bytes"),
        ("{flash0: {0: 3}}", r"storage flash0: at 0x0 names no file: 3"),
        ('{nope: "x.bin"}', r"no partition, storage device or device memory 'nope'"),
    ],
)
def test_a_bad_storage_map_names_its_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: str, message: str
) -> None:
    plan_project(tmp_path, monkeypatch)
    with pytest.raises(PlanError, match=message):
        run_source(
            tmp_path,
            f'system {{ board a: "store" (storage: {storage}) }}\ntest "t" {{ check true }}\n',
            simulation=FakeSimulation(workdir=tmp_path / "out"),
        )


def test_jig_check_contracts_reports_what_building_the_storage_finds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sessions: list[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jig.cli import main

    root = plan_project(tmp_path, monkeypatch)
    path = root / "boards" / "store" / "board.jig"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    link spi:",
            '    partition config: raw(on: flash0, at: 0, size: 4 KiB, contents: "c.bin")\n'
            "    link spi:",
        ),
        encoding="utf-8",
        newline="\n",
    )
    (path.parent / "c.bin").write_bytes(b"CFG")
    make_elf(path.parent / "build" / "app.elf", [(0x08000000, bytes(8))])
    monkeypatch.setenv("JIG_OUTPUT", str(tmp_path / "out"))
    Session.contracts_reply = "found Memory.MappedMemory\n"
    try:
        assert main(["-q", "check", "--contracts", "store"]) == 1
    finally:
        Session.contracts_reply = FLASH_REPLY
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "store: 1 problem(s)",
        "  dut: storage flash0: Memory.MappedMemory does not implement IStorage "
        "(the harness calls AttachImage)",
    ]
    assert sessions[0].stopped


@pytest.mark.parametrize(
    ("part", "expected"),
    [
        ("STM32F407VG", {"flash": (0x08000000, 0x200000), "sram": (0x20000000, 0x20000)}),
        (
            "STM32U575ZI",
            {
                "flash": (0x08000000, 0x200000),
                "sram1": (0x20000000, 0x30000),
                "sram2": (0x20030000, 0x10000),
                "sram3": (0x20040000, 0x80000),
                "sram4": (0x28000000, 0x4000),
            },
        ),
        (
            "STM32U595ZJ",
            {
                "flash": (0x08000000, 0x400000),
                "sram1": (0x20000000, 0xC0000),
                "sram2": (0x200C0000, 0x10000),
                "sram3": (0x200D0000, 0xD0000),
                "sram4": (0x28000000, 0x4000),
                "sram5": (0x201A0000, 0xD0000),
            },
        ),
    ],
)
def test_an_stm32_names_the_memories_its_platform_maps(
    tmp_path: Path, part: str, expected: dict[str, tuple[int, int]]
) -> None:
    from jig.storage.memory import NON_VOLATILE
    from jig.system import boards
    from jig.system.socs.stm32 import Stm32Driver

    path = write_board(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace("STM32F407VG", part),
        encoding="utf-8",
        newline="\n",
    )
    board = boards.load(path)
    memories = Stm32Driver().memories(board)
    assert {name: (m.address, m.size) for name, m in memories.items()} == expected
    assert memories["flash"].kind == NON_VOLATILE and memories["flash"].fill == 0xFF
    assert all(m.kind == VOLATILE and m.fill == 0 for n, m in memories.items() if n != "flash")
