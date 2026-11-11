"""The littlefs format: arguments, building, checking prebuilt images and
reading files back."""

from __future__ import annotations

import shutil
import weakref
from pathlib import Path
from typing import Any

import pytest
from littlefs import LittleFS, UserContext

from jig.errors import ConfigError
from jig.storage import formats
from jig.storage.formats._tree import Source
from jig.storage.formats.littlefs import LittleFs, _Settings

from .storage_support import files, host_tree, make_tree, nested, view_tree

KIB = 1024
MIB = 1024 * KIB
FIRMWARE = {"block_size": 4 * KIB, "read_size": 16, "prog_size": 16}


@pytest.fixture
def littlefs() -> LittleFs:
    found = formats.get("littlefs")
    assert isinstance(found, LittleFs)
    return found


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return make_tree(tmp_path / "tree", nested())


def built(
    littlefs: LittleFs, folder: Path, size: int, **given: Any
) -> tuple[bytes, dict[str, Any]]:
    arguments = littlefs.arguments({**FIRMWARE, **given}, size)
    return littlefs.build(folder, size, arguments), arguments


def mounted(image: bytes, block_size: int = 4 * KIB, **config: Any) -> LittleFS:
    """The image as littlefs-python mounts it, apart from Jig's code."""
    volume = LittleFS(
        context=UserContext(buffer=bytearray(image)),
        block_size=block_size,
        block_count=len(image) // block_size,
        mount=False,
        **config,
    )
    volume.mount()
    return volume


def test_jig_ships_littlefs() -> None:
    assert set(formats.builtin()) == {"raw", "littlefs", "fat"}
    assert formats.get("littlefs").name == "littlefs"


def test_the_arguments_default_to_littlefs_own(littlefs: LittleFs) -> None:
    assert littlefs.arguments({"block_size": 4 * KIB}, MIB) == {
        "block_size": 4 * KIB,
        "block_count": 256,
        "read_size": 4 * KIB,
        "prog_size": 4 * KIB,
        "name_max": 255,
        "file_max": 2_147_483_647,
        "attr_max": 1022,
        "disk_version": "2.1",
    }


def test_completed_arguments_complete_to_themselves(littlefs: LittleFs) -> None:
    given = {**FIRMWARE, "block_count": 200, "name_max": 64, "disk_version": 2.0}
    once = littlefs.arguments(given, MIB)
    assert littlefs.arguments(once, MIB) == once
    assert once["block_count"] == 200
    assert once["disk_version"] == "2.0"


@pytest.mark.parametrize("version", ["2.0", 2.0, 0x0002_0000, " 2.0 "])
def test_disk_version_takes_a_string_a_number_or_the_encoding(
    littlefs: LittleFs, version: object
) -> None:
    assert littlefs.arguments({**FIRMWARE, "disk_version": version}, MIB)["disk_version"] == "2.0"


