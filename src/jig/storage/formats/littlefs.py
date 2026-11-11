"""littlefs volumes, built and mounted with littlefs-python 0.19 (littlefs
2.11, on-disk versions 2.0 and 2.1).

A partition's arguments repeat the firmware's ``lfs_config``. The build
formats the volume and writes the folder's directories and files in name
order, so one folder and one set of arguments give the same bytes every
time. littlefs-python compiles littlefs with its assertions on, and a failed
assertion ends the Python process, so ``arguments`` refuses every
configuration that ``lfs_init`` asserts against before littlefs sees it.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from littlefs import LFSStat, LittleFS, LittleFSError, UserContext, lfs

from jig.errors import ConfigError
from jig.storage.formats import ERASED, Entry, FileSystemView
from jig.storage.formats._tree import Source, TreeView, read_folder

ARGUMENTS = (
    "block_size",
    "block_count",
    "read_size",
    "prog_size",
    "name_max",
    "file_max",
    "attr_max",
    "disk_version",
)

# littlefs's own limits (lfs.h: LFS_NAME_MAX, LFS_FILE_MAX, LFS_ATTR_MAX), and
# the defaults: a firmware with smaller limits refuses a volume made with
# larger ones.
NAME_MAX = 255
FILE_MAX = 2_147_483_647
ATTR_MAX = 1022

# lfs_init asserts that a block holds every pointer of a file's skip-list,
# which takes at least 128 bytes; littlefs-python refuses smaller blocks.
SMALLEST_BLOCK = 128

# On-disk versions, as lfs_config.disk_version encodes them. littlefs 2.6
# and later write 2.1; older releases read only 2.0.
VERSIONS = {"2.0": 0x0002_0000, "2.1": 0x0002_0001}

# The superblock entry starts every metadata block that holds it: a 4-byte
# revision count, the name tag, "littlefs", the struct tag, then six
# little-endian words (littlefs SPEC.md, "Superblock").
MAGIC = b"littlefs"
SUPERBLOCK = struct.Struct("<6I")
SUPERBLOCK_FIELDS = (
    "disk_version",
    "block_size",
    "block_count",
    "name_max",
    "file_max",
    "attr_max",
)
TYPE_SUPERBLOCK = 0x0FF
TYPE_INLINESTRUCT = 0x201


class LittleFs:
    """``littlefs(block_size:, block_count:, read_size:, prog_size:,
    name_max:, file_max:, attr_max:, disk_version:)``."""

    name = "littlefs"

    def arguments(self, given: Mapping[str, Any], size: int) -> dict[str, Any]:
        """The firmware's ``lfs_config`` values. ``block_size`` is required;
        ``size`` is a whole number of blocks. ``block_count`` defaults to the
        blocks in the partition. ``read_size`` and ``prog_size`` default to
        ``block_size``, a multiple of any firmware's program size. The limits
        default to littlefs's own, and ``disk_version`` to 2.1."""
        unknown = sorted(set(given) - set(ARGUMENTS))
        if unknown:
            raise ConfigError(f"littlefs takes {', '.join(ARGUMENTS)}, not {', '.join(unknown)}")
        if "block_size" not in given:
            raise ConfigError("littlefs needs block_size, the firmware's lfs_config.block_size")
        block_size = _whole(given, "block_size", None, SMALLEST_BLOCK)
        if size % block_size:
            raise ConfigError(
                f"littlefs: the partition's {size} bytes are not a whole number of "
                f"{block_size}-byte blocks"
            )
        if "block_count" not in given and size // block_size < 2:
            raise ConfigError(
                f"littlefs: the partition holds {size // block_size} blocks of {block_size} "
                "bytes, and littlefs needs 2 for its superblock"
            )
        block_count = _whole(given, "block_count", size // block_size, 2)
        if block_count * block_size > size:
            raise ConfigError(
                f"littlefs: block_count {block_count} of {block_size}-byte blocks is "
                f"{block_count * block_size} bytes, past the partition's {size}"
            )
        read_size = _whole(given, "read_size", block_size, 1)
        prog_size = _whole(given, "prog_size", block_size, 1)
        if block_size % math.lcm(read_size, prog_size):
            raise ConfigError(
                f"littlefs: block_size {block_size} is not a multiple of both "
                f"read_size {read_size} and prog_size {prog_size}"
            )
        return {
            "block_size": block_size,
            "block_count": block_count,
            "read_size": read_size,
            "prog_size": prog_size,
            "name_max": _whole(given, "name_max", NAME_MAX, 1, NAME_MAX),
            "file_max": _whole(given, "file_max", FILE_MAX, 1, FILE_MAX),
            "attr_max": _whole(given, "attr_max", ATTR_MAX, 1, ATTR_MAX),
            "disk_version": _version(given.get("disk_version", "2.1")),
        }

    def build(
        self, folder: Path, size: int, arguments: Mapping[str, Any], erased: int = ERASED
    ) -> bytes:
        """The folder as a littlefs volume of ``block_count`` blocks, then
        ``erased`` to ``size``. Blocks littlefs never writes hold ``erased``,
        and erasing a block fills it with ``erased``, as the device does."""
        settings = _Settings.of(self.arguments(arguments, size))
        _byte(erased)
        tree = read_folder(folder)
        settings.fits(tree)
        try:
            volume = _write(tree, settings, settings.block_count, erased)
        except _NoSpaceError:
            raise ConfigError(_too_big(folder, tree, settings)) from None
        return bytes(volume.context.buffer) + bytes([erased]) * (size - len(volume.context.buffer))

    def check(self, image: bytes, arguments: Mapping[str, Any]) -> list[str]:
        """Each superblock field of a prebuilt image that differs from the
        arguments (``arguments`` as ``arguments()`` returns them): the disk
        version, the block size and count, and the three limits."""
        settings = _Settings.of(arguments)
        found = _superblock(image, settings.block_size)
        if found is None:
            return [
                "no littlefs superblock: neither of the first two blocks starts "
                f'with one (the name "{MAGIC.decode()}" at byte 8)'
            ]
        problems = []
        try:
            found = _mounted_superblock(image, found)
        except (LittleFSError, ValueError) as error:
            problems.append(
                f"littlefs can't mount the image with the superblock's own block_size "
                f"{found['block_size']} and block_count {found['block_count']} ({error})"
            )
        wanted = settings.values()
        for field in SUPERBLOCK_FIELDS:
            if found[field] != wanted[field]:
                problems.append(
                    f"{field}: the superblock says {found[field]}, "
                    f"the arguments say {wanted[field]}"
                )
        return problems

    def open(self, image: bytes, arguments: Mapping[str, Any]) -> FileSystemView:
        """The volume, mounted as the firmware mounts it: with the
        arguments' configuration, which must match the superblock. Jig
        mounts a copy of the image and writes nothing to it."""
        settings = _Settings.of(arguments)
        size = settings.block_size * settings.block_count
        volume = LittleFS(
            context=_Blocks(bytearray(image[:size]), ERASED), mount=False, **settings.config()
        )
        try:
            volume.mount()
        except LittleFSError as error:
            problems = self.check(image, arguments) or [str(error)]
            raise ConfigError(
                f"littlefs can't mount the image with the partition's arguments: "
                f"{'; '.join(problems)}"
            ) from error
        try:
            entries = _listing(volume)
        except (LittleFSError, UnicodeDecodeError) as error:  # a name that isn't UTF-8
            raise ConfigError(f"littlefs can't list the image: {error}") from error
        return TreeView(entries, lambda entry: _contents(volume, entry))


@dataclass(frozen=True)
class _Settings:
    block_size: int
    block_count: int
    read_size: int
    prog_size: int
    name_max: int
    file_max: int
    attr_max: int
    disk_version: str

    @staticmethod
    def of(arguments: Mapping[str, Any]) -> _Settings:
        """The settings in completed arguments, checked again: the size they
        are checked against is the size of their own blocks."""
        if "block_size" not in arguments or "block_count" not in arguments:
            raise ConfigError(
                "littlefs: check and open take the arguments that arguments() returns, "
                "with block_size and block_count"
            )
        block_size = _whole(arguments, "block_size", None, SMALLEST_BLOCK)
        block_count = _whole(arguments, "block_count", None, 2)
        values = LittleFs().arguments(arguments, block_size * block_count)
        return _Settings(**values)

    def values(self) -> dict[str, Any]:
        return {
            "disk_version": self.disk_version,
            "block_size": self.block_size,
            "block_count": self.block_count,
            "name_max": self.name_max,
            "file_max": self.file_max,
            "attr_max": self.attr_max,
        }

    def config(self, block_count: int | None = None) -> dict[str, Any]:
        """littlefs-python's configuration. Its cache is the smallest that
        ``lfs_init`` accepts: a multiple of both the read and program sizes.
        The cache size bounds the files littlefs keeps inside the metadata,
        and a firmware reads those whatever its own cache size."""
        return {
            "block_size": self.block_size,
            "block_count": self.block_count if block_count is None else block_count,
            "read_size": self.read_size,
            "prog_size": self.prog_size,
            "cache_size": math.lcm(self.read_size, self.prog_size),
            "name_max": self.name_max,
            "file_max": self.file_max,
            "attr_max": self.attr_max,
            "disk_version": VERSIONS[self.disk_version],
            "block_cycles": -1,
        }

    def fits(self, tree: Source) -> None:
        """Refuse names and files the limits don't hold, which littlefs
        would refuse halfway through the build."""
        for item in tree.walk():
            try:
                length = len(item.name.encode("utf-8"))
            except UnicodeEncodeError:
                raise ConfigError(f"littlefs: the name {item.host} is not valid UTF-8") from None
            if length > self.name_max:
                raise ConfigError(
                    f"littlefs: {item.path}: the name takes {length} bytes of UTF-8, "
                    f"past name_max {self.name_max}"
                )
            if item.size > self.file_max:
                raise ConfigError(
                    f"littlefs: {item.path} is {item.size} bytes, past file_max {self.file_max}"
                )


class _Blocks(UserContext):
    """littlefs's block device: an image in memory. Erasing fills a block
    with ``erased``; a read past the image's end returns ``erased``."""

    def __init__(self, image: bytearray, erased: int) -> None:
        super().__init__(buffer=image)
        self.erased = erased

    def read(self, cfg: lfs.LFSConfig, block: int, off: int, size: int) -> bytearray:
        start = block * cfg.block_size + off
        data = self.buffer[start : start + size]
        if len(data) < size:
            data += bytes([self.erased]) * (size - len(data))
        return data

    def prog(self, cfg: lfs.LFSConfig, block: int, off: int, data: bytes) -> int:
        start = block * cfg.block_size + off
        self.buffer[start : start + len(data)] = data
        return 0

    def erase(self, cfg: lfs.LFSConfig, block: int) -> int:
        start = block * cfg.block_size
        self.buffer[start : start + cfg.block_size] = bytes([self.erased]) * cfg.block_size
        return 0

    def sync(self, cfg: lfs.LFSConfig) -> int:
        return 0


class _NoSpaceError(Exception):
    """The volume filled before the folder was written."""


def _write(tree: Source, settings: _Settings, block_count: int, erased: int) -> LittleFS:
    """A volume of ``block_count`` blocks holding the folder, unmounted."""
    blocks = _Blocks(bytearray([erased]) * (settings.block_size * block_count), erased)
    volume = LittleFS(context=blocks, mount=False, **settings.config(block_count))
    where = "/"
    try:
        volume.format()
        volume.mount()
        for item in tree.walk():
            where = item.path
            if item.kind == "dir":
                volume.mkdir(item.path)
                continue
            data = item.read()
            handle = volume.open(item.path, "wb", buffering=0)
            try:
                written = handle.write(data)
            finally:
                handle.close()
            if written != len(data):
                raise ConfigError(f"littlefs: {where}: wrote {written} of {len(data)} bytes")
        volume.unmount()
    except LittleFSError as error:
        if error.code == LittleFSError.Error.LFS_ERR_NOSPC:
            raise _NoSpaceError from error
        raise ConfigError(f"littlefs: {where}: {error}") from error
    except OSError as error:  # littlefs-python turns some codes into OSError subclasses
        raise ConfigError(f"littlefs: {where}: {error}") from error
    return volume


def _too_big(folder: Path, tree: Source, settings: _Settings) -> str:
    """How much the folder takes: the blocks in use after building it into a
    volume with room to spare. The room is a guess (each file's blocks half
    full, two blocks per directory and per file for metadata), doubled up to
    three times."""
    half = settings.block_size // 2
    count = 4 + sum(2 + -(-item.size // half) if item.kind == "file" else 2 for item in tree.walk())
    count = max(count, 2 * settings.block_count)
    for _ in range(4):
        try:
            used = _write(tree, settings, count, ERASED).used_block_count
        except _NoSpaceError:
            count *= 2
            continue
        return (
            f"littlefs: the folder {folder} takes {used} blocks of {settings.block_size} "
            f"bytes ({used * settings.block_size} bytes) with these arguments, and the "
            f"partition has {settings.block_count} ({settings.block_count * settings.block_size} "
            "bytes)"
        )
    return (
        f"littlefs: the folder {folder} takes more than {count // 2} blocks of "
        f"{settings.block_size} bytes, and the partition has {settings.block_count}"
    )


def _superblock_at(image: bytes, offset: int) -> dict[str, Any] | None:
    """The superblock entry a metadata block starts with, read by offsets
    alone, or None when the block starts with none."""
    if len(image) < offset + 20 + SUPERBLOCK.size or image[offset + 8 : offset + 16] != MAGIC:
        return None
    name_tag = int.from_bytes(image[offset + 4 : offset + 8], "big") ^ 0xFFFF_FFFF
    struct_tag = int.from_bytes(image[offset + 16 : offset + 20], "big") ^ name_tag
    if name_tag >> 20 != TYPE_SUPERBLOCK or struct_tag >> 20 != TYPE_INLINESTRUCT:
        return None
    values = dict(zip(SUPERBLOCK_FIELDS, SUPERBLOCK.unpack_from(image, offset + 20), strict=True))
    values["disk_version"] = _version_name(values["disk_version"])
    return values


def _superblock(image: bytes, block_size: int) -> dict[str, Any] | None:
    """The superblock in block 0, or else in block 1, which a firmware's
    compaction can leave as the only copy. Block 1's place depends on the
    image's block size: the arguments' first, then each power of two."""
    found = _superblock_at(image, 0)
    if found is not None:
        return found
    sizes = [block_size, *(1 << shift for shift in range(7, 32))]
    for size in sizes:
        if size > len(image) // 2:
            continue
        found = _superblock_at(image, size)
        if found is not None and found["block_size"] == size:
            return found
    return None


def _mounted_superblock(image: bytes, found: dict[str, Any]) -> dict[str, Any]:
    """The superblock as littlefs mounts it, which takes later commits into
    account, using the block size and count that ``found`` names and
    littlefs's largest limits."""
    block_size, block_count = found["block_size"], found["block_count"]
    if not SMALLEST_BLOCK <= block_size <= len(image) // 2 or block_count < 2:
        raise ValueError(f"block_size {block_size} and block_count {block_count} can't be mounted")
    volume = LittleFS(
        context=_Blocks(bytearray(image), ERASED),
        mount=False,
        block_size=block_size,
        block_count=block_count,
        read_size=block_size,
        prog_size=block_size,
        cache_size=block_size,
        name_max=NAME_MAX,
        file_max=FILE_MAX,
        attr_max=ATTR_MAX,
        block_cycles=-1,
    )
    volume.mount()
    stat = volume.fs_stat()
    return {
        "disk_version": _version_name(stat.disk_version),
        "block_size": stat.block_size,
        "block_count": stat.block_count,
        "name_max": stat.name_max,
        "file_max": stat.file_max,
        "attr_max": stat.attr_max,
    }


def _listing(volume: LittleFS) -> list[Entry]:
    entries: list[Entry] = []
    folders = ["/"]
    while folders:
        folder = folders.pop()
        for stat in volume.scandir(folder):
            path = f"{folder.rstrip('/')}/{stat.name}"
            if stat.type == LFSStat.TYPE_DIR:
                entries.append(Entry(path, stat.name, "dir", 0))
                folders.append(path)
            else:
                entries.append(Entry(path, stat.name, "file", stat.size))
    return entries


def _contents(volume: LittleFS, entry: Entry) -> bytes:
    try:
        with volume.open(entry.path, "rb") as handle:
            data = handle.read()
    except (LittleFSError, OSError) as error:
        raise ConfigError(f"littlefs can't read {entry.path}: {error}") from error
    return bytes(data)


def _whole(
    given: Mapping[str, Any], key: str, default: int | None, least: int, most: int | None = None
) -> int:
    value = given.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"littlefs: {key} is a whole number, not {value!r}")
    if value < least or (most is not None and value > most):
        limits = f"at least {least}" if most is None else f"{least} to {most}"
        raise ConfigError(f"littlefs: {key} is {limits}, not {value}")
    return value


def _version(value: Any) -> str:
    """The disk version, given as the string "2.0" or "2.1", as the number
    2.0 or 2.1, or as littlefs's encoding."""
    if isinstance(value, int) and not isinstance(value, bool) and value in VERSIONS.values():
        return _version_name(value)
    if isinstance(value, float):
        value = str(value)
    if isinstance(value, str) and value.strip() in VERSIONS:
        return value.strip()
    raise ConfigError(f"littlefs: disk_version is 2.0 or 2.1, not {value!r}")


def _version_name(encoded: int) -> str:
    return f"{encoded >> 16}.{encoded & 0xFFFF}"


def _byte(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
        raise ConfigError(f"an erased value is a byte, 0 to 255, not {value!r}")
