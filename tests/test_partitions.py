"""Partitions in board files and the system's storage map, resolved against
a board (``jig.storage.layout``): what each declaration and key must be,
and the problems found before any byte is read."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jig.errors import ConfigError
from jig.storage import formats
from jig.storage.layout import Layout, for_board, resolve, sources
from jig.system import boards, registry
from jig.system.boards import Board
from jig.system.model import InternalRef, PeripheralRef
from jig.system.socs import driver_for

from .storage_doubles import Listing

STM32 = """\
board "store" {{
    root: "."
    storage: [flash0]
{settings}
    device mcu: stm32(part: "STM32F407VG") {{
        firmware: "build/app.elf"
    }}
    peripheral flash0: "JigFixtures.SpiNorFlash" (size: 1 MiB)
    link spi: spi(controller: mcu.spi1, targets: {{0: flash0}})
{members}
}}
"""

PLUGIN = """\
from tests.storage_doubles import Listing

LISTING = Listing()


def register(api):
    api.storage_format("listing", LISTING)
"""


def write_board(
    directory: Path, members: str = "", *, settings: str = "", template: str = STM32
) -> Path:
    path = directory / "store" / "board.jig"
    path.parent.mkdir(parents=True, exist_ok=True)
    indent = lambda text: "\n".join(f"    {line}" if line else "" for line in text.splitlines())  # noqa: E731
    path.write_text(
        template.format(members=indent(members), settings=indent(settings)),
        encoding="utf-8",
        newline="\n",
    )
    return path


def board_with(directory: Path, members: str = "", *, plugin: bool = False) -> Board:
    settings = ""
    if plugin:
        (directory / "store").mkdir(parents=True, exist_ok=True)
        (directory / "store" / "plugin.py").write_text(PLUGIN, encoding="utf-8", newline="\n")
        settings = 'plugin: "plugin.py"'
    return boards.load(write_board(directory, members, settings=settings))


def layout_of(board: Board, given: dict[str, object] | None = None) -> Layout:
    registry.default().load_board(board)
    return for_board(board, driver_for(board), sources(given))


# ------------------------------------------------------------ board files


def test_a_partition_names_its_target_region_format_and_contents(tmp_path: Path) -> None:
    board = board_with(
        tmp_path,
        'option layout: choice("small", "large")\n'
        'partition config: raw(on: flash0, at: 0, size: 64 KiB, contents: "build/config.bin")\n'
        'if layout == "large" {\n'
        "    partition spare: raw(on: flash0, at: 512 KiB, size: 512 KiB)\n"
        "}\n"
        "partition table: raw(on: mcu.flash, at: 0x80000, size: 16 KiB)\n",
    )
    config, table = board.partitions
    assert config.name == "config" and config.format == "raw"
    assert config.on == PeripheralRef("flash0") and config.target == "flash0"
    assert (config.at, config.size, config.end) == (0, 0x10000, 0x10000)
    assert config.contents == (tmp_path / "store" / "build" / "config.bin").resolve()
    assert config.where.endswith("board.jig:11:5")
    assert table.on == InternalRef("mcu", "flash") and table.target == "mcu.flash"
    assert table.contents is None and dict(table.arguments) == {}
    large = board.evaluate({"layout": "large"})
    assert [p.name for p in large.partitions] == ["config", "spare", "table"]
    assert board.partition("table") is table
    with pytest.raises(ConfigError, match=r"no partition 'data' \(has: config, table\)"):
        board.partition("data")


@pytest.mark.parametrize(
    ("member", "message"),
    [
        ("partition p: 3", r"partition p: a partition is FORMAT\(on: \.\.\., at: \.\.\."),
        ("partition p: raw", r"a partition is FORMAT\(on"),
        ('partition p: raw(on: "flash0", at: 0, size: 4 KiB)', r"on is a storage peripheral"),
        ("partition p: raw(at: 0, size: 4 KiB)", r"on is a storage peripheral \(flash0\)"),
        ("partition p: raw(on: flash0, at: -1, size: 4 KiB)", r"at is a whole number of bytes"),
        ("partition p: raw(on: flash0, at: 0, size: 0)", r"size is a whole number of bytes"),
        ('partition p: raw(on: flash0, at: 0, size: "big")', r"size is a whole number"),
        ("partition p: raw(on: flash0, at: 1.5, size: 4 KiB)", r"at is a whole number"),
        (
            "partition p: raw(on: flash0, at: 0, size: 4 KiB, block_size: 512)",
            r"partition p: raw: raw takes no arguments besides on, at, size and contents, "
            r"not block_size",
        ),
        (
            "partition p: spiffs(on: flash0, at: 0, size: 4 KiB)",
            r"no storage format 'spiffs' \(known: [a-z, ]*raw[a-z, ]*; a board's plugin adds "
            r"others\)",
        ),
        ("partition p: raw(on: flash9, at: 0, size: 4 KiB)", r"unknown name 'flash9'"),
        ("partition flash0: raw(on: flash0, at: 0, size: 4 KiB)", r"flash0 is declared twice"),
        (
            "partition p: raw(on: flash0, at: 0, size: 4 KiB, contents: 3)",
            r"contents: expected text",
        ),
    ],
)
def test_a_partition_declaration_is_checked(tmp_path: Path, member: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        board_with(tmp_path, member)


def test_a_partition_on_a_peripheral_off_the_storage_list_is_an_error(tmp_path: Path) -> None:
    template = STM32.replace("storage: [flash0]", "")
    with pytest.raises(ConfigError, match=r"flash0 is not on the board's storage list"):
        boards.load(
            write_board(
                tmp_path, "partition p: raw(on: flash0, at: 0, size: 4 KiB)", template=template
            )
        )


def test_partition_names_follow_the_name_rules(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"spi is declared twice \(link and partition\)"):
        board_with(tmp_path, "partition spi: raw(on: flash0, at: 0, size: 4 KiB)")
    # A partition is no value in expressions, so the option that picks its
    # contents can share its name.
    shared = board_with(
        tmp_path / "a",
        'option data: "build/a.bin"\n'
        "partition data: raw(on: flash0, at: 0, size: 4 KiB, contents: data)",
    )
    assert (
        shared.partition("data").contents
        == (tmp_path / "a" / "store" / "build" / "a.bin").resolve()
    )
    from jig.lang.library import name_collisions

    board = board_with(tmp_path / "b", "partition reset: raw(on: flash0, at: 0, size: 4 KiB)")
    assert name_collisions(board, []) == [
        "board store: partition reset has the name of the board member reset; rename the partition"
    ]


def test_partition_is_a_word_only_where_a_declaration_starts(tmp_path: Path) -> None:
    from jig.testing import run_source

    report = run_source(
        tmp_path, 'test "t" {\n    let partition = 3\n    check partition == 3\n}\n'
    )
    assert report.cases[0].status == "PASS", report.cases[0].detail


# ------------------------------------------------------------- resolving


def test_a_layout_holds_targets_partitions_and_sources(tmp_path: Path) -> None:
    board = board_with(
        tmp_path,
        "partition config: raw(on: flash0, at: 0, size: 64 KiB)\n"
        "partition table: raw(on: mcu.flash, at: 0x80000, size: 16 KiB)\n",
    )
    image, config_file, boot, app = (tmp_path / name for name in ("i", "c", "b", "a"))
    layout = layout_of(
        board,
        {
            "flash0": image,
            "config": config_file,
            "mcu.flash": {0: boot, 0x10000: app},
        },
    )
    assert layout.problems == []
    assert sorted(layout.targets) == ["flash0", "mcu.flash", "mcu.sram"]
    flash = layout.targets["mcu.flash"]
    assert flash.memory is not None and flash.size == 0x200000 and flash.memory.fill == 0xFF
    assert layout.targets["mcu.sram"].size == 0x20000
    assert layout.bases["flash0"].path == image
    assert layout.offsets["mcu.flash"].offsets == ((0, boot), (0x10000, app))
    config = layout.partition("config")
    assert config.source is not None and config.source.path == config_file
    assert config.format_name == "raw" and isinstance(config.format, formats.Raw)
    assert layout.partition("table").source is None
    assert layout.written("flash0") and layout.written("mcu.flash")
    assert not layout.written("mcu.sram")


def test_device_memories_are_named_only_when_something_names_one(tmp_path: Path) -> None:
    board = board_with(tmp_path, "partition config: raw(on: flash0, at: 0, size: 64 KiB)")
    assert sorted(layout_of(board).targets) == ["flash0"]


@pytest.mark.parametrize(
    ("members", "given", "message"),
    [
        (
            "partition a: raw(on: flash0, at: 0, size: 64 KiB)\n"
            "partition b: raw(on: flash0, at: 32 KiB, size: 64 KiB)\n"
            "partition c: raw(on: flash0, at: 16 KiB, size: 4 KiB)",
            {},
            r"partition b \(0x8000-0x17FFF\) overlaps partition a \(0x0-0xFFFF\) on flash0",
        ),
        (
            "partition a: raw(on: flash0, at: 1020 KiB, size: 8 KiB)",
            {},
            r"0xFF000 \+ 0x2000 runs past the end of storage flash0 \(1048576 bytes\)",
        ),
        (
            "partition a: raw(on: mcu.eeprom, at: 0, size: 8 KiB)",
            {},
            r"partition a: mcu has no memory eeprom \(has: mcu.flash, mcu.sram\)",
        ),
        ("", {"nothing": "x.bin"}, r"no partition, storage device or device memory 'nothing'"),
        ("", {"mcu.rom": "x.bin"}, r"a device memory is DEVICE.MEMORY, quoted"),
        ("", {"flash0": {0x100000: "x.bin"}}, r"offset 0x100000 is past the end of storage"),
        (
            "partition a: raw(on: flash0, at: 0, size: 4 KiB)",
            {"a": {0x1000: "x.bin"}},
            r"offset 0x1000 is past the end of partition a \(4096 bytes\)",
        ),
    ],
)
def test_a_layout_names_its_problems(
    tmp_path: Path, members: str, given: dict[str, object], message: str
) -> None:
    layout = layout_of(board_with(tmp_path, members), given)
    assert any(re.search(message, problem) for problem in layout.problems), layout.problems
    with pytest.raises(ConfigError):
        layout.raise_problems()


def test_overlaps_name_the_partition_reaching_furthest(tmp_path: Path) -> None:
    layout = layout_of(
        board_with(
            tmp_path,
            "partition a: raw(on: flash0, at: 0, size: 64 KiB)\n"
            "partition b: raw(on: flash0, at: 4 KiB, size: 4 KiB)\n"
            "partition c: raw(on: flash0, at: 16 KiB, size: 4 KiB)",
        )
    )
    assert [problem.split(": ", 1)[1] for problem in layout.problems] == [
        "partition b (0x1000-0x1FFF) overlaps partition a (0x0-0xFFFF) on flash0",
        "partition c (0x4000-0x4FFF) overlaps partition a (0x0-0xFFFF) on flash0",
    ]


def test_a_storage_device_with_partitions_needs_its_size(tmp_path: Path) -> None:
    path = write_board(tmp_path, "partition a: raw(on: flash0, at: 0, size: 4 KiB)")
    path.write_text(
        path.read_text(encoding="utf-8").replace(" (size: 1 MiB)", ""),
        encoding="utf-8",
        newline="\n",
    )
    layout = layout_of(boards.load(path))
    assert layout.problems == [
        "storage flash0: its partitions and files at offsets need the peripheral's size, "
        "which it does not give: add size to flash0, such as size: 16 MiB"
    ]
    # A file for the whole device alone needs no size: the model gives it.
    assert layout_of(boards.load(path).evaluate(), {}).targets["flash0"].size is None


def test_formats_a_plugin_registers_are_known_to_its_board(tmp_path: Path) -> None:
    board = board_with(
        tmp_path,
        "partition data: listing(on: flash0, at: 64 KiB, size: 64 KiB, block_size: 4 KiB)\n"
        "partition odd: listing(on: flash0, at: 130 KiB, size: 4 KiB, block_size: 4 KiB)\n",
        plugin=True,
    )
    layout = layout_of(board)
    data = layout.partition("data")
    assert isinstance(data.format, Listing) and dict(data.arguments) == {"block_size": 4096}
    assert layout.problems == [
        f"{layout.partition('odd').where}: partition odd: at 0x20800 is not a multiple of "
        "its block_size, 4096"
    ]
    other = board_with(
        tmp_path / "b",
        "partition data: blocks(on: flash0, at: 0, size: 64 KiB)",
        plugin=True,
    )
    (problem,) = layout_of(other).problems
    known = ", ".join(
        sorted([*formats.builtin(), "listing"])
    )  # the shipped formats and the plugin's
    assert problem.endswith(f"partition data: no storage format 'blocks' (known: {known})")


def test_the_storage_map_takes_paths_and_offsets(tmp_path: Path) -> None:
    assert sources({"a": "x.bin", "b": tmp_path, "c": {0: "y.bin"}}) == {
        "a": Path("x.bin"),
        "b": tmp_path,
        "c": {0: Path("y.bin")},
    }
    with pytest.raises(ConfigError, match=r"storage a: -1 is not an offset"):
        sources({"a": {-1: "x.bin"}})
    with pytest.raises(ConfigError, match=r"storage a: at 0x0 names no file"):
        sources({"a": {0: 3}})
    with pytest.raises(ConfigError, match=r"storage a: a source is a file, a folder or a map"):
        sources({"a": 3})


def test_resolve_takes_memories_and_formats_from_its_caller(tmp_path: Path) -> None:
    board = board_with(tmp_path, "partition config: raw(on: flash0, at: 0, size: 64 KiB)")
    layout = resolve(board, {}, {"config": tmp_path / "c.bin"}, {"listing": Listing()})
    assert layout.problems == [] and list(layout.targets) == ["flash0"]
