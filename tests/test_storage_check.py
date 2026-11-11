"""``jig check`` on a board's storage: the partitions' rules, composing
what they write with the IStorage default erased value, the ELF segments of
a prebuilt STM32 firmware, and formats a plugin registers through
``api.storage_format``."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig.errors import ConfigError
from jig.plugin import PluginApi
from jig.storage import formats
from jig.system import boards, checks
from jig.system.registry import Owner, Registry

from .storage_doubles import Listing, listing_image, make_elf
from .test_partitions import board_with, write_board

KIB = 1024


def problems_of(tmp_path: Path, members: str, *, plugin: bool = False) -> list[str]:
    """The board's problems, its firmware built (an ELF with one segment at
    the start of flash) unless the test made one."""
    elf = tmp_path / "store" / "build" / "app.elf"
    if not elf.exists():
        make_elf(elf, [(0x08000000, bytes(16))])
    return checks.problems(board_with(tmp_path, members, plugin=plugin))


def test_a_board_whose_storage_composes_has_no_problems(tmp_path: Path) -> None:
    folder = tmp_path / "store" / "fs"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_bytes(b"alpha")
    assert (
        problems_of(
            tmp_path,
            'partition data: listing(on: flash0, at: 0, size: 16 KiB, contents: "fs")\n'
            "partition table: raw(on: mcu.flash, at: 0x80000, size: 4 KiB)\n",
            plugin=True,
        )
        == []
    )


def test_check_reports_each_partition_rule(tmp_path: Path) -> None:
    found = problems_of(
        tmp_path,
        "partition a: raw(on: flash0, at: 0, size: 8 KiB)\n"
        "partition b: raw(on: flash0, at: 4 KiB, size: 8 KiB)\n"
        "partition c: listing(on: flash0, at: 1 MiB, size: 4 KiB)\n"
        "partition d: blocks(on: flash0, at: 64 KiB, size: 4 KiB)\n",
        plugin=True,
    )
    known = ", ".join(
        sorted([*formats.builtin(), "listing"])
    )  # the shipped formats and the plugin's
    assert [problem.split(": ", 1)[1] for problem in found] == [
        "partition c: 0x100000 + 0x1000 runs past the end of storage flash0 (1048576 bytes)",
        f"partition d: no storage format 'blocks' (known: {known})",
        "partition b (0x1000-0x2FFF) overlaps partition a (0x0-0x1FFF) on flash0",
    ]


def test_check_reports_what_composing_finds(tmp_path: Path) -> None:
    store = tmp_path / "store"
    (store / "big").mkdir(parents=True)
    (store / "big" / "huge.bin").write_bytes(b"x" * 8 * KIB)
    (store / "wrong.bin").write_bytes(listing_image({"a": b"1"}, 4 * KIB, block_size=512))
    (store / "long.bin").write_bytes(b"y" * 5 * KIB)
    found = problems_of(
        tmp_path,
        'partition built: listing(on: flash0, at: 0, size: 4 KiB, contents: "big")\n'
        "partition prebuilt: listing(on: flash0, at: 4 KiB, size: 4 KiB, block_size: 1 KiB,\n"
        '                            contents: "wrong.bin")\n'
        'partition missing: raw(on: flash0, at: 8 KiB, size: 4 KiB, contents: "none.bin")\n'
        'partition long: raw(on: mcu.sram, at: 0, size: 4 KiB, contents: "long.bin")\n',
        plugin=True,
    )
    assert len(found) == 4, found
    assert "partition built: the folder needs 8220 bytes, the partition has 4096" in found[0]
    assert found[1].endswith("wrong.bin as listing: block size 512, the partition's is 1024")
    assert found[2].endswith(f"{(store / 'none.bin').resolve()} not found")
    assert found[3].endswith("long.bin (5120 bytes) is larger than partition long (4096 bytes)")


def test_check_reports_a_prebuilt_elf_over_a_flash_partition(tmp_path: Path) -> None:
    found = problems_of(tmp_path, "partition vectors: raw(on: mcu.flash, at: 0, size: 1 KiB)")
    assert found == [
        "app.elf's segment at 0x08000000-0x0800000F lands on partition vectors on mcu.flash "
        "(0x08000000-0x080003FF)"
    ]


def test_check_reports_a_memory_its_family_cannot_name(tmp_path: Path) -> None:
    template = """\
board "store" {{
    root: "."
{settings}
    device zynq: zynq7000(part: "XC7Z020-CLG484", xsa: "build/none.xsa", ps_clk: 50 MHz,
                          ps_version: 3) {{
        firmware: {{app: "build/app.elf"}}
    }}
{members}
}}
"""
    path = write_board(
        tmp_path,
        "partition table: raw(on: zynq.axi_bram_ctrl_0, at: 0, size: 4 KiB)",
        template=template,
    )
    found = checks.problems(boards.load(path))
    assert any(
        problem.startswith("storage: board store: XSA") and problem.endswith("not found")
        for problem in found
    ), found


def test_jig_check_lists_storage_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jig.cli import main

    root = tmp_path / "project"
    (root / "boards").mkdir(parents=True)
    (root / "jig.toml").write_text('[project]\nname = "p"\n', encoding="utf-8", newline="\n")
    write_board(
        root / "boards",
        "partition a: raw(on: flash0, at: 0, size: 8 KiB)\n"
        "partition b: raw(on: flash0, at: 4 KiB, size: 8 KiB)\n",
    )
    monkeypatch.setenv("JIG_PROJECT", str(root))
    assert main(["check", "store"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "store: 2 problem(s)"
    assert "overlaps partition a" in out[2]


# -------------------------------------------------------------- the API


def api_for(tmp_path: Path) -> PluginApi:
    board = board_with(tmp_path)
    return PluginApi(Owner("board", board.name, board.source), board)


def test_a_plugin_registers_a_storage_format(tmp_path: Path) -> None:
    api = api_for(tmp_path)
    listing = Listing()
    api.storage_format("listing", listing)
    assert api.staged.storage_formats == {"listing": listing}
    fresh = Registry()
    fresh.commit(api.staged)
    assert fresh.storage_formats_for(api.board) == {"listing": listing}


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("raw", Listing(), "storage format 'raw' ships with Jig"),
        ("not a name", Listing(), "storage format 'not a name' is not a name a plan can use"),
        ("check", Listing(), "storage format 'check' is not a name a plan can use"),
        ("blocks", object(), "storage format blocks: arguments must be callable"),
    ],
)
def test_the_api_checks_a_storage_format(
    tmp_path: Path, name: str, value: object, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        api_for(tmp_path).storage_format(name, value)  # type: ignore[arg-type]


def test_two_boards_may_register_formats_of_one_name(tmp_path: Path) -> None:
    fresh = Registry()
    first, second = board_with(tmp_path / "a"), board_with(tmp_path / "b")
    formats_by_board = {}
    for board in (first, second):
        api = PluginApi(Owner("board", board.name, board.source), board)
        made = Listing()
        api.storage_format("listing", made)
        fresh.commit(api.staged)
        formats_by_board[board.source] = made
    for board in (first, second):
        assert fresh.storage_formats_for(board)["listing"] is formats_by_board[board.source]
    assert "listing" not in formats.builtin()
