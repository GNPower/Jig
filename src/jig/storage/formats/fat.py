"""FAT12, FAT16 and FAT32 volumes.

Jig writes the volume itself, from Microsoft's "FAT32 File System
Specification" (version 1.03, 2000-12-06, also published as fatgen103),
and reads volumes back with pyfatfs's ``PyFat``. pyfatfs 1.1 can't write
them for Jig: its file-level layer (``PyFatFS``) imports PyFilesystem2, whose
``fs`` 2.4.16 imports ``pkg_resources``, which setuptools 84.2.1 doesn't
have; and ``PyFat.mkfs`` takes no cluster size, swaps a volume ID of 0 for
one made from the clock, and stamps the label's entry with the time of the
build.

One folder and one set of arguments give the same bytes: entries are
written in name order, clusters are allocated in that order from cluster 2,
and every entry carries the timestamp ``time``.
"""

from __future__ import annotations

import contextlib
import io
import posixpath
import struct
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pyfatfs.PyFat import PyFat  # type: ignore[import-untyped]

from jig.errors import ConfigError
from jig.storage.formats import ERASED, Entry, FileSystemView
from jig.storage.formats._tree import Source, TreeView, read_folder

ARGUMENTS = (
    "fat_type",
    "sector_size",
    "sector_count",
    "cluster_size",
    "label",
    "volume_id",
    "time",
)

SECTOR_SIZES = (512, 1024, 2048, 4096)  # BPB_BytsPerSec's legal values
FATS = 2  # BPB_NumFATs, the specification's recommendation
MEDIA = 0xF8  # BPB_Media for fixed media
ROOT_ENTRIES = 512  # BPB_RootEntCnt of FAT12 and FAT16, the specification's value for FAT16
ENTRY = 32  # bytes in a directory entry
NO_LABEL = "NO NAME"  # BS_VolLab of a volume without a label
EARLIEST = datetime(1980, 1, 1)  # the first and last times a directory entry holds
LATEST = datetime(2107, 12, 31, 23, 59, 59)

# The cluster counts of each FAT type. The specification puts a volume of
# 4085 clusters in FAT16 and one of 65525 in FAT32; FatFs (ff.c, MAX_FAT12
# and MAX_FAT16) puts them in FAT12 and FAT16, so Jig makes neither.
# FAT32's largest count is FatFs's MAX_FAT32.
CLUSTERS = {12: (1, 4084), 16: (4086, 65524), 32: (65526, 0x0FFF_FFF5)}

# The specification's tables of cluster sizes (DskTableFAT16, DskTableFAT32):
# a volume of up to so many 512-byte sectors gets clusters of so many bytes.
# FAT16's table starts at 8400 sectors and is used up to 1048576 (512 MiB),
# FAT32's above that; a volume of 8400 sectors or fewer is FAT12.
FAT16_TABLE = ((32680, 1024), (262144, 2048), (524288, 4096), (1048576, 8192))
FAT32_TABLE = (
    (532480, 512),
    (16777216, 4096),
    (33554432, 8192),
    (67108864, 16384),
    (0xFFFF_FFFF, 32768),
)
FAT12_SECTORS = 8400
FAT16_SECTORS = 1048576

END = {12: 0xFFF, 16: 0xFFFF, 32: 0x0FFF_FFFF}  # end of a cluster chain, and FAT[1]
ATTR_READ_ONLY, ATTR_HIDDEN, ATTR_SYSTEM, ATTR_VOLUME_ID = 0x01, 0x02, 0x04, 0x08
ATTR_DIRECTORY, ATTR_ARCHIVE = 0x10, 0x20
ATTR_LONG_NAME = ATTR_READ_ONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_VOLUME_ID
LOWER_BASE, LOWER_EXTENSION = 0x08, 0x10  # DIR_NTRes bits Windows NT and FatFs set
LONG_NAME_MAX = 255  # UTF-16 code units in a long name
DIRECTORY_MAX = 65536  # entries FatFs reads in one directory (ff.c, MAX_DIR)

# Characters a long name can't hold, and those a short name can't hold besides.
NOT_LONG = frozenset('"*/:<>?\\|') | frozenset(chr(code) for code in range(32))
NOT_SHORT = NOT_LONG | frozenset("+,;=[]")
LABEL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !#$%&'()-@^_`{}~")


