"""The FAT format: arguments, the volumes Jig writes (read back with
pyfatfs, apart from Jig's view), checking prebuilt images and reading files
back."""

from __future__ import annotations

import io
import shutil
import struct
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pyfatfs.PyFat import PyFat  # type: ignore[import-untyped]

from jig.errors import ConfigError
from jig.storage import formats
from jig.storage.formats._tree import Source
from jig.storage.formats.fat import Fat, _basis, _names

from .storage_support import files, host_tree, make_tree, nested, view_tree

KIB = 1024
MIB = 1024 * KIB


@pytest.fixture
def fat() -> Fat:
    found = formats.get("fat")
    assert isinstance(found, Fat)
    return found


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return make_tree(tmp_path / "tree", nested(unit=2048))


def built(fat: Fat, folder: Path, size: int, **given: Any) -> tuple[bytes, dict[str, Any]]:
    arguments = fat.arguments(given, size)
    return fat.build(folder, size, arguments), arguments


def pyfat(image: bytes) -> Any:
    volume = PyFat()
    volume.set_fp(io.BufferedReader(io.BytesIO(image)))
    return volume


def every_entry(volume: Any) -> Iterator[Any]:
    """Every short-name entry pyfatfs finds, the label and "." and ".."
    included."""
    stack = [volume.root_dir]
    while stack:
        directory = stack.pop()
        folders, found, special = directory.get_entries()
        yield from [*folders, *found, *special]
        stack.extend(folders)


def test_jig_ships_fat() -> None:
    assert formats.get("fat").name == "fat"


NO_PYFILESYSTEM = """
import sys
from pathlib import Path

# A module set to None in sys.modules raises ModuleNotFoundError on import.
# pyfatfs's PyFatFS imports PyFilesystem2 (fs), whose __init__ imports
# pkg_resources, which setuptools 81 and later don't ship.
sys.modules["pkg_resources"] = None
sys.modules["fs"] = None

from jig.storage import formats

fat = formats.get("fat")
folder = Path(sys.argv[1])
arguments = fat.arguments({}, 1 << 20)
image = fat.build(folder, 1 << 20, arguments)
assert fat.open(image, arguments).read("/hello.txt") == b"hi"
assert fat.check(image, arguments) == []
assert "pyfatfs.PyFatFS" not in sys.modules
print("built and read without pkg_resources or PyFilesystem2")
"""


