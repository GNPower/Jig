"""Test doubles of ``jig.storage.formats.StorageFormat``, for the storage
tests that run without the littlefs and fat formats.

``Listing`` is a file system of its own: a header (``LIST``, the block size,
the entry count), then each entry as a path, a kind and the file's bytes. It
builds from a folder in sorted order, checks a prebuilt image's header
against its ``block_size`` argument and opens images for reading back.
``builds`` counts its builds, so tests can see the artifact cache work.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jig.errors import ConfigError
from jig.storage.formats import ERASED, Entry

MAGIC = b"LIST"


class ListingView:
    """A Listing image, read: the ``FileSystemView`` methods."""

    def __init__(self, entries: dict[str, tuple[str, bytes]]) -> None:
        self._entries = entries  # path -> (kind, data)

    def _entry(self, path: str) -> Entry:
        kind, data = self._entries[path]
        return Entry(path, path.rsplit("/", 1)[-1], kind, len(data))

    def files(self, path: str = "/") -> list[Entry]:
        if path != "/" and self._entries.get(path, ("", b""))[0] != "dir":
            raise ConfigError(f"{path} is not a directory")
        prefix = path.rstrip("/") + "/"
        inside = [p for p in self._entries if p.startswith(prefix) and "/" not in p[len(prefix) :]]
        return [self._entry(p) for p in sorted(inside)]

    def walk(self) -> list[Entry]:
        found: list[Entry] = []

        def visit(path: str) -> None:
            for entry in self.files(path):
                found.append(entry)
                if entry.kind == "dir":
                    visit(entry.path)

        visit("/")
        return found

    def read(self, path: str) -> bytes:
        kind, data = self._entries.get(path, ("", b""))
        if kind != "file":
            raise ConfigError(f"{path} is not a file")
        return data

    def exists(self, path: str) -> bool:
        return path == "/" or path in self._entries

    def stat(self, path: str) -> Entry:
        if path not in self._entries:
            raise ConfigError(f"nothing at {path}")
        return self._entry(path)


class Listing:
    """A file-system format for tests (see the module documentation)."""

    name = "listing"

    def __init__(self) -> None:
        self.builds = 0

    def arguments(self, given: Mapping[str, Any], size: int) -> dict[str, Any]:
        unknown = sorted(set(given) - {"block_size"})
        if unknown:
            raise ConfigError(f"listing takes block_size, not {', '.join(unknown)}")
        block = given.get("block_size", 512)
        if not isinstance(block, int) or block <= 0:
            raise ConfigError(f"block_size is a whole number of bytes, not {block!r}")
        return {"block_size": block}

    def build(
        self, folder: Path, size: int, arguments: Mapping[str, Any], erased: int = ERASED
    ) -> bytes:
        self.builds += 1
        entries = []
        for path in sorted(folder.rglob("*"), key=lambda p: p.relative_to(folder).as_posix()):
            relative = "/" + path.relative_to(folder).as_posix()
            if path.is_dir():
                entries.append((relative, "dir", b""))
            else:
                entries.append((relative, "file", path.read_bytes()))
        body = bytearray(MAGIC + struct.pack("<II", arguments["block_size"], len(entries)))
        for relative, kind, data in entries:
            name = relative.encode()
            body += struct.pack("<HBI", len(name), kind == "dir", len(data)) + name + data
        if len(body) > size:
            raise ConfigError(f"the folder needs {len(body)} bytes, the partition has {size}")
        return bytes(body) + bytes([erased]) * (size - len(body))

    def check(self, image: bytes, arguments: Mapping[str, Any]) -> list[str]:
        if image[:4] != MAGIC:
            return ["no listing header"]
        (block,) = struct.unpack_from("<I", image, 4)
        if block != arguments["block_size"]:
            return [f"block size {block}, the partition's is {arguments['block_size']}"]
        return []

    def open(self, image: bytes, arguments: Mapping[str, Any]) -> ListingView | None:
        problems = self.check(image, arguments)
        if problems:
            raise ConfigError(problems[0])
        (count,) = struct.unpack_from("<I", image, 8)
        at = 12
        entries: dict[str, tuple[str, bytes]] = {}
        for _ in range(count):
            length, is_dir, size = struct.unpack_from("<HBI", image, at)
            at += 7
            path = image[at : at + length].decode()
            at += length
            entries[path] = ("dir" if is_dir else "file", image[at : at + size])
            at += size
        return ListingView(entries)


def make_elf(
    path: Path,
    segments: list[tuple[int, bytes]],
    *,
    wide: bool = False,
    entry: int = 0,
    memory_sizes: Mapping[int, int] | None = None,
) -> Path:
    """An ELF holding one loadable segment per (physical address, bytes),
    with no sections, starting at ``entry``. ``memory_sizes`` gives a
    segment, by its address, a ``p_memsz`` other than its length, as a
    ``.bss`` has."""
    count = len(segments)
    if wide:
        header_size, entry_size = 64, 56
    else:
        header_size, entry_size = 52, 32
    data_at = header_size + entry_size * count
    out = bytearray(b"\x7fELF" + bytes([2 if wide else 1, 1, 1]) + bytes(9))
    if wide:
        out += struct.pack(
            "<HHIQQQIHHHHHH", 2, 40, 1, entry, header_size, 0, 0, 64, 56, count, 0, 0, 0
        )
    else:
        out += struct.pack(
            "<HHIIIIIHHHHHH", 2, 40, 1, entry, header_size, 0, 0x5000000, 52, 32, count, 0, 0, 0
        )
    body = bytearray()
    for address, data in segments:
        offset = data_at + len(body)
        memory = (memory_sizes or {}).get(address, len(data))
        if wide:
            out += struct.pack("<IIQQQQQQ", 1, 5, offset, address, address, len(data), memory, 4)
        else:
            out += struct.pack("<IIIIIIII", 1, offset, address, address, len(data), memory, 5, 4)
        body += data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out + body))
    return path


def listing_image(files: Mapping[str, bytes], size: int, block_size: int = 512) -> bytes:
    """A Listing image holding ``files`` (path from the root, without the
    leading slash) and the directories they sit in."""
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for name, data in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return Listing().build(root, size, {"block_size": block_size})
