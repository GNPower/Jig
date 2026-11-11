"""Reading a partition back while a run goes and after it ends.

A partition on a storage device reads the device's image file in the
machine's directory (``<machine>/<storage>.img``): the model writes each
program or erase to it as it completes, and plans read between steps, while
Renode is paused. A partition on a memory of the device reads the machine
(``jig.storage.memory.read``). A partition whose format has files opens them
through the format (``StorageFormat.open``) over those bytes.
"""

from __future__ import annotations

from pathlib import Path

from jig.errors import ConfigError
from jig.storage import memory
from jig.storage.formats import FileSystemView
from jig.storage.layout import Partition


def image_path(workdir: Path, storage: str) -> Path:
    """The image file a run attaches to a storage device."""
    return workdir / f"{storage}.img"


def from_image(partition: Partition, data: bytes, image: str = "the image") -> bytes:
    """``partition``'s bytes out of a whole image of its target."""
    if len(data) < partition.end:
        raise ConfigError(
            f"{image} holds {len(data)} bytes; partition {partition.name} ends at "
            f"0x{partition.end:X}"
        )
    return data[partition.at : partition.end]


def from_file(partition: Partition, path: Path) -> bytes:
    """``partition``'s bytes out of an image file of its target, reading only
    the partition."""
    if not path.is_file():
        raise ConfigError(f"partition {partition.name}: no image {path}")
    with path.open("rb") as handle:
        handle.seek(partition.at)
        data = handle.read(partition.size)
    if len(data) != partition.size:
        raise ConfigError(
            f"{path} holds {path.stat().st_size} bytes; partition {partition.name} ends at "
            f"0x{partition.end:X}"
        )
    return data


def partition_bytes(
    partition: Partition, workdir: Path, session: memory.MachineSession, machine: str
) -> bytes:
    """``partition``'s bytes in a running machine: from its storage device's
    image file, or from the machine for a memory of the device."""
    target = partition.target
    if target.memory is None:
        return from_file(partition, image_path(workdir, target.name))
    return memory.read(session, machine, target.memory, partition.at, partition.size, workdir)


def view(partition: Partition, data: bytes) -> FileSystemView:
    """The file system in ``data``, a partition's bytes; a ConfigError for a
    format without files (``raw``)."""
    opened = partition.format.open(data, partition.arguments)
    if opened is None:
        raise ConfigError(
            f"partition {partition.name} is {partition.format_name}, a format without files: "
            "read its bytes"
        )
    return opened