def test_fat_needs_neither_pkg_resources_nor_pyfilesystem2(tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "folder", {"hello.txt": b"hi"})
    result = subprocess.run(
        [sys.executable, "-c", NO_PYFILESYSTEM, str(folder)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "built and read without pkg_resources or PyFilesystem2"


@pytest.mark.parametrize(
    ("size", "fat_type", "cluster_size"),
    [
        (64 * KIB, 12, 512),
        (MIB, 12, 512),
        (4 * MIB, 12, 1024),  # 8192 sectors: FAT12 up to the specification's 8400
        (5 * MIB, 16, 1024),
        (32 * MIB, 16, 2048),  # DskTableFAT16: up to 262144 sectors, 2 KiB clusters
        (512 * MIB, 16, 8192),
        (513 * MIB, 32, 4096),  # DskTableFAT32: up to 16777216 sectors, 4 KiB clusters
    ],
)
def test_the_fat_type_and_cluster_size_follow_the_size(
    fat: Fat, size: int, fat_type: int, cluster_size: int
) -> None:
    arguments = fat.arguments({}, size)
    assert (arguments["fat_type"], arguments["cluster_size"]) == (fat_type, cluster_size)
    assert arguments["sector_count"] == size // 512


def test_the_other_arguments_default(fat: Fat) -> None:
    assert fat.arguments({"fat_type": 32}, 64 * MIB) == {
        "fat_type": 32,
        "sector_size": 512,
        "sector_count": 131072,
        "cluster_size": 512,
        "label": "NO NAME",
        "volume_id": 0,
        "time": "1980-01-01T00:00:00",
    }


def test_given_arguments_are_kept_and_complete_to_themselves(fat: Fat) -> None:
    given = {
        "fat_type": 16,
        "sector_size": 1024,
        "cluster_size": 4096,
        "label": "sensor log",
        "volume_id": 0x1234_ABCD,
        "time": "2026-11-10 12:34:57+05:00",
    }
    arguments = fat.arguments(given, 64 * MIB)
    assert arguments["label"] == "SENSOR LOG"
    assert arguments["time"] == "2026-11-10T12:34:57"
    assert arguments["sector_count"] == 65536
    assert fat.arguments(arguments, 64 * MIB) == arguments
    assert fat.arguments({"time": datetime(2000, 1, 2, 3, 4, 5, 6)}, MIB)["time"] == (
        "2000-01-02T03:04:05"
    )


def test_a_cluster_size_alone_decides_the_fat_type(fat: Fat) -> None:
    assert fat.arguments({"cluster_size": 512}, 8 * MIB)["fat_type"] == 16
    assert fat.arguments({"cluster_size": 4 * KIB}, 8 * MIB)["fat_type"] == 12
    assert fat.arguments({"cluster_size": 512}, 64 * MIB)["fat_type"] == 32


@pytest.mark.parametrize(
    ("given", "size", "message"),
    [
        ({"fat_type": 32}, 16 * MIB, "FAT32 needs at least 65526 clusters, and 32768 sectors"),
        ({"fat_type": 16}, MIB, "FAT16 needs at least 4086 clusters"),
        ({"fat_type": 12}, 1024 * MIB, "FAT12 holds at most 4084 clusters"),
        ({"fat_type": 16, "cluster_size": 32 * KIB}, 64 * MIB, "FAT16 takes 4086 to 65524"),
        ({"fat_type": 24}, MIB, "fat_type is 12, 16 or 32"),
        ({"fat_type": 16.0}, MIB, "fat_type is 12, 16 or 32"),
        ({"fat_type": True}, MIB, "fat_type is 12, 16 or 32"),
        ({"cluster_size": 64 * KIB}, 16 * KIB, "no FAT type holds 32 sectors"),
        ({}, 8 * KIB, "no FAT type holds 16 sectors"),
        ({"sector_size": 768}, MIB, "sector_size is 512, 1024, 2048 or 4096"),
        ({"sector_size": 8192}, MIB, "sector_size is 512 to 4096"),
        ({"cluster_size": 3000}, MIB, "cluster_size is 512 bytes times a power of two"),
        ({"sector_size": 2048, "cluster_size": 1024}, MIB, "cluster_size is 2048 to 262144"),
        ({"sector_count": 2049}, MIB, "past the partition's 1048576"),
        ({"label": "TWELVE CHARS"}, MIB, "label is 1 to 11"),
        ({"label": " LEADING"}, MIB, "not starting with a space"),
        ({"label": "A.B"}, MIB, "label is 1 to 11"),
        ({"label": 7}, MIB, "label is a string"),
        ({"sector_size": "512"}, MIB, "sector_size is a whole number"),
        ({"volume_id": -1}, MIB, "volume_id is 0 to 4294967295"),
        ({"volume_id": 2**32}, MIB, "volume_id is 0 to 4294967295"),
        ({"time": "yesterday"}, MIB, "time is a date and time"),
        ({"time": 0}, MIB, "time is a date and time"),
        ({"time": "1979-12-31T23:59:59"}, MIB, "time is from 1980-01-01T00:00:00"),
        ({"time": "2108-01-01"}, MIB, "to 2107-12-31T23:59:59"),
        ({"type": 16}, MIB, "fat takes fat_type, .* not type"),
    ],
)
def test_arguments_the_firmware_would_refuse(
    fat: Fat, given: dict[str, Any], size: int, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        fat.arguments(given, size)


@pytest.mark.parametrize(
    ("given", "size"),
    [
        ({"fat_type": 12}, MIB),
        ({"fat_type": 12, "cluster_size": 4096}, 4 * MIB),
        ({"fat_type": 16}, 8 * MIB),
        ({"fat_type": 16, "sector_size": 4096, "cluster_size": 4096}, 32 * MIB),
        ({"fat_type": 32}, 40 * MIB),
        ({"fat_type": 32, "sector_size": 1024, "cluster_size": 1024}, 80 * MIB),
        ({"fat_type": 16, "sector_count": 12000, "label": "DATA"}, 8 * MIB),
    ],
)
def test_a_folder_round_trips(fat: Fat, tree: Path, given: dict[str, Any], size: int) -> None:
    image, arguments = built(fat, tree, size, **given)
    assert len(image) == size
    assert view_tree(fat.open(image, arguments)) == host_tree(tree)
    assert fat.check(image, arguments) == []
    volume = pyfat(image)
    assert volume.fat_type == given["fat_type"]
    assert volume.bytes_per_cluster == arguments["cluster_size"]


@pytest.mark.parametrize(("fat_type", "size"), [(12, MIB), (16, 8 * MIB), (32, 40 * MIB)])
@pytest.mark.parametrize("label", ["NO NAME", "EMPTY"])
def test_an_empty_folder_round_trips(
    fat: Fat, tmp_path: Path, fat_type: int, size: int, label: str
) -> None:
    folder = make_tree(tmp_path / "empty", {})
    image, arguments = built(fat, folder, size, fat_type=fat_type, label=label)
    assert fat.open(image, arguments).walk() == []
    volume = pyfat(image)
    if fat_type == 32:
        assert volume.fat[2] == 0x0FFF_FFFF  # the root directory's one cluster
    specials = volume.root_dir.get_entries()[2]
    assert [entry.get_short_name() for entry in specials] == ([] if label == "NO NAME" else [label])


def test_long_short_and_lower_case_names(fat: Fat, tree: Path) -> None:
    image, arguments = built(fat, tree, MIB)
    view = fat.open(image, arguments)
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
    names = {
        entry.get_short_name(): entry.lfn_entry for entry in pyfat(image).root_dir.get_entries()[1]
    }
    assert names["README.TXT"] is None  # already a short name: no long-name entries
    assert str(names["NOTES.TXT"]) == "notes.txt"
    assert str(names["AFILEN~1.DAT"]) == "A file name longer than eight.dat"
    # FAT ignores case, and so does the view.
    assert view.read("/CONFIG/Settings.JSON") == b'{"rate": 100}\n'
    assert view.stat("/readme.txt").path == "/README.TXT"
    assert view.exists("/DATA/TWO/DEEP/DEEPER/LEAF.TXT")
    assert view.files("/Logs") == []


def test_numeric_tails_keep_short_names_apart(fat: Fat, tmp_path: Path) -> None:
    layout = {
        # First: on NTFS, LONGFI~1.TXT made later would open longfilename1.txt
        # through the short name NTFS gives it.
        "LONGFI~1.TXT": b"taken",
        "longfilename1.txt": b"1",
        "longfilename2.txt": b"2",
        "a+b.txt": b"plus",
        "two.dots.tar.gz": b"dots",
        ".hidden": b"dot",
        "über.bin": b"umlaut",
        "日本.txt": b"not in code page 437",
    }
    folder = make_tree(tmp_path / "names", layout)
    image, arguments = built(fat, folder, MIB)
    shorts = {
        str(entry.lfn_entry or entry.get_short_name()): entry.get_short_name()
        for entry in pyfat(image).root_dir.get_entries()[1]
    }
    assert shorts == {
        "LONGFI~1.TXT": "LONGFI~1.TXT",
        "longfilename1.txt": "LONGFI~2.TXT",
        "longfilename2.txt": "LONGFI~3.TXT",
        "a+b.txt": "A_B~1.TXT",
        "two.dots.tar.gz": "TWO~1.GZ",
        ".hidden": "HIDDEN~1",
        "über.bin": "ÜBER.BIN",
        "日本.txt": "__~1.TXT",
    }
    assert view_tree(fat.open(image, arguments)) == host_tree(folder)


def test_names_at_the_length_limits(fat: Fat, tmp_path: Path) -> None:
    longest = "x" * 251 + ".bin"
    try:
        folder = make_tree(
            tmp_path / "long", {"EIGHTCHR.EXT": b"8.3", "NINECHARS.EXT": b"9", longest: b"255"}
        )
    except OSError:
        pytest.skip("this host's file system can't hold a 255-character name here")
    image, arguments = built(fat, folder, MIB)
    view = fat.open(image, arguments)
    assert view.read("/" + longest) == b"255"
    entries = {entry.get_short_name(): entry for entry in pyfat(image).root_dir.get_entries()[1]}
    assert entries["EIGHTCHR.EXT"].lfn_entry is None
    assert str(entries["NINECH~1.EXT"].lfn_entry) == "NINECHARS.EXT"
    assert len(entries["XXXXXX~1.BIN"].lfn_entry.lfn_entries) == 20  # 255 units, 13 an entry


def source(*names: str) -> list[Source]:
    return [Source(name, "/" + name, Path(name), "file", 0) for name in names]


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("x" * 256, "the name is 256 UTF-16 units long, and FAT holds 255"),
        ("a:b", "a FAT name can't hold ':'"),
        ("tab\there", "a FAT name can't hold '\\\\t'"),
        ("trailing.", "can't start or end with a space or end with a period"),
        (" leading", "can't start or end with a space"),
        ("\udcff.bin", "not valid Unicode"),
    ],
)
def test_names_fat_cannot_hold(name: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        _names(source(name), "/")


def test_names_that_differ_only_in_case_are_one_name() -> None:
    with pytest.raises(ConfigError, match=r"'Data\.bin' and 'data\.BIN' are one name to FAT"):
        _names(source("Data.bin", "data.BIN"), "/logs")


def test_the_basis_name_follows_the_specification() -> None:
    assert _basis("The quick brown.fox") == ("THEQUICK", "FOX", False)
    assert _basis("...dots") == ("DOTS", "", False)
    assert _basis("a[1]=2") == ("A_1__2", "", True)
    assert _basis("π.txt") == ("_", "TXT", True)


def test_two_builds_of_one_folder_give_the_same_bytes(fat: Fat, tree: Path, tmp_path: Path) -> None:
    first, arguments = built(fat, tree, 4 * MIB, fat_type=16)
    copy = shutil.copytree(tree, tmp_path / "elsewhere" / "copy")
    assert fat.build(tree, 4 * MIB, arguments) == first
    assert fat.build(copy, 4 * MIB, arguments) == first


def test_every_entry_carries_the_time(fat: Fat, tree: Path) -> None:
    for fat_type, size in ((12, MIB), (16, 8 * MIB), (32, 40 * MIB)):
        image, _ = built(
            fat, tree, size, fat_type=fat_type, label="STAMPED", time="2026-11-10T12:34:57"
        )
        entries = list(every_entry(pyfat(image)))
        names = {entry.get_short_name() for entry in entries}
        assert {".", "..", "STAMPED", "AFILEN~1.DAT", "SETTIN~1.JSO"} <= names
        for entry in entries:
            assert entry.get_mtime() == datetime(2026, 11, 10, 12, 34, 56)
            assert entry.get_ctime() == datetime(2026, 11, 10, 12, 34, 56)
            assert entry.crttimetenth == 100  # the odd second
            assert entry.lstaccessdate == entry.wrtdate


def test_unused_clusters_hold_the_erased_value(fat: Fat, tree: Path) -> None:
    arguments = fat.arguments({"fat_type": 16, "sector_count": 12000}, 8 * MIB)
    for erased in (0xFF, 0x00, 0xA5):
        image = fat.build(tree, 8 * MIB, arguments, erased)
        volume = pyfat(image)
        # The FATs and the root directory are zeros past what Jig wrote: a FAT
        # entry of 0 marks a free cluster, and a directory entry that starts
        # with 0 ends the directory. Read as erased flash (0xFF), the root
        # would be full and every cluster taken.
        first_data = volume.first_data_sector * 512
        root = volume.root_dir_sector * 512
        end = next(at for at in range(root, first_data, 32) if image[at] == 0)
        assert end < root + 32 * 20 and image[end:first_data] == bytes(first_data - end)
        fat_end = root  # the second FAT ends where the root directory starts
        used = 2 * len([value for value in volume.fat if value])  # bytes of FAT16 entries in use
        assert image[512 + used : 512 + volume._fat_size * 512] == bytes(
            volume._fat_size * 512 - used
        )
        assert image[fat_end - 512 : fat_end] == bytes(512)
        size = volume.bytes_per_cluster
        clusters = (12000 - volume.first_data_sector) * 512 // size
        last = volume.get_data_cluster_address(clusters + 1)  # clusters count from 2
        assert image[last : last + size] == bytes([erased]) * size
        assert image[12000 * 512 :] == bytes([erased]) * (8 * MIB - 12000 * 512)
        assert view_tree(fat.open(image, arguments)) == host_tree(tree)
    with pytest.raises(ConfigError, match="an erased value is a byte"):
        fat.build(tree, 8 * MIB, arguments, -1)


def test_the_boot_sector_and_fsinfo(fat: Fat, tree: Path) -> None:
    image, _ = built(fat, tree, 40 * MIB, fat_type=32, label="BOOT", volume_id=0xCAFE)
    assert image[0:3] == b"\xeb\x58\x90" and image[510:512] == b"\x55\xaa"
    assert image[6 * 512 : 7 * 512] == image[0:512]  # BPB_BkBootSec
    assert struct.unpack_from("<I", image, 64 + 3)[0] == 0xCAFE  # BS_VolID
    assert image[64 + 7 : 64 + 18] == b"BOOT       "
    assert image[64 + 18 : 64 + 26] == b"FAT32   "
    (lead,) = struct.unpack_from("<I", image, 512)
    signature, free, following = struct.unpack_from("<III", image, 512 + 484)
    assert (lead, signature) == (0x4161_5252, 0x6141_7272)
    volume = pyfat(image)
    in_use = sum(1 for value in volume.fat[2:] if value)
    clusters = 81920 - 32 - 2 * volume._fat_size  # 512-byte clusters: one a sector
    assert free == clusters - in_use
    assert following == in_use + 2
    assert image[7 * 512 : 8 * 512] == image[512:1024]


def test_a_folder_too_big_for_the_volume_says_what_it_needs(fat: Fat, tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "full", files(20, 9, (30_000,)))
    arguments = fat.arguments({}, 256 * KIB)
    with pytest.raises(ConfigError) as raised:
        fat.build(folder, 256 * KIB, arguments)
    message = str(raised.value)
    # 20 files of 59 clusters and 10 folders of one; 512 sectors less the
    # boot sector, two 2-sector FATs and 32 sectors of root directory.
    assert "needs 1190 clusters of 512 bytes (609280 bytes)" in message
    assert "the volume has 475 (243200 bytes)" in message


def test_the_root_directory_of_fat16_holds_512_entries(fat: Fat, tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "wide", {f"a long name {n:03}.txt": b"" for n in range(200)})
    with pytest.raises(
        ConfigError, match="holds 512 entries, and the folder's top level needs 600"
    ):
        built(fat, folder, 8 * MIB, fat_type=16)
    image, arguments = built(fat, folder, 40 * MIB, fat_type=32)
    assert view_tree(fat.open(image, arguments)) == host_tree(folder)


def test_check_names_each_part_that_differs(fat: Fat, tree: Path) -> None:
    image, _ = built(fat, tree, 4 * MIB, fat_type=16, sector_count=8000)
    assert fat.check(image, fat.arguments({}, 4 * MIB)) == [
        "sector_count: the boot sector says 8000, the arguments say 8192",
        "cluster_size: the boot sector says 512, the arguments say 1024",
        "fat_type: the boot sector says 16, the arguments say 12",
    ]
    image, _ = built(fat, tree, 4 * MIB, sector_size=1024)
    assert fat.check(image, fat.arguments({}, 4 * MIB)) == [
        "sector_size: the boot sector says 1024, the arguments say 512",
        "sector_count: the boot sector says 4096, the arguments say 8192",
    ]


def test_check_of_bytes_that_are_no_fat(fat: Fat) -> None:
    arguments = fat.arguments({}, MIB)
    assert fat.check(b"\xff" * MIB, arguments) == [
        "no FAT boot sector: bytes 510 and 511 hold ff ff, not 55 aa"
    ]
    assert fat.check(b"\x55\xaa", arguments) == [
        "the image is 2 bytes, too short to hold a FAT boot sector"
    ]
    junk = bytearray(MIB)
    junk[510:512] = b"\x55\xaa"
    assert fat.check(bytes(junk), arguments)[0].startswith("not a FAT boot sector")


def test_check_names_a_cluster_count_readers_disagree_on(fat: Fat, tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "empty", {})
    image = bytearray(fat.build(folder, 4 * MIB, fat.arguments({"fat_type": 16}, 4 * MIB)))
    # 4085 clusters: shrink the volume until the count is the one in dispute.
    struct.pack_into("<H", image, 19, 1 + 2 * 32 + 32 + 4085)
    problems = fat.check(bytes(image), fat.arguments({"fat_type": 16}, 4 * MIB))
    assert problems[0] == (
        "the volume has 4085 clusters, which the FAT specification and FatFs read as "
        "different FAT types"
    )


def test_check_names_a_fat32_count_under_a_fat16_boot_sector(fat: Fat, tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "empty", {})
    arguments = fat.arguments({"fat_type": 32}, 40 * MIB)
    image = bytearray(fat.build(folder, 40 * MIB, arguments))
    struct.pack_into("<H", image, 17, 512)  # BPB_RootEntCnt, which FAT32 keeps at 0
    (fat_sectors,) = struct.unpack_from("<I", image, 36)  # BPB_FATSz32
    clusters = 81920 - 32 - 2 * fat_sectors - 32  # the root directory now takes 32 sectors
    assert fat.check(bytes(image), arguments)[0] == (
        f"the volume has {clusters} clusters, FAT32, but BPB_FATSz16 is 0 and BPB_RootEntCnt is 512"
    )


def test_check_of_a_boot_sector_with_no_reserved_sectors(fat: Fat, tree: Path) -> None:
    image, arguments = built(fat, tree, MIB)
    damaged = bytearray(image)
    struct.pack_into("<H", damaged, 14, 0)  # BPB_RsvdSecCnt
    assert fat.check(bytes(damaged), arguments) == [
        "not a FAT boot sector: 2048 sectors, 0 reserved, 2 FATs of 6 sectors, 512 root entries"
    ]


def test_check_takes_completed_arguments(fat: Fat) -> None:
    with pytest.raises(ConfigError, match="with sector_size and sector_count"):
        fat.check(b"", {"fat_type": 12})


def test_open_reads_the_volume_from_its_own_boot_sector(fat: Fat, tree: Path) -> None:
    image, _ = built(fat, tree, 8 * MIB, fat_type=16)
    other = fat.arguments({"fat_type": 12}, 8 * MIB)
    assert view_tree(fat.open(image, other)) == host_tree(tree)
    with pytest.raises(ConfigError, match="can't read the image as a FAT volume"):
        fat.open(b"\xff" * MIB, other)


def test_open_shows_lower_case_short_names(fat: Fat, tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "case", {"README.TXT": b"r", "LOG.TXT": b"l", "MAKEFILE": b"m"})
    image = bytearray(fat.build(folder, MIB, fat.arguments({}, MIB)))
    volume = pyfat(bytes(image))
    root = volume.root_dir_sector * 512
    for index in range(3):  # set DIR_NTRes as Windows NT and FatFs do for lower-case names
        name = image[root + 32 * index : root + 32 * index + 11]
        image[root + 32 * index + 12] = {b"LOG     TXT": 0x18, b"MAKEFILE   ": 0x08}.get(
            bytes(name), 0x10
        )
    view = fat.open(bytes(image), fat.arguments({}, MIB))
    assert [entry.name for entry in view.files()] == ["README.txt", "log.txt", "makefile"]
    assert view.read("/Log.Txt") == b"l"


def test_open_reads_what_a_firmware_changed(fat: Fat, tree: Path) -> None:
    image, arguments = built(fat, tree, MIB)
    volume = pyfat(image)
    entry = volume.root_dir.get_entry("notes.txt")
    start = volume.get_data_cluster_address(entry.get_cluster())
    changed = image[:start] + b"LOWER CASE\n" + image[start + 11 :]
    assert fat.open(changed, arguments).read("/notes.txt") == b"LOWER CASE\n"


def test_a_damaged_cluster_chain_is_an_error(fat: Fat, tree: Path) -> None:
    image, arguments = built(fat, tree, MIB, fat_type=12)
    volume = pyfat(image)
    first = volume.root_dir.get_entry("exact.bin").get_cluster()
    damaged = bytearray(image)
    # FAT12 packs an entry in 12 bits; both bytes it touches go to 0, which
    # marks the file's first cluster free.
    offset = 512 + first * 3 // 2
    damaged[offset : offset + 2] = b"\x00\x00"
    with pytest.raises(ConfigError, match=r"can't read /exact\.bin"):
        fat.open(bytes(damaged), arguments).read("/exact.bin")


def entry_offset(image: bytes, folder: str, short: bytes) -> int:
    """The byte where the entry of the short name ``short`` (11 bytes, as
    DIR_Name holds it) starts in ``folder``'s first cluster."""
    volume = pyfat(image)
    start = volume.get_data_cluster_address(volume.root_dir.get_entry(folder).get_cluster())
    for offset in range(start, start + volume.bytes_per_cluster, 32):
        if image[offset : offset + 11] == short:
            return offset
    raise AssertionError(f"no entry {short!r} in {folder}")


def test_a_directory_that_loops_back_is_listed_empty(fat: Fat, tree: Path) -> None:
    image, arguments = built(fat, tree, MIB)
    data = pyfat(image).root_dir.get_entry("data").get_cluster()
    offset = entry_offset(image, "/data", b"ONE        ")
    damaged = bytearray(image)
    struct.pack_into("<H", damaged, offset + 26, data)  # DIR_FstClusLO of /data/one: /data
    view = fat.open(bytes(damaged), arguments)
    assert [entry.name for entry in view.files("/data")] == ["big.bin", "one", "two"]
    assert view.files("/data/one") == []


def test_a_file_whose_clusters_are_short_is_an_error(fat: Fat, tree: Path) -> None:
    image, arguments = built(fat, tree, MIB)
    offset = entry_offset(image, "/config", b"SETTIN~1JSO")  # settings.json
    assert struct.unpack_from("<I", image, offset + 28)[0] == 14
    for field, value, message in (
        (28, 5000, "is 5000 bytes, and its clusters hold 512"),  # DIR_FileSize
        (26, 0, "its first cluster is 0"),  # DIR_FstClusLO
    ):
        damaged = bytearray(image)
        struct.pack_into("<I" if field == 28 else "<H", damaged, offset + field, value)
        with pytest.raises(ConfigError, match=message):
            fat.open(bytes(damaged), arguments).read("/config/settings.json")


def test_a_directory_with_more_entries_than_readers_take(
    fat: Fat, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jig.storage.formats.fat.DIRECTORY_MAX", 10)
    folder = make_tree(tmp_path / "wide", {"logs": {f"L{n}.TXT": b"" for n in range(9)}})
    with pytest.raises(ConfigError, match="/logs needs 11 directory entries"):
        built(fat, folder, MIB)


def test_a_file_fat_cannot_hold(fat: Fat, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = make_tree(tmp_path / "big", {"big.bin": b"x"})
    real = Path.stat

    def huge(path: Path, **options: Any) -> Any:
        found = real(path, **options)
        if path.name != "big.bin":
            return found
        values = list(found)
        values[6] = 2**32  # st_size
        return type(found)(values)

    monkeypatch.setattr(Path, "stat", huge)
    with pytest.raises(ConfigError, match="a FAT file holds at most 4 GiB less one byte"):
        built(fat, folder, 64 * MIB)


def test_files_in_a_200_file_folder_round_trip(fat: Fat, tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "many", files(200, 5, (0, 10, 100, 1000, 4096, 10_000)))
    for size, given in ((4 * MIB, {}), (40 * MIB, {"fat_type": 32})):
        image, arguments = built(fat, folder, size, **given)
        assert view_tree(fat.open(image, arguments)) == host_tree(folder)
