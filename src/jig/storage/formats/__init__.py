"""Formats of a partition: building an image from a folder, checking a
prebuilt image, and reading files back out of one.

A format is an object with the methods of ``StorageFormat``. Jig ships the
formats ``builtin()`` returns; a board's plugin adds its own, which the
caller passes to ``get``. Every method works on bytes, so one format serves a
storage device's image file, a memory read from a machine, and an image a
test makes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

from jig.errors import ConfigError

ERASED = 0xFF
"""The erased value of NOR flash, and the default fill of an image."""

WRITERS = {"littlefs": "littlefs-python"}
"""The distribution whose code writes a shipped format's volumes, by format.
Jig writes fat volumes itself, and raw builds nothing."""


@dataclass(frozen=True)
class Entry:
    """A file or a directory in a file system."""

    path: str  # from the volume's root, "/config/settings.txt"
    name: str  # "settings.txt"
    kind: str  # "file" or "dir"
    size: int  # bytes, 0 for a directory


class FileSystemView(Protocol):
    """A file system read from an image, read-only."""

    def files(self, path: str = "/") -> list[Entry]:
        """The entries directly inside ``path``, sorted by name."""

    def walk(self) -> list[Entry]:
        """Every entry under the root, depth first, each directory's entries
        sorted by name."""

    def read(self, path: str) -> bytes:
        """A file's contents; a ConfigError when ``path`` isn't a file."""

    def exists(self, path: str) -> bool:
        """Whether a file or directory is at ``path``."""

    def stat(self, path: str) -> Entry:
        """The entry at ``path``; a ConfigError when nothing is there."""


class StorageFormat(Protocol):
    """What a partition's format does with the partition's bytes."""

    name: str

    def arguments(self, given: Mapping[str, Any], size: int) -> dict[str, Any]:
        """The format's arguments, checked against the partition's size and
        completed with their defaults. A ConfigError names a bad or unknown
        one."""

    def build(
        self, folder: Path, size: int, arguments: Mapping[str, Any], erased: int = ERASED
    ) -> bytes:
        """An image of exactly ``size`` bytes holding the folder's files.
        One folder and one set of arguments give the same bytes every time.
        Bytes the format doesn't write hold ``erased``."""

    def check(self, image: bytes, arguments: Mapping[str, Any]) -> list[str]:
        """Why a prebuilt image doesn't fit the arguments; empty when it
        fits."""

    def open(self, image: bytes, arguments: Mapping[str, Any]) -> FileSystemView | None:
        """The image's file system, or None for a format without files."""


class Raw:
    """Bytes placed as they are: a raw partition's contents is a file, not a
    folder, and it has no files to read back."""

    name = "raw"

    def arguments(self, given: Mapping[str, Any], size: int) -> dict[str, Any]:
        if given:
            raise ConfigError(
                "raw takes no arguments besides on, at, size and contents, "
                f"not {', '.join(sorted(given))}"
            )
        return {}

    def build(
        self, folder: Path, size: int, arguments: Mapping[str, Any], erased: int = ERASED
    ) -> bytes:
        raise ConfigError(f"a raw partition's contents is a file, not the folder {folder}")

    def check(self, image: bytes, arguments: Mapping[str, Any]) -> list[str]:
        return []

    def open(self, image: bytes, arguments: Mapping[str, Any]) -> FileSystemView | None:
        return None


def builtin() -> dict[str, StorageFormat]:
    """The formats Jig ships, by name."""
    # Imported here: the modules import this one, and littlefs loads a C extension.
    from jig.storage.formats.fat import Fat
    from jig.storage.formats.littlefs import LittleFs

    return {"raw": Raw(), "littlefs": LittleFs(), "fat": Fat()}


def builders(name: str) -> dict[str, str]:
    """The code that builds a folder in the format called ``name``, as
    versions by distribution: Jig's, and for a format Jig ships that a
    library writes, the library's (littlefs-python for littlefs).
    ``jig.storage.compose`` keys each built image with them, so a release
    that changes a builder never reuses an image built before it."""
    found = {"jig-dsl": _version("jig-dsl")}
    library = WRITERS.get(name)
    if library is not None:
        found[library] = _version(library)
    return found


def _version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:  # a source tree that was never installed
        return "unknown"


def get(name: str, registered: Mapping[str, StorageFormat] | None = None) -> StorageFormat:
    """The format called ``name``: one Jig ships or one of ``registered``
    (the formats board plugins added). A ConfigError lists the known names."""
    known = {**builtin(), **(registered or {})}
    if name not in known:
        raise ConfigError(f"no storage format {name!r} (known: {', '.join(sorted(known))})")
    return known[name]
