"""Composing a target's bytes (``jig.storage.compose``): the erased fill,
the base image, partitions and files at offsets in their order, the errors
for regions written twice and for writes over a non-erased base, prebuilt
images checked by their format, and folders built once per content and
per version of the code that builds them."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import Any

import pytest

from jig.errors import ConfigError
from jig.storage import formats
from jig.storage.compose import compose, write_regions
from jig.storage.formats.littlefs import LittleFs

from .storage_doubles import Listing, listing_image
from .test_partitions import board_with, layout_of

KIB = 1024


def files(directory: Path, **contents: bytes) -> dict[str, Path]:
    """Files by keyword: ``config__txt`` is config.txt, ``config`` config.bin."""
    directory.mkdir(parents=True, exist_ok=True)
    found = {}
    for name, data in contents.items():
        path = directory / (name.replace("__", ".") if "__" in name else f"{name}.bin")
        path.write_bytes(data)
        found[name] = path
    return found


def test_an_image_is_erased_bytes_with_its_base_partitions_and_offsets(tmp_path: Path) -> None:
    board = board_with(
        tmp_path,
        "partition config: raw(on: flash0, at: 64 KiB, size: 4 KiB)\n"
        "partition spare: raw(on: flash0, at: 128 KiB, size: 4 KiB)\n",
    )
    given = files(tmp_path / "in", base=b"BOOT" + b"\xff" * 16, config=b"CFG", app=b"APP")
    layout = layout_of(board, {"flash0": given["base"], "config": given["config"]})
    composed = compose(layout, "flash0", erased=0xFF)
    image = composed.image()
    assert len(image) == 1024 * KIB and composed.size == 1024 * KIB
    assert image[:20] == b"BOOT" + b"\xff" * 16
    assert image[64 * KIB : 64 * KIB + 4] == b"CFG\xff"  # the rest of the partition erased
    assert image[128 * KIB : 132 * KIB] == b"\xff" * 4 * KIB  # spare: no contents
    assert image.count(0xFF) == len(image) - 7
    assert composed.base is not None and composed.base.label == "the base image base.bin"
    assert [r.label for r in composed.regions] == ["partition config (config.bin)"]
    offsets = layout_of(board, {"flash0": {0x200: given["app"]}})
    placed = compose(offsets, "flash0", erased=0x00).image()
    assert placed[0x200:0x203] == b"APP" and placed.count(0) == len(placed) - 3


def test_a_partitions_default_contents_comes_from_the_board_file(tmp_path: Path) -> None:
    files(tmp_path / "store" / "build", config__bin=b"DEFAULT")
    board = board_with(
        tmp_path,
        'partition config: raw(on: flash0, at: 0, size: 4 KiB, contents: "build/config.bin")',
    )
    assert compose(layout_of(board), "flash0", erased=0xFF).image()[:7] == b"DEFAULT"
    other = files(tmp_path / "in", other=b"SYSTEM")
    replaced = layout_of(board, {"config": other["other"]})
    assert compose(replaced, "flash0", erased=0xFF).image()[:7] == b"SYSTEM\xff"


def test_a_folder_is_built_by_the_partitions_format_once_per_content(tmp_path: Path) -> None:
    board = board_with(
        tmp_path,
        "partition data: listing(on: flash0, at: 64 KiB, size: 16 KiB, block_size: 4 KiB)",
        plugin=True,
    )
    folder = tmp_path / "folder"
    files(folder, a__txt=b"alpha")
    files(folder / "sub", b__txt=b"beta")
    layout = layout_of(board, {"data": folder})
    listing = layout.partition("data").format
    assert isinstance(listing, Listing)
    first = compose(layout, "flash0", erased=0xFF).image()
    view = listing.open(first[64 * KIB : 80 * KIB], {"block_size": 4096})
    assert view is not None and view.read("/sub/b.txt") == b"beta"
    assert [e.path for e in view.walk()] == ["/a.txt", "/sub", "/sub/b.txt"]
    assert compose(layout, "flash0", erased=0xFF).image() == first
    assert listing.builds == 1  # the second composition reused the artifact
    (folder / "a.txt").write_bytes(b"ALPHA")
    compose(layout, "flash0", erased=0xFF)
    (folder / "sub" / "c.txt").write_bytes(b"")
    compose(layout, "flash0", erased=0xFF)
    compose(layout, "flash0", erased=0x00)  # the erased value is part of the key
    assert listing.builds == 4


def test_a_folder_is_built_again_for_another_version_of_its_builders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = board_with(
        tmp_path,
        "partition data: littlefs(on: flash0, at: 64 KiB, size: 16 KiB, block_size: 4 KiB)",
    )
    folder = tmp_path / "folder"
    files(folder, a__txt=b"alpha")
    layout = layout_of(board, {"data": folder})
    built: list[int] = []
    build = LittleFs.build

    def counted(self: LittleFs, *args: Any, **kwargs: Any) -> bytes:
        built.append(1)
        return build(self, *args, **kwargs)

    monkeypatch.setattr(LittleFs, "build", counted)
    versions = {"jig-dsl": "0.3.0a1", "littlefs-python": "0.19.0"}
    monkeypatch.setattr(formats, "_version", lambda distribution: versions[distribution])
    first = compose(layout, "flash0", erased=0xFF).image()
    compose(layout, "flash0", erased=0xFF)
    assert len(built) == 1  # the cache served the second
    versions["littlefs-python"] = "0.19.1"
    assert compose(layout, "flash0", erased=0xFF).image() == first
    assert len(built) == 2
    versions["jig-dsl"] = "0.3.0a2"
    compose(layout, "flash0", erased=0xFF)
    assert len(built) == 3


def test_the_builders_of_a_format_are_jig_and_the_library_that_writes_it() -> None:
    jig = importlib.metadata.version("jig-dsl")
    assert formats.builders("littlefs") == {
        "jig-dsl": jig,
        "littlefs-python": importlib.metadata.version("littlefs-python"),
    }
    assert formats.builders("fat") == {"jig-dsl": jig}  # Jig writes FAT volumes itself
    assert formats.builders("listing") == {"jig-dsl": jig}  # a plugin's format


def test_a_prebuilt_image_is_checked_by_its_format(tmp_path: Path) -> None:
    board = board_with(
        tmp_path,
        "partition data: listing(on: flash0, at: 0, size: 16 KiB, block_size: 4 KiB)",
        plugin=True,
    )
    good = tmp_path / "good.bin"
    good.write_bytes(listing_image({"a.txt": b"x"}, 8 * KIB, block_size=4096))
    image = compose(layout_of(board, {"data": good}), "flash0", erased=0xFF).image()
    assert image[: 8 * KIB] == good.read_bytes() and image[8 * KIB : 16 * KIB] == b"\xff" * 8 * KIB
    bad = tmp_path / "bad.bin"
    bad.write_bytes(listing_image({"a.txt": b"x"}, 8 * KIB, block_size=512))
    with pytest.raises(
        ConfigError,
        match=r"the storage map's data: bad.bin as listing: block size 512, the partition's "
        r"is 4096",
    ):
        compose(layout_of(board, {"data": bad}), "flash0", erased=0xFF)


def test_each_problem_composing_a_target_is_named(tmp_path: Path) -> None:
    board = board_with(
        tmp_path,
        "partition config: raw(on: flash0, at: 0, size: 4 KiB)\n"
        "partition log: raw(on: flash0, at: 8 KiB, size: 4 KiB)\n",
    )
    given = files(
        tmp_path / "in",
        big=b"x" * (4 * KIB + 1),
        small=b"y" * 16,
        base=b"\x00" * (12 * KIB),
    )
    layout = layout_of(
        board,
        {
            "flash0": given["base"],
            "config": given["big"],
            "log": tmp_path / "missing.bin",
        },
    )
    with pytest.raises(ConfigError) as caught:
        compose(layout, "flash0", erased=0xFF)
    lines = str(caught.value).splitlines()
    assert lines[0] == "storage flash0: 2 problems composing it:"
    assert "big.bin (4097 bytes) is larger than partition config (4096 bytes)" in lines[1]
    assert lines[2].endswith(f"the storage map's log: {tmp_path / 'missing.bin'} not found")
    twice = layout_of(board, {"config": given["small"], "flash0": {0x10: given["small"]}})
    with pytest.raises(
        ConfigError,
        match=r"small.bin at 0x10 \(0x10-0x1F\) writes over partition config \(small.bin\) "
        r"\(0x0-0xFFF\)",
    ):
        compose(twice, "flash0", erased=0xFF)
    over = layout_of(board, {"flash0": given["base"], "log": given["small"]})
    with pytest.raises(
        ConfigError,
        match=r"partition log \(small.bin\) \(0x2000-0x2FFF\) lands on bytes of the base image "
        r"base.bin that are not erased \(0xFF\), from 0x2000",
    ):
        compose(over, "flash0", erased=0xFF)
    # The same base is erased for a device whose erased value is 0.
    assert compose(over, "flash0", erased=0x00).image()[8 * KIB : 8 * KIB + 16] == b"y" * 16


def test_the_base_must_fit_and_be_a_file(tmp_path: Path) -> None:
    board = board_with(tmp_path)
    big = tmp_path / "big.bin"
    big.write_bytes(b"\xff" * (1024 * KIB + 1))
    with pytest.raises(ConfigError, match=r"big.bin \(1048577 bytes\) is larger than storage"):
        compose(layout_of(board, {"flash0": big}), "flash0", erased=0xFF)
    with pytest.raises(ConfigError, match=r"is a folder: a folder fills a partition"):
        compose(layout_of(board, {"flash0": tmp_path}), "flash0", erased=0xFF)
    family = tmp_path / "boot.bin"
    family.write_bytes(b"HEADER")
    composed = compose(layout_of(board), "flash0", erased=0xFF, base=family, base_label="hook")
    assert composed.image()[:6] == b"HEADER" and composed.written
    assert not compose(layout_of(board), "flash0", erased=0xFF).written
    with pytest.raises(ConfigError, match=r"the board gives 1048576 bytes and the model 4096"):
        compose(layout_of(board), "flash0", erased=0xFF, size=4096)
    with pytest.raises(ConfigError, match=r"an erased value is a byte, not 256"):
        compose(layout_of(board), "flash0", erased=256)


def test_a_device_memory_receives_its_writes_as_files(tmp_path: Path) -> None:
    board = board_with(
        tmp_path,
        "partition table: raw(on: mcu.flash, at: 0x80000, size: 4 KiB)\n"
        "partition scratch: raw(on: mcu.sram, at: 0, size: 1 KiB)\n",
    )
    given = files(tmp_path / "in", table=b"TBL", word=b"\x01\x02")
    layout = layout_of(board, {"table": given["table"], "mcu.flash": {0x90000: given["word"]}})
    composed = compose(layout, "mcu.flash", erased=0xFF)
    assert [(r.offset, len(r.data), r.label) for r in composed.writes()] == [
        (0x80000, 4096, "partition table (table.bin)"),
        (0x90000, 2, "word.bin at 0x90000"),
    ]
    image = write_regions(composed, tmp_path / "out")
    assert image.memory.name == "flash"
    first, second = image.regions
    assert (first.offset, first.size, first.end) == (0x80000, 4096, 0x81000)
    assert first.path.read_bytes() == b"TBL" + b"\xff" * 4093
    assert second.path.name == "mcu.flash-1.bin" and second.label == "word.bin at 0x90000"
    with pytest.raises(ConfigError, match=r"storage flash0 is not a memory of the device"):
        write_regions(compose(layout_of(board), "flash0", erased=0xFF), tmp_path / "out")


def test_a_format_that_builds_the_wrong_size_is_an_error(tmp_path: Path) -> None:
    board = board_with(
        tmp_path,
        "partition data: listing(on: flash0, at: 0, size: 16 KiB, block_size: 4 KiB)",
        plugin=True,
    )
    folder = tmp_path / "folder"
    files(folder, a__txt=b"alpha")
    layout = layout_of(board, {"data": folder})
    listing = layout.partition("data").format
    assert isinstance(listing, Listing)
    listing.build = lambda *args, **kwargs: b"short"  # type: ignore[method-assign]
    with pytest.raises(ConfigError, match=r"format listing built 5 bytes for 16384 bytes"):
        compose(layout, "flash0", erased=0xFF)


def test_writing_an_image_gives_the_bytes_image_gives(tmp_path: Path) -> None:
    board = board_with(tmp_path, "partition log: raw(on: flash0, at: 1000 KiB, size: 4 KiB)")
    given = files(tmp_path / "in", base=b"B" * 16 + b"\xff" * (1001 * KIB), log=b"LOG")
    composed = compose(
        layout_of(board, {"flash0": given["base"], "log": given["log"]}), "flash0", erased=0xFF
    )
    path = tmp_path / "flash0.img"
    composed.write(path)
    assert path.read_bytes() == composed.image()
    assert path.stat().st_size == 1024 * KIB
