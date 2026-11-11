"""Composing what a layout (``jig.storage.layout``) puts in a target: a
storage device's whole image, or the regions a memory of the device
receives.

For each target, in this order:

1. every byte starts at the target's erased value: a storage model's
   ``ErasedValue``, a device memory's ``fill``;
2. the base image goes in at byte 0: the system's file for the whole
   target, else the image the device's family builds (a Zynq-7000's
   Quad-SPI boot image);
3. each partition's source goes in, in the order the board declares the
   partitions, then the files the system places at offsets.

A region written twice in step 3 is an error, and so is a write that lands
on bytes of the base image that are not erased, so a boot image and a file
system can't overwrite each other unnoticed. A file placed in a partition
fills it, the rest of the partition erased; it goes through the format's
``check`` first, and each problem the format names is an error. A folder
becomes the partition's bytes through the format's ``build``, cached by
``jig.artifacts`` under the folder's paths and contents, the format's name,
its completed arguments, the partition's size, the erased value, and the
versions of the code that builds it (``jig.storage.formats.builders``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jig import artifacts
from jig.errors import ConfigError
from jig.storage import formats
from jig.storage.layout import Layout, Partition, Target
from jig.storage.memory import MemoryImage, RegionFile

IMAGE = "image.bin"  # the file a cached partition build holds


@dataclass(frozen=True)
class Region:
    """Bytes placed at ``offset`` of a target; ``label`` says what they are."""

    offset: int
    data: bytes
    label: str

    @property
    def end(self) -> int:
        return self.offset + len(self.data)


@dataclass
class Composed:
    """What one target receives: its base image and the regions of step 3,
    over ``size`` bytes of ``erased``."""

    target: Target
    size: int
    erased: int
    base: Region | None = None
    regions: list[Region] = field(default_factory=list)

    @property
    def written(self) -> bool:
        """Whether anything writes the target."""
        return self.base is not None or bool(self.regions)

    def image(self) -> bytes:
        """The whole target: erased bytes with the base and the regions in place."""
        data = bytearray([self.erased]) * self.size
        for region in self.writes():
            data[region.offset : region.end] = region.data
        return bytes(data)

    def writes(self) -> list[Region]:
        """The base, then the regions: what a memory of the device receives."""
        return [*([self.base] if self.base is not None else []), *self.regions]

    def write(self, path: Path) -> None:
        """``image`` to the file ``path``, without holding all of it: erased
        bytes a megabyte at a time, then the base and the regions in place."""
        block = bytes([self.erased]) * min(self.size, 1 << 20)
        with path.open("wb") as out:
            left = self.size
            while left:
                step = min(left, len(block))
                out.write(block[:step])
                left -= step
            for region in self.writes():
                out.seek(region.offset)
                out.write(region.data)


def compose(
    layout: Layout,
    target: str,
    *,
    erased: int,
    size: int | None = None,
    base: Path | None = None,
    base_label: str = "",
) -> Composed:
    """What ``target`` of ``layout`` receives. ``erased`` is its erased value
    (a device memory's ``fill``), ``size`` its size when the board gives none
    (a storage model's), ``base`` the family's image for it, used when the
    system gives no file for the whole target. Every problem raises one
    ConfigError that lists them all."""
    composed, found = _compose(layout, target, erased, size, base, base_label)
    if len(found) == 1:
        raise ConfigError(found[0])
    if found:
        listed = "".join(f"\n    {problem}" for problem in found)
        raise ConfigError(f"{composed.target.what}: {len(found)} problems composing it:{listed}")
    return composed


def composition_problems(
    layout: Layout, target: str, *, erased: int, size: int | None = None
) -> list[str]:
    """What ``compose`` would refuse for ``target``, one problem each
    (``jig check``)."""
    try:
        return _compose(layout, target, erased, size, None, "")[1]
    except ConfigError as exc:
        return [str(exc)]


def _compose(
    layout: Layout,
    target: str,
    erased: int,
    size: int | None,
    base: Path | None,
    base_label: str,
) -> tuple[Composed, list[str]]:
    found = layout.targets[target]
    size = found.size if size is None else size
    if size is None:
        raise ConfigError(f"{found.what}: its size is unknown: give its peripheral a size property")
    if found.size is not None and size != found.size:
        raise ConfigError(
            f"{found.what}: the board gives {found.size} bytes and the model {size}: "
            "make the size property agree with the model"
        )
    if not 0 <= erased <= 0xFF:
        raise ConfigError(f"{found.what}: an erased value is a byte, not {erased}")
    composed = Composed(found, size, erased)
    problems: list[str] = []
    composed.base = _base(layout, found, size, base, base_label, problems)
    for partition in layout.on(target):
        if partition.source is not None:
            composed.regions += _partition_regions(partition, erased, problems)
    offsets = layout.offsets.get(target)
    if offsets is not None:
        for offset, path in offsets.offsets:
            data = _read(path, offsets.origin, problems)
            if data is None:
                continue
            if offset + len(data) > size:
                problems.append(
                    f"{offsets.origin}: {path.name} at 0x{offset:X} ({len(data)} bytes) runs "
                    f"past the end of {found.what} ({size} bytes)"
                )
                continue
            composed.regions.append(Region(offset, data, f"{path.name} at 0x{offset:X}"))
    _check_overlaps(found, composed.regions, problems)
    if composed.base is not None:
        _check_base(found, composed.base, composed.regions, erased, problems)
    return composed, problems


def _read(path: Path, origin: str, problems: list[str]) -> bytes | None:
    if path.is_dir():
        problems.append(
            f"{origin}: {path} is a folder: a folder fills a partition, which its format builds"
        )
        return None
    if not path.is_file():
        problems.append(f"{origin}: {path} not found")
        return None
    return path.read_bytes()


def _base(
    layout: Layout,
    target: Target,
    size: int,
    family: Path | None,
    family_label: str,
    problems: list[str],
) -> Region | None:
    """Step 2: the system's file for the whole target, else the family's image."""
    given = layout.bases.get(target.name)
    if given is not None and given.path is not None:
        path, origin = given.path, given.origin
    elif family is not None:
        path, origin = family, family_label or f"the family's image for {target.name}"
    else:
        return None
    data = _read(path, origin, problems)
    if data is None:
        return None
    if len(data) > size:
        problems.append(
            f"{origin}: {path.name} ({len(data)} bytes) is larger than {target.what} ({size} bytes)"
        )
        return None
    return Region(0, data, f"the base image {path.name}")


def _partition_regions(partition: Partition, erased: int, problems: list[str]) -> list[Region]:
    source = partition.source
    assert source is not None
    if source.path is None:
        regions = []
        for offset, path in source.offsets:
            data = _read(path, source.origin, problems)
            if data is None:
                continue
            if offset + len(data) > partition.size:
                problems.append(
                    f"{source.origin}: {path.name} at 0x{offset:X} ({len(data)} bytes) runs "
                    f"past the end of {partition.label} ({partition.size} bytes)"
                )
                continue
            label = f"{partition.label}: {path.name} at +0x{offset:X}"
            regions.append(Region(partition.at + offset, data, label))
        return regions
    path = source.path
    if path.is_dir():
        try:
            data = build(partition, path, erased)
        except ConfigError as exc:
            problems.append(f"{partition.where}: {partition.label}: {exc}")
            return []
        return [Region(partition.at, data, f"{partition.label}, built from {path.name}")]
    if not path.is_file():
        problems.append(f"{source.origin}: {path} not found")
        return []
    data = path.read_bytes()
    if len(data) > partition.size:
        problems.append(
            f"{source.origin}: {path.name} ({len(data)} bytes) is larger than "
            f"{partition.label} ({partition.size} bytes)"
        )
        return []
    for issue in partition.format.check(data, partition.arguments):
        problems.append(f"{source.origin}: {path.name} as {partition.format_name}: {issue}")
    padded = data + bytes([erased]) * (partition.size - len(data))
    return [Region(partition.at, padded, f"{partition.label} ({path.name})")]


def build(partition: Partition, folder: Path, erased: int) -> bytes:
    """The folder built into ``partition`` by its format: exactly the
    partition's size, cached under the folder's paths and contents, the
    format's name, its completed arguments, the size, ``erased``, and the
    versions of Jig and of the library that writes the format."""
    files: list[Path] = []
    paths: list[str] = []
    for path in sorted(folder.rglob("*"), key=lambda p: p.relative_to(folder).as_posix()):
        relative = path.relative_to(folder).as_posix()
        if path.is_dir():
            paths.append(relative + "/")
        elif path.is_file():
            files.append(path)
            paths.append(relative)
    params: dict[str, Any] = {
        "format": partition.format_name,
        "builders": formats.builders(partition.format_name),
        "arguments": dict(partition.arguments),
        "size": partition.size,
        "erased": erased,
        "paths": paths,
    }

    def make(directory: Path) -> None:
        data = partition.format.build(folder, partition.size, partition.arguments, erased)
        if len(data) != partition.size:
            raise ConfigError(
                f"format {partition.format_name} built {len(data)} bytes for {partition.size} bytes"
            )
        (directory / IMAGE).write_bytes(data)

    entry = artifacts.cached(f"partition-{partition.format_name}", files, make, params)
    return (entry / IMAGE).read_bytes()


def _check_overlaps(target: Target, regions: list[Region], problems: list[str]) -> None:
    """No byte of step 3 is written twice."""
    reach: Region | None = None
    for region in sorted(regions, key=lambda r: (r.offset, r.end)):
        if not region.data:
            continue
        if reach is not None and region.offset < reach.end:
            problems.append(
                f"{target.what}: {region.label} (0x{region.offset:X}-0x{region.end - 1:X}) "
                f"writes over {reach.label} (0x{reach.offset:X}-0x{reach.end - 1:X})"
            )
        if reach is None or region.end > reach.end:
            reach = region


def _check_base(
    target: Target, base: Region, regions: list[Region], erased: int, problems: list[str]
) -> None:
    """Step 3 writes only where the base image is erased."""
    for region in regions:
        low, high = region.offset, min(region.end, base.end)
        if low >= high:
            continue
        under = base.data[low:high]
        stray = next((i for i, byte in enumerate(under) if byte != erased), None)
        if stray is not None:
            problems.append(
                f"{target.what}: {region.label} (0x{region.offset:X}-0x{region.end - 1:X}) "
                f"lands on bytes of {base.label} that are not erased (0x{erased:02X}), "
                f"from 0x{low + stray:X}"
            )


def write_regions(composed: Composed, directory: Path) -> MemoryImage:
    """A device memory's writes, each in a file of ``directory``
    (``<target>-<n>.bin``), as its family's driver loads them."""
    memory = composed.target.memory
    if memory is None:
        raise ConfigError(f"{composed.target.what} is not a memory of the device")
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for index, region in enumerate(composed.writes()):
        path = directory / f"{composed.target.name}-{index}.bin"
        path.write_bytes(region.data)
        files.append(RegionFile(region.offset, len(region.data), path, region.label))
    return MemoryImage(memory, tuple(files))
