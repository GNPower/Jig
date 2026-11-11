"""Partitions and sources resolved against a board: what each storage
device and each memory of the device receives, before any byte is read.

A board's ``partition`` lines (``jig.system.model.PartitionSpec``) name
their target with ``on``: a peripheral on the board's ``storage`` list, or a
memory of the device itself (``mcu.flash``, which the device's family names,
``jig.storage.memory``). A system's ``storage`` map gives each board
instance its own sources, by key:

* a storage peripheral (``flash0``) or a device memory as a quoted name
  (``"zynq.ocm"``): a file, placed at its first byte as the base image, or a
  map from offset to file, each file placed at that offset;
* a partition's name: a file, placed as it is; a folder, built by the
  partition's format; or a map from offset to file, the offsets counted from
  the partition's first byte. It replaces the board file's ``contents``.

``resolve`` checks what needs no byte read: each partition's target and
format, the format's arguments, alignment to a ``block_size`` or
``sector_size`` argument, bounds and overlaps between partitions, and the
map's keys. ``jig.storage.compose`` reads the files.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jig.errors import ConfigError
from jig.storage import formats
from jig.storage.formats import StorageFormat
from jig.storage.memory import DeviceMemory
from jig.system.boards import Board, Storage
from jig.system.model import InternalRef, PartitionSpec

ALIGNMENT = ("block_size", "sector_size")
"""Completed format arguments that ``at`` and ``size`` are multiples of."""

SourceValue = Path | Mapping[int, Path]
"""A value of a system's storage map, its paths resolved: a file or folder,
or files by offset."""


@dataclass(frozen=True)
class Target:
    """A storage device or a memory of the device: its name as partitions and
    storage maps write it (``flash0``, ``mcu.flash``) and its size in bytes
    (None for a storage peripheral that gives no ``size``; its model knows)."""

    name: str
    size: int | None
    storage: Storage | None = None
    memory: DeviceMemory | None = None

    @property
    def what(self) -> str:
        return f"memory {self.name}" if self.memory is not None else f"storage {self.name}"


@dataclass(frozen=True)
class Source:
    """What goes into a target or a partition: a file or a folder
    (``path``), or files at offsets (``offsets``). ``origin`` says where it
    was given, for messages."""

    path: Path | None = None
    offsets: tuple[tuple[int, Path], ...] = ()
    origin: str = ""

    @property
    def files(self) -> list[Path]:
        return [self.path] if self.path is not None else [path for _at, path in self.offsets]


@dataclass(frozen=True)
class Partition:
    """A partition resolved: its target, its format and the format's
    completed arguments, and the source that fills it (the system's, else
    the board file's ``contents``; None leaves the region as it was)."""

    name: str
    target: Target
    at: int
    size: int
    format_name: str
    format: StorageFormat
    arguments: Mapping[str, Any]
    source: Source | None
    where: str

    @property
    def end(self) -> int:
        return self.at + self.size

    @property
    def label(self) -> str:
        return f"partition {self.name}"


@dataclass
class Layout:
    """A board's partitions and a system's sources, by target. Problems
    found while resolving them are in ``problems``; ``raise_problems`` turns
    them into one ConfigError."""

    board: str
    targets: dict[str, Target] = field(default_factory=dict)
    partitions: list[Partition] = field(default_factory=list)
    bases: dict[str, Source] = field(default_factory=dict)
    offsets: dict[str, Source] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def on(self, target: str) -> list[Partition]:
        """The partitions on ``target``, in the order the board declares them."""
        return [p for p in self.partitions if p.target.name == target]

    def partition(self, name: str) -> Partition:
        for partition in self.partitions:
            if partition.name == name:
                return partition
        known = ", ".join(p.name for p in self.partitions) or "none"
        raise ConfigError(f"board {self.board} has no partition {name!r} (has: {known})")

    def written(self, target: str) -> bool:
        """Whether a base, a partition's source or a file at an offset
        writes ``target``."""
        return (
            target in self.bases
            or target in self.offsets
            or any(p.source is not None for p in self.on(target))
        )

    def raise_problems(self) -> None:
        if len(self.problems) == 1:
            raise ConfigError(self.problems[0])
        if self.problems:
            listed = "".join(f"\n    {problem}" for problem in self.problems)
            raise ConfigError(f"board {self.board}: {len(self.problems)} storage problems:{listed}")


def sources(given: Mapping[str, Any] | None) -> dict[str, SourceValue]:
    """A storage map as code gives it (paths as text or paths), with each
    file a ``Path``: a file or folder, or a map from offset to file."""
    found: dict[str, SourceValue] = {}
    for key, value in (given or {}).items():
        if isinstance(value, (str, os.PathLike)):
            found[str(key)] = Path(value)
        elif isinstance(value, Mapping):
            offsets: dict[int, Path] = {}
            for offset, path in value.items():
                if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                    raise ConfigError(f"storage {key}: {offset!r} is not an offset in bytes")
                if not isinstance(path, (str, os.PathLike)):
                    raise ConfigError(f"storage {key}: at 0x{offset:X} names no file")
                offsets[offset] = Path(path)
            found[str(key)] = offsets
        else:
            raise ConfigError(
                f"storage {key}: a source is a file, a folder or a map from offset to file, "
                f"not {value!r}"
            )
    return found


def files_needed(board: Board, given: Mapping[str, SourceValue]) -> list[tuple[str, Path]]:
    """What the storage of a board instance reads from disk, as (what it is,
    path): the system's sources, and the ``contents`` of each partition the
    system gives none."""
    partitions = {p.name for p in board.partitions}
    found: list[tuple[str, Path]] = []
    for key, value in given.items():
        if isinstance(value, Path):
            what = f"contents of partition {key}" if key in partitions else f"image for {key}"
            found.append((what, value))
        else:
            found += [(f"file for {key} at 0x{at:X}", path) for at, path in sorted(value.items())]
    for partition in board.partitions:
        if partition.contents is not None and partition.name not in given:
            found.append((f"contents of partition {partition.name}", partition.contents))
    return found


def device_memories(board: Board, driver: Any) -> dict[str, DeviceMemory]:
    """The memories of ``board``'s device, as its family's driver names them
    (``memories(board)``); none for a family that names none."""
    find = getattr(driver, "memories", None)
    if not callable(find) or not board.devices:
        return {}
    return dict(find(board))


def for_board(
    board: Board, driver: Any, sources: Mapping[str, SourceValue] | None = None
) -> Layout:
    """``resolve`` with the device's memories from ``driver`` and the formats
    the board's plugin registered (the plugin has loaded). The family names
    its memories only when a partition or a key of the storage map names
    one, since a family may need files to know them (a Zynq-7000's XSA for
    its block RAM)."""
    from jig.system import registry

    registered = registry.default().storage_formats_for(board)
    wanted = any(isinstance(p.on, InternalRef) for p in board.partitions) or any(
        "." in key for key in sources or {}
    )
    memories = device_memories(board, driver) if wanted else {}
    return resolve(board, memories, sources, registered)


def targets(board: Board, memories: Mapping[str, DeviceMemory]) -> dict[str, Target]:
    """Every storage device and device memory of ``board`` by the name a
    partition's ``on`` gives it."""
    found = {s.name: Target(s.name, s.size, storage=s) for s in board.storage}
    if board.devices:
        device = board.device.name
        for memory in memories.values():
            name = f"{device}.{memory.name}"
            found[name] = Target(name, memory.size, memory=memory)
    return found


def resolve(
    board: Board,
    memories: Mapping[str, DeviceMemory],
    sources: Mapping[str, SourceValue] | None = None,
    registered: Mapping[str, StorageFormat] | None = None,
) -> Layout:
    """``board``'s partitions and the system's ``sources`` (its storage map
    with the paths resolved), checked as far as they go without reading a
    file. ``memories`` are the device's own (its family's ``memories``),
    ``registered`` the formats the board's plugin added."""
    layout = Layout(board.name, targets(board, memories))
    given = dict(sources or {})
    names = {p.name for p in board.partitions}
    for key, value in given.items():
        _source(layout, board, key, value, names)
    for spec in board.partitions:
        partition = _partition(layout, spec, given.get(spec.name), registered or {})
        if partition is not None:
            layout.partitions.append(partition)
    _overlaps(layout)
    for name, target in layout.targets.items():
        if target.size is None and (layout.on(name) or name in layout.offsets):
            layout.problems.append(
                f"{target.what}: its partitions and files at offsets need the peripheral's "
                f"size, which it does not give: add size to {name}, such as size: 16 MiB"
            )
    return layout


def _origin(key: str) -> str:
    return f"the storage map's {key}"


def _source(
    layout: Layout, board: Board, key: str, value: SourceValue, partitions: set[str]
) -> None:
    """One entry of the system's storage map (as ``sources`` checks it),
    filed under its target."""
    if isinstance(value, Path):
        source = Source(path=value, origin=_origin(key))
    else:
        source = Source(offsets=tuple(sorted(value.items())), origin=_origin(key))
    if key in partitions:
        return  # the partition takes it (_partition)
    if key in layout.targets:
        target = layout.targets[key]
        if source.path is not None:
            layout.bases[key] = source
        else:
            for offset, _path in source.offsets:
                if target.size is not None and offset >= target.size:
                    layout.problems.append(
                        f"{_origin(key)}: offset 0x{offset:X} is past the end of "
                        f"{target.what} ({target.size} bytes)"
                    )
            layout.offsets[key] = source
        return
    known = [
        *(f"partition {name}" for name in sorted(partitions)),
        *(target.what for target in layout.targets.values()),
    ]
    if "." in key:
        hint = "a device memory is DEVICE.MEMORY, quoted"
    else:
        hint = "a key is a partition, a storage device, or a device memory in quotes"
    layout.problems.append(
        f"board {board.name} has no partition, storage device or device memory {key!r} "
        f"({hint}; the board has: {', '.join(known) or 'none'})"
    )


def _partition(
    layout: Layout,
    spec: PartitionSpec,
    given: SourceValue | None,
    registered: Mapping[str, StorageFormat],
) -> Partition | None:
    what = f"{spec.where}: partition {spec.name}"
    target = layout.targets.get(spec.target)
    if target is None:
        if isinstance(spec.on, InternalRef):
            memories = sorted(t.name for t in layout.targets.values() if t.memory is not None)
            layout.problems.append(
                f"{what}: {spec.on.device} has no memory {spec.on.name} "
                f"(has: {', '.join(memories) or 'none'})"
            )
        else:
            layout.problems.append(f"{what}: {spec.target} is not a storage device of the board")
        return None
    try:
        format_ = formats.get(spec.format, registered)
        arguments = format_.arguments(spec.arguments, spec.size)
    except ConfigError as exc:
        layout.problems.append(f"{what}: {exc}")
        return None
    for key in ALIGNMENT:
        unit = arguments.get(key)
        if not isinstance(unit, int) or isinstance(unit, bool) or unit <= 0:
            continue
        for name, value in (("at", spec.at), ("size", spec.size)):
            if value % unit:
                layout.problems.append(
                    f"{what}: {name} 0x{value:X} is not a multiple of its {key}, {unit}"
                )
    if target.size is not None and spec.end > target.size:
        layout.problems.append(
            f"{what}: 0x{spec.at:X} + 0x{spec.size:X} runs past the end of {target.what} "
            f"({target.size} bytes)"
        )
    source: Source | None = None
    if given is not None:
        if isinstance(given, Path):
            source = Source(path=given, origin=_origin(spec.name))
        else:
            source = Source(offsets=tuple(sorted(given.items())), origin=_origin(spec.name))
            for offset, _path in source.offsets:
                if offset >= spec.size:
                    layout.problems.append(
                        f"{_origin(spec.name)}: offset 0x{offset:X} is past the end of "
                        f"partition {spec.name} ({spec.size} bytes)"
                    )
    elif spec.contents is not None:
        source = Source(path=spec.contents, origin=f"{what}: contents")
    return Partition(
        spec.name, target, spec.at, spec.size, spec.format, format_, arguments, source, spec.where
    )


def _overlaps(layout: Layout) -> None:
    """Partitions on one target never share a byte."""
    for name in layout.targets:
        reach: Partition | None = None  # the partition reaching furthest so far
        for partition in sorted(layout.on(name), key=lambda p: (p.at, p.end)):
            if reach is not None and partition.at < reach.end:
                layout.problems.append(
                    f"{partition.where}: partition {partition.name} (0x{partition.at:X}-"
                    f"0x{partition.end - 1:X}) overlaps partition {reach.name} "
                    f"(0x{reach.at:X}-0x{reach.end - 1:X}) on {name}"
                )
            if reach is None or partition.end > reach.end:
                reach = partition