class Fat:
    """``fat(fat_type:, sector_size:, sector_count:, cluster_size:, label:,
    volume_id:, time:)``."""

    name = "fat"

    def arguments(self, given: Mapping[str, Any], size: int) -> dict[str, Any]:
        """The volume's layout and stamps. ``sector_size`` defaults to 512 and
        ``sector_count`` to the sectors in the partition. ``fat_type`` (12,
        16 or 32) defaults to the specification's choice for the volume's
        size, ``cluster_size`` to its table for FAT16 and FAT32 and to the
        smallest that fits for FAT12. ``label`` defaults to none, ``volume_id``
        to 0 and ``time`` to 1980-01-01T00:00:00."""
        unknown = sorted(set(given) - set(ARGUMENTS))
        if unknown:
            raise ConfigError(f"fat takes {', '.join(ARGUMENTS)}, not {', '.join(unknown)}")
        sector_size = _whole(given, "sector_size", 512, 512, 4096)
        if sector_size not in SECTOR_SIZES:
            raise ConfigError(f"fat: sector_size is 512, 1024, 2048 or 4096, not {sector_size}")
        sector_count = _whole(given, "sector_count", size // sector_size, 1, 0xFFFF_FFFF)
        if sector_count * sector_size > size:
            raise ConfigError(
                f"fat: sector_count {sector_count} of {sector_size}-byte sectors is "
                f"{sector_count * sector_size} bytes, past the partition's {size}"
            )
        fat_type = given.get("fat_type")
        if fat_type is not None and (
            isinstance(fat_type, bool) or not isinstance(fat_type, int) or fat_type not in CLUSTERS
        ):
            raise ConfigError(f"fat: fat_type is 12, 16 or 32, not {fat_type!r}")
        cluster_size = given.get("cluster_size")
        if cluster_size is not None:
            cluster_size = _whole(given, "cluster_size", None, sector_size, 128 * sector_size)
            if cluster_size & (cluster_size - 1) or cluster_size % sector_size:
                raise ConfigError(
                    f"fat: cluster_size is {sector_size} bytes times a power of two up to 128, "
                    f"not {cluster_size}"
                )
        layout = _choose(fat_type, sector_size, sector_count, cluster_size)
        return {
            "fat_type": layout.fat_type,
            "sector_size": sector_size,
            "sector_count": sector_count,
            "cluster_size": layout.cluster_size,
            "label": _label(given.get("label", NO_LABEL)),
            "volume_id": _whole(given, "volume_id", 0, 0, 0xFFFF_FFFF),
            "time": _time(given.get("time", EARLIEST.isoformat())).isoformat(),
        }

    def build(
        self, folder: Path, size: int, arguments: Mapping[str, Any], erased: int = ERASED
    ) -> bytes:
        """The folder as a FAT volume of ``sector_count`` sectors, then
        ``erased`` to ``size``. The reserved sectors, the FATs and the root
        directory are written in full, zeros where nothing else goes, as a
        formatter writes them: a FAT entry of 0 marks a free cluster and a
        directory entry starting with 0 ends the directory, so left erased
        (0xFF on NOR flash) they would read as a full root directory and no
        free clusters. Of the data area, Jig writes only the clusters in use;
        the rest, and a file's last cluster past its end, hold ``erased``.
        Every entry gets ``time``."""
        values = self.arguments(arguments, size)
        if isinstance(erased, bool) or not isinstance(erased, int) or not 0 <= erased <= 0xFF:
            raise ConfigError(f"an erased value is a byte, 0 to 255, not {erased!r}")
        layout = _layout(
            values["fat_type"],
            values["sector_size"],
            values["sector_count"],
            values["cluster_size"] // values["sector_size"],
        )
        label = values["label"]
        stamp = _stamp(datetime.fromisoformat(values["time"]))
        root = _plan(read_folder(folder), layout, label != NO_LABEL)
        used = _allocate(root, layout)
        if used > layout.clusters:
            raise ConfigError(
                f"fat: the folder {folder} needs {used} clusters of {layout.cluster_size} bytes "
                f"({used * layout.cluster_size} bytes), and the volume has {layout.clusters} "
                f"({layout.clusters * layout.cluster_size} bytes)"
            )
        image = bytearray([erased]) * size
        _write(image, root, layout, label, values["volume_id"], stamp, used)
        return bytes(image)

    def check(self, image: bytes, arguments: Mapping[str, Any]) -> list[str]:
        """Each part of a prebuilt image's boot sector that differs from the
        arguments (as ``arguments()`` returns them): the sector size and
        count, the cluster size and the FAT type, which the cluster count
        decides. The label, volume ID and time don't change how a firmware
        mounts the volume, so they aren't compared."""
        values = _completed(arguments)
        found = _boot_sector(image)
        if isinstance(found, str):
            return [found]
        problems = list(found.pop("problems"))
        for key, value in found.items():
            if value != values[key]:
                problems.append(
                    f"{key}: the boot sector says {value}, the arguments say {values[key]}"
                )
        return problems

    def open(self, image: bytes, arguments: Mapping[str, Any]) -> FileSystemView:
        """The volume, read with pyfatfs from its own boot sector, so a
        volume the firmware formatted again reads back too. Names compare as
        FAT compares them, ignoring case."""
        volume = PyFat(lazy_load=True)
        try:
            with warnings.catch_warnings():
                # pyfatfs warns about a volume not marked clean and FATs that
                # differ: both happen while a firmware is writing.
                warnings.simplefilter("ignore")
                volume.set_fp(io.BufferedReader(io.BytesIO(image)))  # read-only to pyfatfs
                entries, found = _listing(volume)
        except Exception as error:  # pyfatfs raises many types on a damaged volume
            raise ConfigError(f"fat: can't read the image as a FAT volume: {error}") from error
        return TreeView(
            entries, lambda entry: _contents(volume, found[entry.path.upper()], entry), str.upper
        )


@dataclass(frozen=True)
class _Layout:
    """Where the parts of a volume go, in sectors from its first byte."""

    fat_type: int
    sector_size: int
    sectors: int
    cluster_sectors: int
    reserved: int
    fat_sectors: int
    root_entries: int
    clusters: int

    @property
    def cluster_size(self) -> int:
        return self.cluster_sectors * self.sector_size

    @property
    def root_start(self) -> int:
        return self.reserved + FATS * self.fat_sectors

    @property
    def data_start(self) -> int:
        return self.root_start + self.root_entries * ENTRY // self.sector_size

    @property
    def fits(self) -> bool:
        least, most = CLUSTERS[self.fat_type]
        return least <= self.clusters <= most

    def offset(self, cluster: int) -> int:
        """The byte where a data cluster starts."""
        return (self.data_start + (cluster - 2) * self.cluster_sectors) * self.sector_size


def _layout(fat_type: int, sector_size: int, sectors: int, cluster_sectors: int) -> _Layout:
    """The layout of a volume, with the smallest FAT that covers its
    clusters. FAT32 takes 32 reserved sectors (boot sector, FSInfo, their
    copies at 6 and 7) and keeps its root directory in clusters."""
    reserved = 32 if fat_type == 32 else 1
    root_entries = 0 if fat_type == 32 else ROOT_ENTRIES
    root_sectors = root_entries * ENTRY // sector_size

    def clusters_left(fat_sectors: int) -> int:
        data = sectors - reserved - FATS * fat_sectors - root_sectors
        return max(data // cluster_sectors, 0)

    def needed(fat_sectors: int) -> int:
        bits = (clusters_left(fat_sectors) + 2) * fat_type  # entries 0 and 1 are reserved
        return -(-bits // (8 * sector_size))

    # A larger FAT leaves fewer clusters to cover: climb to a FAT that covers
    # its clusters, then step down to the smallest that does.
    fat_sectors = 1
    while needed(fat_sectors) > fat_sectors:
        fat_sectors = needed(fat_sectors)
    while fat_sectors > 1 and needed(fat_sectors - 1) <= fat_sectors - 1:
        fat_sectors -= 1
    clusters = clusters_left(fat_sectors)
    return _Layout(
        fat_type,
        sector_size,
        sectors,
        cluster_sectors,
        reserved,
        fat_sectors,
        root_entries,
        clusters,
    )


def _choose(
    fat_type: int | None, sector_size: int, sectors: int, cluster_size: int | None
) -> _Layout:
    """The layout for the arguments given, filling in the FAT type and the
    cluster size when they're missing."""
    small = sectors * sector_size // 512
    preferred = 12 if small <= FAT12_SECTORS else 16 if small <= FAT16_SECTORS else 32
    kinds = (
        [fat_type]
        if fat_type is not None
        else [preferred, *(t for t in CLUSTERS if t != preferred)]
    )
    for kind in kinds:
        sizes = [cluster_size] if cluster_size else _cluster_sizes(kind, sector_size, small)
        for size in sizes:
            layout = _layout(kind, sector_size, sectors, size // sector_size)
            if layout.fits:
                return layout
    volume = f"{sectors} sectors of {sector_size} bytes"
    if fat_type is None:
        clusters = f" in {cluster_size}-byte clusters" if cluster_size else ""
        raise ConfigError(f"fat: no FAT type holds {volume}{clusters}")
    least, most = CLUSTERS[fat_type]
    if cluster_size:
        clusters_made = _layout(
            fat_type, sector_size, sectors, cluster_size // sector_size
        ).clusters
        raise ConfigError(
            f"fat: {volume} make {clusters_made} clusters of {cluster_size} bytes, and "
            f"FAT{fat_type} takes {least} to {most}"
        )
    most_made = _layout(fat_type, sector_size, sectors, 1).clusters
    if most_made < least:
        raise ConfigError(
            f"fat: FAT{fat_type} needs at least {least} clusters, and {volume} make at most "
            f"{most_made}"
        )
    least_made = _layout(fat_type, sector_size, sectors, 128).clusters
    raise ConfigError(
        f"fat: FAT{fat_type} holds at most {most} clusters, and {volume} make at least "
        f"{least_made}, with clusters of {128 * sector_size} bytes"
    )


def _cluster_sizes(fat_type: int, sector_size: int, small: int) -> list[int]:
    """Cluster sizes to try: the specification's for the volume first, then
    each size from one sector up."""
    sizes = [sector_size << shift for shift in range(8)]
    table = FAT16_TABLE if fat_type == 16 else FAT32_TABLE if fat_type == 32 else ()
    for limit, size in table:
        if small <= limit:
            preferred = max(size, sector_size)
            return [preferred, *(s for s in sizes if s != preferred)]
    return sizes


@dataclass
class _Node:
    """A file or folder on its way into the volume."""

    source: Source
    short: bytes  # DIR_Name: 11 bytes of code page 437, padded with spaces
    long: str | None  # the long name, when the short one doesn't spell the name
    parent: _Node | None
    children: list[_Node] = field(default_factory=list)
    entries: int = 0  # directory entries a folder holds
    first: int = 0  # first cluster, 0 for an empty file
    count: int = 0  # clusters, which are consecutive

    def walk(self) -> list[_Node]:
        """This node and everything under it, depth first in name order."""
        found = []
        stack = [self]
        while stack:
            node = stack.pop()
            found.append(node)
            stack.extend(reversed(node.children))
        return found


def _plan(tree: Source, layout: _Layout, labelled: bool) -> _Node:
    """The folder's names, as short and long names, and its folders' sizes
    in directory entries."""
    root = _Node(tree, b"", None, None)
    stack = [root]
    while stack:
        node = stack.pop()
        names = _names(node.source.children, node.source.path)
        node.children = [
            _Node(child, short, long, node)
            for child, (short, long) in zip(node.source.children, names, strict=True)
        ]
        node.entries = (1 if labelled else 0) if node is root else 2  # the label, or "." and ".."
        node.entries += sum(1 + _long_entries(child.long) for child in node.children)
        if node.entries > DIRECTORY_MAX:
            raise ConfigError(
                f"fat: {node.source.path} needs {node.entries} directory entries, and FAT "
                f"readers such as FatFs read at most {DIRECTORY_MAX}"
            )
        stack.extend(child for child in node.children if child.source.kind == "dir")
    if layout.fat_type != 32 and root.entries > ROOT_ENTRIES:
        raise ConfigError(
            f"fat: the root directory of FAT{layout.fat_type} holds {ROOT_ENTRIES} entries, and "
            f"the folder's top level needs {root.entries}: a name that isn't 8.3 takes one "
            "more entry for each 13 characters"
        )
    return root


def _allocate(root: _Node, layout: _Layout) -> int:
    """Give each folder and file its clusters, depth first in name order,
    from cluster 2 (FAT32's root directory first). The clusters used."""

    def clusters(size: int) -> int:
        return -(-size // layout.cluster_size)

    following = 2
    if layout.fat_type == 32:  # BPB_RootClus names a cluster even when the root is empty
        root.first, root.count = following, max(1, clusters(root.entries * ENTRY))
        following += root.count
    stack = [iter(root.children)]
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
            continue
        if node.source.kind == "dir":
            node.count = clusters(node.entries * ENTRY)
        else:
            if node.source.size > 0xFFFF_FFFF:
                raise ConfigError(
                    f"fat: {node.source.path} is {node.source.size} bytes, and a FAT file holds "
                    "at most 4 GiB less one byte"
                )
            node.count = clusters(node.source.size)
        if node.count:
            node.first, following = following, following + node.count
        if node.source.kind == "dir":
            stack.append(iter(node.children))
    return following - 2


def _write(
    image: bytearray,
    root: _Node,
    layout: _Layout,
    label: str,
    volume_id: int,
    stamp: tuple[int, int, int],
    used: int,
) -> None:
    sector = layout.sector_size
    image[: layout.data_start * sector] = bytes(layout.data_start * sector)
    boot = _boot(layout, label, volume_id)
    image[0:sector] = boot
    if layout.fat_type == 32:
        free, following = layout.clusters - used, used + 2
        info = _fsinfo(sector, free, following if free else 0xFFFF_FFFF)
        image[sector : 2 * sector] = info
        image[6 * sector : 7 * sector] = boot
        image[7 * sector : 8 * sector] = info

    table = [0] * (layout.clusters + 2)
    table[0] = END[layout.fat_type] & ~0xFF | MEDIA
    table[1] = END[layout.fat_type]
    nodes = root.walk()
    for node in nodes:
        for cluster in range(node.first, node.first + node.count):
            table[cluster] = cluster + 1
        if node.count:
            table[node.first + node.count - 1] = END[layout.fat_type]
    fat = _table(table, layout.fat_type).ljust(layout.fat_sectors * sector, b"\0")
    for copy in range(FATS):
        start = (layout.reserved + copy * layout.fat_sectors) * sector
        image[start : start + len(fat)] = fat

    for node in nodes:
        if node.source.kind == "dir":
            listing = _directory(node, label, stamp)
            if node is root and layout.fat_type != 32:
                start = layout.root_start * sector
            else:
                start = layout.offset(node.first)
                listing = listing.ljust(node.count * layout.cluster_size, b"\0")
            image[start : start + len(listing)] = listing
        elif node.count:
            data = node.source.read()
            start = layout.offset(node.first)
            image[start : start + len(data)] = data


def _directory(node: _Node, label: str, stamp: tuple[int, int, int]) -> bytes:
    """A folder's directory entries: "." and ".." (where the root's cluster
    counts as 0), or the label's entry in the root, then one entry per child
    in name order, each after its long-name entries."""
    out = bytearray()
    if node.parent is None:
        if label != NO_LABEL:
            out += _entry(label.ljust(11).encode("ascii"), ATTR_VOLUME_ID, 0, 0, stamp)
    else:
        up = 0 if node.parent.parent is None else node.parent.first
        out += _entry(b".".ljust(11), ATTR_DIRECTORY, node.first, 0, stamp)
        out += _entry(b"..".ljust(11), ATTR_DIRECTORY, up, 0, stamp)
    for child in node.children:
        if child.long is not None:
            out += _long(child.long, child.short)
        if child.source.kind == "dir":
            out += _entry(child.short, ATTR_DIRECTORY, child.first, 0, stamp)
        else:
            out += _entry(child.short, ATTR_ARCHIVE, child.first, child.source.size, stamp)
    return bytes(out)


def _entry(
    name: bytes, attributes: int, cluster: int, size: int, stamp: tuple[int, int, int]
) -> bytes:
    date, time, tenths = stamp
    return struct.pack(
        "<11sBBBHHHHHHHI",
        name,
        attributes,
        0,  # DIR_NTRes
        tenths,
        time,
        date,
        date,  # DIR_LstAccDate
        cluster >> 16,
        time,
        date,
        cluster & 0xFFFF,
        size,
    )


def _long(name: str, short: bytes) -> bytes:
    """The long-name entries of ``name``, last part first: 13 UTF-16 units
    each, the name ended by a 0 unit and padded with 0xFFFF."""
    units = name.encode("utf-16-le")
    if len(units) % 26:
        units += b"\0\0"
    units += b"\xff" * (-len(units) % 26)
    parts = [units[start : start + 26] for start in range(0, len(units), 26)]
    checksum = _checksum(short)
    out = bytearray()
    for order in range(len(parts), 0, -1):
        part = parts[order - 1]
        out += struct.pack(
            "<B10sBBB12sH4s",
            order | (0x40 if order == len(parts) else 0),
            part[:10],
            ATTR_LONG_NAME,
            0,
            checksum,
            part[10:22],
            0,
            part[22:],
        )
    return bytes(out)


def _long_entries(name: str | None) -> int:
    return 0 if name is None else -(-len(name.encode("utf-16-le")) // 26)


def _checksum(short: bytes) -> int:
    """The short name's checksum, which each of its long-name entries holds."""
    total = 0
    for byte in short:
        total = (((total & 1) << 7) + (total >> 1) + byte) & 0xFF
    return total


def _names(children: Sequence[Source], where: str) -> list[tuple[bytes, str | None]]:
    """Each child's short name and, when the short name doesn't spell it,
    its long name. A name that already is a short name (upper case, 8.3)
    keeps it; others get the specification's basis name, with a numeric tail
    "~N" when the conversion lost something or the basis is taken."""
    seen: dict[str, str] = {}
    for child in children:
        _check_long(child.name, child.path)
        if child.name.upper() in seen:
            raise ConfigError(
                f"fat: {where}: {seen[child.name.upper()]!r} and {child.name!r} are one name "
                "to FAT, which ignores case"
            )
        seen[child.name.upper()] = child.name
    names: dict[str, tuple[bytes, str | None]] = {}
    taken: set[bytes] = set()
    for child in children:
        exact = _exact(child.name)
        if exact is not None:
            names[child.name] = (exact, None)
            taken.add(exact)
    for child in children:
        if child.name in names:
            continue
        base, extension, lossy = _basis(child.name)
        short = _short(base, extension)
        if lossy or not _fits(child.name) or short in taken:
            for number in range(1, 1_000_000):
                tail = f"~{number}"
                short = _short(base[: 8 - len(tail)] + tail, extension)
                if short not in taken:
                    break
            else:
                raise ConfigError(f"fat: {where}: no short name left for {child.name!r}")
        taken.add(short)
        names[child.name] = (short, child.name)
    return [names[child.name] for child in children]


def _check_long(name: str, path: str) -> None:
    try:
        units = len(name.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise ConfigError(f"fat: {path}: the name is not valid Unicode") from None
    if units > LONG_NAME_MAX:
        raise ConfigError(
            f"fat: {path}: the name is {units} UTF-16 units long, and FAT holds {LONG_NAME_MAX}"
        )
    bad = sorted(set(name) & NOT_LONG)
    if bad:
        raise ConfigError(f"fat: {path}: a FAT name can't hold {''.join(bad)!r}")
    if name != name.strip(" ") or name.endswith("."):
        raise ConfigError(
            f"fat: {path}: a FAT name can't start or end with a space or end with a period"
        )


def _exact(name: str) -> bytes | None:
    """``name`` as a short name, when it is one already."""
    if name != name.upper() or not _fits(name):
        return None
    base, _, extension = name.partition(".")
    return _short(base, extension)


def _fits(name: str) -> bool:
    """Whether ``name``, in upper case, is a valid 8.3 name."""
    base, dot, extension = name.upper().partition(".")
    if not 1 <= len(base) <= 8 or len(extension) > 3 or (dot and not extension):
        return False
    return all(_short_character(character) for character in base + extension)


def _short_character(character: str) -> bool:
    if character in NOT_SHORT or character in ". ":
        return False
    try:
        character.encode("cp437")
    except UnicodeEncodeError:
        return False
    return True


def _basis(name: str) -> tuple[str, str, bool]:
    """The specification's basis name: upper case, characters a short name
    can't hold made "_", spaces and leading periods dropped, up to 8
    characters before the first period and 3 after the last. Whether a
    character was lost."""
    lossy = False
    characters = []
    for character in name.upper():
        if character in ". " or _short_character(character):
            characters.append(character)
        else:
            characters.append("_")
            lossy = True
    text = "".join(characters).replace(" ", "").lstrip(".")
    base = text.split(".", 1)[0][:8] or "_"
    extension = text.rsplit(".", 1)[1][:3] if "." in text else ""
    return base, extension, lossy


def _short(base: str, extension: str) -> bytes:
    """DIR_Name. Its first byte is never 0xE5, which marks a free entry:
    that is lower-case sigma in code page 437, and names are upper case."""
    return (base.ljust(8) + extension.ljust(3)).encode("cp437")


def _boot(layout: _Layout, label: str, volume_id: int) -> bytes:
    """The boot sector: the BIOS parameter block at byte 11, then the
    extended fields at 36 (FAT12, FAT16) or 64 (FAT32), and 55 AA at 510."""
    sector = bytearray(layout.sector_size)
    fat32 = layout.fat_type == 32
    sector[0:3] = b"\xeb\x58\x90" if fat32 else b"\xeb\x3c\x90"  # jump past the fields
    sector[3:11] = b"MSWIN4.1"  # the OEM name the specification recommends
    small = layout.sectors if not fat32 and layout.sectors < 0x10000 else 0
    struct.pack_into(
        "<HBHBHHBHHHII",
        sector,
        11,
        layout.sector_size,
        layout.cluster_sectors,
        layout.reserved,
        FATS,
        layout.root_entries,
        small,  # BPB_TotSec16
        MEDIA,
        0 if fat32 else layout.fat_sectors,  # BPB_FATSz16
        63,  # BPB_SecPerTrk and BPB_NumHeads: no geometry on flash; FatFs's f_mkfs values
        255,
        0,  # BPB_HiddSec
        0 if small else layout.sectors,  # BPB_TotSec32
    )
    extended = 36
    if fat32:
        # BPB_FATSz32, BPB_ExtFlags (both FATs kept), BPB_FSVer, BPB_RootClus,
        # BPB_FSInfo, BPB_BkBootSec
        struct.pack_into("<IHHIHH", sector, 36, layout.fat_sectors, 0, 0, 2, 1, 6)
        extended = 64
    struct.pack_into(
        "<BBBI11s8s",
        sector,
        extended,
        0x80,  # BS_DrvNum
        0,
        0x29,  # BS_BootSig: the next three fields are present
        volume_id,
        label.ljust(11).encode("ascii"),
        f"FAT{layout.fat_type}".ljust(8).encode("ascii"),
    )
    sector[510:512] = b"\x55\xaa"
    return bytes(sector)


def _fsinfo(sector_size: int, free: int, following: int) -> bytes:
    """FAT32's FSInfo sector: the free cluster count and where to look for
    the next free cluster."""
    sector = bytearray(sector_size)
    struct.pack_into("<I", sector, 0, 0x4161_5252)
    struct.pack_into("<III", sector, 484, 0x6141_7272, free, following)
    struct.pack_into("<I", sector, 508, 0xAA55_0000)
    return bytes(sector)


def _table(entries: list[int], fat_type: int) -> bytes:
    if fat_type == 16:
        return struct.pack(f"<{len(entries)}H", *entries)
    if fat_type == 32:
        return struct.pack(f"<{len(entries)}I", *entries)
    out = bytearray()  # FAT12: two entries in three bytes
    for index in range(0, len(entries), 2):
        low = entries[index]
        high = entries[index + 1] if index + 1 < len(entries) else 0
        out += bytes((low & 0xFF, low >> 8 | (high & 0x0F) << 4, high >> 4))
    return bytes(out[: -(-len(entries) * 3 // 2)])


def _stamp(time: datetime) -> tuple[int, int, int]:
    """DIR_WrtDate, DIR_WrtTime (2-second steps) and DIR_CrtTimeTenth (the
    odd second, in 10 ms units)."""
    date = (time.year - 1980) << 9 | time.month << 5 | time.day
    clock = time.hour << 11 | time.minute << 5 | time.second // 2
    return date, clock, (time.second % 2) * 100


def _boot_sector(image: bytes) -> dict[str, Any] | str:
    """What a prebuilt image's boot sector says, or why it isn't one."""
    if len(image) < 512:
        return f"the image is {len(image)} bytes, too short to hold a FAT boot sector"
    if image[510:512] != b"\x55\xaa":
        return f"no FAT boot sector: bytes 510 and 511 hold {image[510:512].hex(' ')}, not 55 aa"
    (
        sector_size,
        cluster_sectors,
        reserved,
        fats,
        root_entries,
        small,
        _media,
        fat16,
        _track,
        _heads,
        _hidden,
        large,
    ) = struct.unpack_from("<HBHBHHBHHHII", image, 11)
    fat_sectors = fat16 or struct.unpack_from("<I", image, 36)[0]
    sectors = small or large
    if sector_size not in SECTOR_SIZES or cluster_sectors not in [1 << s for s in range(8)]:
        return (
            f"not a FAT boot sector: {sector_size} bytes per sector and {cluster_sectors} "
            "sectors per cluster"
        )
    root_sectors = -(-root_entries * ENTRY // sector_size)
    data = sectors - reserved - fats * fat_sectors - root_sectors
    if not reserved or not fats or not fat_sectors or data < cluster_sectors:
        return (
            f"not a FAT boot sector: {sectors} sectors, {reserved} reserved, {fats} FATs of "
            f"{fat_sectors} sectors, {root_entries} root entries"
        )
    clusters = data // cluster_sectors
    fat_type = 12 if clusters < 4085 else 16 if clusters < 65525 else 32  # the specification's rule
    problems = []
    if clusters in (4085, 65525):
        problems.append(
            f"the volume has {clusters} clusters, which the FAT specification and FatFs "
            "read as different FAT types"
        )
    if (fat_type == 32) != (fat16 == 0) or (fat_type == 32) != (root_entries == 0):
        problems.append(
            f"the volume has {clusters} clusters, FAT{fat_type}, but BPB_FATSz16 is {fat16} "
            f"and BPB_RootEntCnt is {root_entries}"
        )
    return {
        "problems": problems,
        "sector_size": sector_size,
        "sector_count": sectors,
        "cluster_size": sector_size * cluster_sectors,
        "fat_type": fat_type,
    }


def _completed(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Completed arguments, checked again against the size of their own
    sectors."""
    if "sector_size" not in arguments or "sector_count" not in arguments:
        raise ConfigError(
            "fat: check takes the arguments that arguments() returns, with sector_size and "
            "sector_count"
        )
    size = _whole(arguments, "sector_size", None, 512, 4096) * _whole(
        arguments, "sector_count", None, 1, 0xFFFF_FFFF
    )
    return Fat().arguments(arguments, size)


def _listing(volume: Any) -> tuple[list[Entry], dict[str, Any]]:
    """Every entry under the root, and pyfatfs's entry for each path in
    upper case."""
    entries: list[Entry] = []
    found: dict[str, Any] = {}
    seen = {0, int(volume.bpb_header["BPB_RootClus"])} if volume.fat_type == 32 else {0}
    stack = [("/", volume.root_dir)]
    while stack:
        path, directory = stack.pop()
        folders, files, _special = directory.get_entries()
        for item in [*folders, *files]:
            name = _shown(item)
            inside = posixpath.join(path, name)
            found[inside.upper()] = item
            if item.is_directory():
                entries.append(Entry(inside, name, "dir", 0))
                if item.get_cluster() not in seen:  # a damaged volume can loop
                    seen.add(item.get_cluster())
                    stack.append((inside, item))
            else:
                entries.append(Entry(inside, name, "file", int(item.filesize)))
    return entries, found


def _shown(item: Any) -> str:
    """The entry's long name, or its short name with the case bits of
    DIR_NTRes applied: Windows NT and FatFs store "readme.txt" as README.TXT
    with both bits set, and no long name."""
    if item.lfn_entry is not None:
        return str(item.lfn_entry)
    base, dot, extension = str(item.get_short_name()).partition(".")
    if item.ntres & LOWER_BASE:
        base = base.lower()
    if item.ntres & LOWER_EXTENSION:
        extension = extension.lower()
    return base + dot + extension


def _contents(volume: Any, item: Any, entry: Entry) -> bytes:
    if not entry.size:
        return b""
    first = int(item.get_cluster())
    data = bytearray()
    try:
        if first < 2:
            raise ValueError(f"its first cluster is {first}")
        for cluster in volume.get_cluster_chain(first):
            data += volume.read_cluster_contents(cluster)
            if len(data) >= entry.size:
                break
    except Exception as error:  # pyfatfs raises many types on a damaged volume
        raise ConfigError(f"fat: can't read {entry.path}: {error}") from error
    if len(data) < entry.size:
        raise ConfigError(
            f"fat: {entry.path} is {entry.size} bytes, and its clusters hold {len(data)}"
        )
    return bytes(data[: entry.size])


def _label(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"fat: label is a string, not {value!r}")
    label = value.upper()
    if not 1 <= len(label) <= 11 or label.startswith(" ") or not set(label) <= LABEL:
        raise ConfigError(
            "fat: label is 1 to 11 of A-Z, 0-9, space and !#$%&'()-@^_`{}~, not starting "
            f"with a space, not {value!r}"
        )
    return label


def _time(value: Any) -> datetime:
    """A date and time, as a FAT entry holds it: the wall-clock fields, to
    the second, with any time zone dropped (FAT records none)."""
    when = value if isinstance(value, datetime) else None
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            when = datetime.fromisoformat(value.strip())
    if when is None:
        raise ConfigError(
            f'fat: time is a date and time such as "2026-01-31T12:00:00", not {value!r}'
        )
    when = when.replace(tzinfo=None, microsecond=0)
    if not EARLIEST <= when <= LATEST:
        raise ConfigError(
            f"fat: time is from {EARLIEST.isoformat()} to {LATEST.isoformat()}, the times a FAT "
            f"entry holds, not {when.isoformat()}"
        )
    return when


def _whole(given: Mapping[str, Any], key: str, default: int | None, least: int, most: int) -> int:
    value = given.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"fat: {key} is a whole number, not {value!r}")
    if not least <= value <= most:
        raise ConfigError(f"fat: {key} is {least} to {most}, not {value}")
    return value
