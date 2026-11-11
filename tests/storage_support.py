"""Folders to build file systems from, and what a view of one holds."""

from __future__ import annotations

import random
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jig.storage.formats import FileSystemView


def make_tree(root: Path, layout: Mapping[str, Any]) -> Path:
    """Create ``layout`` (names mapped to a file's bytes or to a folder's own
    layout) under ``root``, which may exist, and return ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    for name, item in layout.items():
        if isinstance(item, bytes):
            (root / name).write_bytes(item)
        else:
            make_tree(root / name, item)
    return root


def host_tree(root: Path) -> dict[str, bytes | None]:
    """Each path under ``root``, from the volume's root: a file's bytes, or
    None for a folder."""
    found: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        key = "/" + path.relative_to(root).as_posix()
        found[key] = path.read_bytes() if path.is_file() else None
    return found


def view_tree(view: FileSystemView) -> dict[str, bytes | None]:
    """The same for a file system read from an image."""
    return {
        entry.path: view.read(entry.path) if entry.kind == "file" else None for entry in view.walk()
    }


def nested(seed: int = 1, unit: int = 4096) -> dict[str, Any]:
    """Nested folders, empty folders, an empty file, files that span several
    blocks or clusters of ``unit`` bytes and files that end on a boundary."""
    rng = random.Random(seed)
    return {
        "README.TXT": b"a short name already\n",
        "notes.txt": b"lower case\n",
        "A file name longer than eight.dat": rng.randbytes(3 * unit + 17),
        "empty.bin": b"",
        "exact.bin": rng.randbytes(2 * unit),
        "config": {
            "settings.json": b'{"rate": 100}\n',
            "calibration": {"table.bin": rng.randbytes(unit + 1)},
        },
        "logs": {},
        "data": {
            "one": {},
            "two": {"deep": {"deeper": {"leaf.txt": b"leaf\n"}}},
            "big.bin": rng.randbytes(9 * unit + 5),
        },
    }


def files(count: int, seed: int, sizes: tuple[int, ...]) -> dict[str, Any]:
    """``count`` files of the given sizes, spread over ten folders."""
    rng = random.Random(seed)
    layout: dict[str, dict[str, bytes]] = {}
    for number in range(count):
        folder = layout.setdefault(f"folder{number % 10}", {})
        folder[f"file{number:03d}.bin"] = rng.randbytes(rng.choice(sizes))
    return layout
