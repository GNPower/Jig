"""What the file-system formats share: the folder a build reads, and the
read-only view of the files in an image."""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from jig.errors import ConfigError
from jig.storage.formats import Entry

ROOT = Entry(path="/", name="", kind="dir", size=0)


@dataclass(frozen=True)
class Source:
    """A file or a folder in the folder an image is built from."""

    name: str  # "" for the folder itself
    path: str  # where it goes in the volume: "/" for the folder itself, "/config/a.txt"
    host: Path
    kind: str  # "file" or "dir"
    size: int  # bytes, 0 for a folder
    children: tuple[Source, ...] = ()  # sorted by name

    def walk(self) -> Iterator[Source]:
        """Everything under this folder, depth first, each folder's entries
        in name order."""
        for child in self.children:
            yield child
            yield from child.walk()

    def read(self) -> bytes:
        """The file's bytes, which must still be as many as when the folder
        was read."""
        try:
            data = self.host.read_bytes()
        except OSError as error:
            raise ConfigError(f"can't read {self.host}: {error}") from error
        if len(data) != self.size:
            raise ConfigError(
                f"{self.host} changed from {self.size} to {len(data)} bytes "
                "while the image was being built"
            )
        return data


def read_folder(folder: Path) -> Source:
    """``folder`` and everything in it. Each folder's entries are sorted by
    name (Python's string order), so a build that follows this order writes
    the same image from the same folder on every host. Symbolic links are
    followed; a link to a folder that holds it is an error."""
    if not folder.is_dir():
        if folder.exists():
            raise ConfigError(f"{folder} is a file; a file system is built from a folder")
        raise ConfigError(f"no folder {folder}")
    return _read(folder, "/", "", ())


def _read(host: Path, path: str, name: str, above: tuple[Path, ...]) -> Source:
    try:
        real = host.resolve()
        if real in above:
            raise ConfigError(f"{host} links to {real}, a folder that holds it")
        children = []
        for item in sorted(host.iterdir(), key=lambda item: item.name):
            inside = posixpath.join(path, item.name)
            if item.is_dir():
                children.append(_read(item, inside, item.name, (*above, real)))
            elif item.is_file():
                children.append(Source(item.name, inside, item, "file", item.stat().st_size))
            else:
                raise ConfigError(f"{item} is neither a file nor a folder")
    except OSError as error:
        raise ConfigError(f"can't read {host}: {error}") from error
    return Source(name, path, host, "dir", 0, tuple(children))


def normal(path: str) -> str:
    """``path`` from the volume's root with "." and ".." resolved:
    "config//a.txt/" is "/config/a.txt"."""
    if not isinstance(path, str):
        raise ConfigError(f'a path in an image is a string such as "/config/a.txt", not {path!r}')
    return posixpath.normpath("/" + path.lstrip("/"))


def _same(text: str) -> str:
    return text


class TreeView:
    """A file system read from an image. The entries are listed once, when
    the image is opened; a file's bytes are read when asked for. ``fold``
    maps a path to the key two names that the file system treats as one
    share (``str.upper`` for FAT, which ignores case)."""

    def __init__(
        self,
        entries: Iterable[Entry],
        contents: Callable[[Entry], bytes],
        fold: Callable[[str], str] = _same,
    ) -> None:
        self._fold = fold
        self._contents = contents
        self._entries: dict[str, Entry] = {fold("/"): ROOT}
        self._inside: dict[str, list[Entry]] = {fold("/"): []}
        for entry in entries:
            key = fold(entry.path)
            self._entries[key] = entry
            self._inside.setdefault(fold(posixpath.dirname(entry.path)), []).append(entry)
            if entry.kind == "dir":
                self._inside.setdefault(key, [])
        for listed in self._inside.values():
            listed.sort(key=lambda entry: entry.name)

    def files(self, path: str = "/") -> list[Entry]:
        """The entries directly inside ``path``, sorted by name."""
        entry = self.stat(path)
        if entry.kind != "dir":
            raise ConfigError(f"{entry.path} is a file, not a directory")
        return list(self._inside[self._fold(entry.path)])

    def walk(self) -> list[Entry]:
        """Every entry under the root, depth first, each directory's entries
        sorted by name."""
        found: list[Entry] = []
        stack = [iter(self._inside[self._fold("/")])]
        while stack:
            entry = next(stack[-1], None)
            if entry is None:
                stack.pop()
                continue
            found.append(entry)
            if entry.kind == "dir":
                stack.append(iter(self._inside[self._fold(entry.path)]))
        return found

    def read(self, path: str) -> bytes:
        """A file's contents; a ConfigError when ``path`` isn't a file."""
        entry = self.stat(path)
        if entry.kind != "file":
            raise ConfigError(f"{entry.path} is a directory, not a file")
        return self._contents(entry)

    def exists(self, path: str) -> bool:
        """Whether a file or directory is at ``path``."""
        return self._fold(normal(path)) in self._entries

    def stat(self, path: str) -> Entry:
        """The entry at ``path``; a ConfigError when nothing is there."""
        where = normal(path)
        entry = self._entries.get(self._fold(where))
        if entry is None:
            raise ConfigError(f"no file or directory {where} in the image")
        return entry