@pytest.mark.parametrize(
    ("given", "size", "message"),
    [
        ({}, MIB, "needs block_size"),
        ({"block_size": 4 * KIB, "blocksize": 4}, MIB, "not blocksize"),
        ({"block_size": 64}, MIB, "block_size is at least 128"),
        ({"block_size": 4.0 * KIB}, MIB, "block_size is a whole number"),
        ({"block_size": True}, MIB, "block_size is a whole number"),
        ({"block_size": 4 * KIB}, MIB + 512, "not a whole number of 4096-byte blocks"),
        ({"block_size": 4 * KIB}, 4 * KIB, "holds 1 blocks of 4096 bytes, and littlefs needs 2"),
        ({"block_size": 4 * KIB, "block_count": 1}, MIB, "block_count is at least 2"),
        ({"block_size": 4 * KIB, "disk_version": 2.05}, MIB, "disk_version is 2.0 or 2.1"),
        ({"block_size": 4 * KIB, "block_count": 257}, MIB, "past the partition's 1048576"),
        ({"block_size": 4 * KIB, "read_size": 48, "prog_size": 32}, MIB, "multiple of both"),
        ({"block_size": 4 * KIB, "read_size": 0}, MIB, "read_size is at least 1"),
        ({"block_size": 4 * KIB, "name_max": 256}, MIB, "name_max is 1 to 255"),
        ({"block_size": 4 * KIB, "attr_max": 1023}, MIB, "attr_max is 1 to 1022"),
        ({"block_size": 4 * KIB, "file_max": 2**31}, MIB, "file_max is 1 to 2147483647"),
        ({"block_size": 4 * KIB, "disk_version": "2.2"}, MIB, "disk_version is 2.0 or 2.1"),
        ({"block_size": 4 * KIB, "disk_version": True}, MIB, "disk_version is 2.0 or 2.1"),
    ],
)
def test_arguments_the_firmware_would_refuse(
    littlefs: LittleFs, given: dict[str, Any], size: int, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        littlefs.arguments(given, size)


@pytest.mark.parametrize(
    "given",
    [
        {},
        {"read_size": 1, "prog_size": 1},
        {"read_size": 256, "prog_size": 32},
        {"disk_version": "2.0"},
        {"name_max": 64, "attr_max": 64},
        {"block_size": 512},
    ],
)
def test_a_folder_round_trips(littlefs: LittleFs, tree: Path, given: dict[str, Any]) -> None:
    image, arguments = built(littlefs, tree, MIB, **given)
    assert len(image) == MIB
    view = littlefs.open(image, arguments)
    assert view_tree(view) == host_tree(tree)
    assert littlefs.check(image, arguments) == []
    # littlefs-python, apart from Jig's view, reads the same files.
    volume = mounted(image, arguments["block_size"])
    with volume.open("/data/big.bin", "rb") as handle:
        assert handle.read() == (tree / "data" / "big.bin").read_bytes()
    assert volume.fs_stat().disk_version == (
        0x0002_0000 if given.get("disk_version") else 0x0002_0001
    )


def test_the_view_lists_reads_and_refuses(littlefs: LittleFs, tree: Path) -> None:
    image, arguments = built(littlefs, tree, MIB)
    view = littlefs.open(image, arguments)
    assert [entry.name for entry in view.files()] == [
        "A file name longer than eight.dat",
        "README.TXT",
        "config",
        "data",
        "empty.bin",
        "exact.bin",
        "logs",
        "notes.txt",
    ]
    assert view.files("/logs") == []
    assert view.stat("config//settings.json/").path == "/config/settings.json"
    assert view.stat("/data/../config/settings.json").size == 14
    assert view.stat("/").kind == "dir"
    assert view.read("/empty.bin") == b""
    assert view.exists("/data/two/deep") and not view.exists("/readme.txt")
    with pytest.raises(ConfigError, match="no file or directory /missing"):
        view.stat("/missing")
    with pytest.raises(ConfigError, match="/config is a directory"):
        view.read("/config")
    with pytest.raises(ConfigError, match=r"/notes\.txt is a file"):
        view.files("/notes.txt")
    with pytest.raises(ConfigError, match="a path in an image is a string"):
        view.exists(3)  # type: ignore[arg-type]


def test_two_builds_of_one_folder_give_the_same_bytes(
    littlefs: LittleFs, tree: Path, tmp_path: Path
) -> None:
    first, arguments = built(littlefs, tree, MIB)
    copy = shutil.copytree(tree, tmp_path / "elsewhere" / "copy")
    assert littlefs.build(tree, MIB, arguments) == first
    assert littlefs.build(copy, MIB, arguments) == first


def test_blocks_littlefs_leaves_unwritten_hold_the_erased_value(
    littlefs: LittleFs, tree: Path
) -> None:
    arguments = littlefs.arguments({**FIRMWARE, "block_count": 128}, MIB)
    for erased in (0xFF, 0x00):
        image = littlefs.build(tree, MIB, arguments, erased)
        blocks = [image[start : start + 4 * KIB] for start in range(0, MIB, 4 * KIB)]
        untouched = [block for block in blocks if block == bytes([erased]) * (4 * KIB)]
        assert len(untouched) > 128  # past block_count, and most of the volume
        assert image[128 * 4 * KIB :] == bytes([erased]) * (128 * 4 * KIB)
        assert view_tree(littlefs.open(image, arguments)) == host_tree(tree)
    with pytest.raises(ConfigError, match="an erased value is a byte"):
        littlefs.build(tree, MIB, arguments, 256)


def test_names_at_the_name_limit(littlefs: LittleFs, tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "names", {"a" * 32: b"fits", "b" * 31 + "é": b"33 bytes"})
    arguments = littlefs.arguments({**FIRMWARE, "name_max": 33}, MIB)
    view = littlefs.open(littlefs.build(folder, MIB, arguments), arguments)
    assert view.read("/" + "b" * 31 + "é") == b"33 bytes"
    arguments = littlefs.arguments({**FIRMWARE, "name_max": 32}, MIB)
    with pytest.raises(ConfigError, match="takes 33 bytes of UTF-8, past name_max 32"):
        littlefs.build(folder, MIB, arguments)


def test_a_name_of_littlefs_largest_size(littlefs: LittleFs, tmp_path: Path) -> None:
    name = "n" * 255
    try:
        folder = make_tree(tmp_path / "long", {"d": {name: b"255"}})
    except OSError:
        pytest.skip("this host's file system can't hold a 255-character name here")
    arguments = littlefs.arguments(FIRMWARE, MIB)
    view = littlefs.open(littlefs.build(folder, MIB, arguments), arguments)
    assert view.read(f"/d/{name}") == b"255"


def test_a_file_past_file_max(littlefs: LittleFs, tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "big", {"ok.bin": bytes(1000), "big.bin": bytes(1001)})
    arguments = littlefs.arguments({**FIRMWARE, "file_max": 1000}, MIB)
    with pytest.raises(ConfigError, match=r"/big\.bin is 1001 bytes, past file_max 1000"):
        littlefs.build(folder, MIB, arguments)


def test_a_folder_too_big_for_the_partition_says_what_it_takes(
    littlefs: LittleFs, tmp_path: Path
) -> None:
    folder = make_tree(tmp_path / "full", files(40, 3, (5000, 9000)))
    arguments = littlefs.arguments(FIRMWARE, 64 * KIB)
    with pytest.raises(ConfigError) as raised:
        littlefs.build(folder, 64 * KIB, arguments)
    message = str(raised.value)
    assert "blocks of 4096 bytes" in message
    assert "the partition has 16 (65536 bytes)" in message
    taken = int(message.split(" takes ")[1].split(" blocks")[0])
    roomy = littlefs.arguments({**FIRMWARE, "block_count": taken + 8}, (taken + 8) * 4 * KIB)
    image = littlefs.build(folder, (taken + 8) * 4 * KIB, roomy)
    assert mounted(image).used_block_count == taken


def test_no_volume_is_asked_its_size_after_its_unmount(
    littlefs: LittleFs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # lfs_unmount frees the caches lfs_fs_size reads through, so a size query
    # after it writes into freed heap memory: glibc and macOS abort later, at
    # whatever allocation finds the damage, and Windows carries on.
    unmounted: weakref.WeakSet[LittleFS] = weakref.WeakSet()
    unmounts: list[int] = []
    unmount = LittleFS.unmount
    used_block_count = LittleFS.used_block_count.fget

    def tracked_unmount(volume: LittleFS) -> int:
        unmounted.add(volume)
        unmounts.append(1)
        return unmount(volume)

    def guarded_used_block_count(volume: LittleFS) -> int:
        assert volume not in unmounted, "a size query on an unmounted volume"
        return used_block_count(volume)

    monkeypatch.setattr(LittleFS, "unmount", tracked_unmount)
    monkeypatch.setattr(LittleFS, "used_block_count", property(guarded_used_block_count))
    folder = make_tree(tmp_path / "full", files(40, 3, (5000, 9000)))
    with pytest.raises(ConfigError, match=r"takes \d+ blocks of 4096 bytes"):
        littlefs.build(folder, 64 * KIB, littlefs.arguments(FIRMWARE, 64 * KIB))
    assert unmounts  # _too_big built and unmounted a roomier volume


def test_a_folder_is_a_folder(littlefs: LittleFs, tmp_path: Path) -> None:
    arguments = littlefs.arguments(FIRMWARE, MIB)
    with pytest.raises(ConfigError, match="no folder"):
        littlefs.build(tmp_path / "missing", MIB, arguments)
    (tmp_path / "file.bin").write_bytes(b"x")
    with pytest.raises(ConfigError, match="is a file"):
        littlefs.build(tmp_path / "file.bin", MIB, arguments)


def test_check_names_each_field_that_differs(littlefs: LittleFs, tree: Path) -> None:
    image, _ = built(
        littlefs,
        tree,
        MIB,
        block_count=200,
        name_max=64,
        file_max=100_000,
        attr_max=100,
        disk_version="2.0",
    )
    problems = littlefs.check(image, littlefs.arguments(FIRMWARE, MIB))
    assert problems == [
        "disk_version: the superblock says 2.0, the arguments say 2.1",
        "block_count: the superblock says 200, the arguments say 256",
        "name_max: the superblock says 64, the arguments say 255",
        "file_max: the superblock says 100000, the arguments say 2147483647",
        "attr_max: the superblock says 100, the arguments say 1022",
    ]


def test_check_finds_the_block_size_of_a_different_image(littlefs: LittleFs, tree: Path) -> None:
    image, _ = built(littlefs, tree, MIB, block_size=2 * KIB)
    problems = littlefs.check(image, littlefs.arguments(FIRMWARE, MIB))
    assert problems == [
        "block_size: the superblock says 2048, the arguments say 4096",
        "block_count: the superblock says 512, the arguments say 256",
    ]


def test_check_reads_block_one_when_block_zero_is_erased(littlefs: LittleFs, tree: Path) -> None:
    image, arguments = built(littlefs, tree, MIB, block_size=2 * KIB)
    erased = b"\xff" * (2 * KIB) + image[2 * KIB :]
    assert littlefs.check(erased, littlefs.arguments(FIRMWARE, MIB)) == [
        "block_size: the superblock says 2048, the arguments say 4096",
        "block_count: the superblock says 512, the arguments say 256",
    ]
    assert view_tree(littlefs.open(erased, arguments)) == host_tree(tree)


def test_check_of_bytes_that_are_no_littlefs(littlefs: LittleFs) -> None:
    arguments = littlefs.arguments(FIRMWARE, MIB)
    for image in (b"\xff" * MIB, bytes(range(256)) * 4096, b""):
        assert littlefs.check(image, arguments) == [
            "no littlefs superblock: neither of the first two blocks starts with one "
            '(the name "littlefs" at byte 8)'
        ]


def test_check_of_a_superblock_littlefs_cannot_mount(littlefs: LittleFs, tree: Path) -> None:
    image, arguments = built(littlefs, tree, MIB)
    damaged = bytearray(image)
    damaged[24:28] = (8 * KIB).to_bytes(4, "little")  # block_size in the first superblock entry
    problems = littlefs.check(bytes(damaged), arguments)
    assert problems[0].startswith("littlefs can't mount the image")
    assert "block_size: the superblock says 8192, the arguments say 4096" in problems


def test_check_of_a_superblock_too_large_to_mount(littlefs: LittleFs, tree: Path) -> None:
    image, arguments = built(littlefs, tree, MIB)
    damaged = bytearray(image)
    damaged[24:28] = (1 << 30).to_bytes(4, "little")
    problems = littlefs.check(bytes(damaged), arguments)
    assert problems[0].startswith("littlefs can't mount the image with the superblock's own")
    assert "block_size 1073741824 and block_count 256 can't be mounted" in problems[0]


def test_check_of_the_name_without_its_tags(littlefs: LittleFs, tree: Path) -> None:
    image, arguments = built(littlefs, tree, MIB)
    damaged = bytearray(image)
    damaged[4:8] = b"\0\0\0\0"  # the name tag
    damaged[4 * KIB : 8 * KIB] = b"\xff" * (4 * KIB)  # and block 1
    assert littlefs.check(bytes(damaged), arguments)[0].startswith("no littlefs superblock")


def test_check_of_an_image_shorter_than_its_volume(littlefs: LittleFs, tmp_path: Path) -> None:
    # An empty volume keeps everything in the superblock pair, blocks 0 and
    # 1; the reads past the image's end return erased bytes.
    image, arguments = built(littlefs, make_tree(tmp_path / "empty", {}), MIB)
    assert littlefs.check(image[: 8 * KIB], arguments) == []


def test_a_name_that_is_not_utf8(littlefs: LittleFs) -> None:
    odd = Source("\udcff", "/\udcff", Path("\udcff"), "file", 1)
    folder = Source("", "/", Path(), "dir", 0, (odd,))
    settings = _Settings.of(littlefs.arguments(FIRMWARE, MIB))
    with pytest.raises(ConfigError, match="is not valid UTF-8"):
        settings.fits(folder)


def test_check_and_open_take_completed_arguments(littlefs: LittleFs) -> None:
    with pytest.raises(ConfigError, match="with block_size and block_count"):
        littlefs.check(b"", FIRMWARE)
    with pytest.raises(ConfigError, match="with block_size and block_count"):
        littlefs.open(b"", FIRMWARE)


def test_open_mounts_as_the_firmware_would(littlefs: LittleFs, tree: Path) -> None:
    image, _ = built(littlefs, tree, MIB, block_count=200)
    with pytest.raises(ConfigError, match="block_count: the superblock says 200"):
        littlefs.open(image, littlefs.arguments(FIRMWARE, MIB))
    with pytest.raises(ConfigError, match=r"disk_version: the superblock says 2\.1"):
        littlefs.open(image, littlefs.arguments({**FIRMWARE, "disk_version": "2.0"}, 200 * 4 * KIB))


def test_open_reads_what_a_firmware_wrote_later(littlefs: LittleFs, tree: Path) -> None:
    image, arguments = built(littlefs, tree, MIB)
    firmware = mounted(image, read_size=16, prog_size=16)
    with firmware.open("/logs/boot.log", "wb") as handle:
        handle.write(b"booted\n")
    firmware.remove("/notes.txt")
    later = bytes(firmware.context.buffer)
    view = littlefs.open(later, arguments)
    assert view.read("/logs/boot.log") == b"booted\n"
    assert not view.exists("/notes.txt")


def test_files_in_a_200_file_folder_round_trip(littlefs: LittleFs, tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "many", files(200, 5, (0, 10, 100, 1000, 4096, 10_000)))
    image, arguments = built(littlefs, folder, 4 * MIB)
    assert view_tree(littlefs.open(image, arguments)) == host_tree(folder)
