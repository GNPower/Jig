"""The folder a file-system format builds from, and the view of an image's
files that the formats share."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig.errors import ConfigError
from jig.storage.formats import Entry
from jig.storage.formats._tree import Source, TreeView, normal, read_folder

from .storage_support import make_tree


def test_the_folder_is_read_in_name_order(tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "f", {"b": {"z.txt": b"z", "a.txt": b"a"}, "B.txt": b"", "a": {}})
    tree = read_folder(folder)
    assert [item.path for item in tree.walk()] == ["/B.txt", "/a", "/b", "/b/a.txt", "/b/z.txt"]
    assert [item.size for item in tree.walk()] == [0, 0, 0, 1, 1]


def test_a_file_that_changes_during_the_build(tmp_path: Path) -> None:
    (tmp_path / "grows.txt").write_bytes(b"12345")
    item = Source("grows.txt", "/grows.txt", tmp_path / "grows.txt", "file", 3)
    with pytest.raises(ConfigError, match="changed from 3 to 5 bytes"):
        item.read()
    gone = Source("gone.txt", "/gone.txt", tmp_path / "gone.txt", "file", 3)
    with pytest.raises(ConfigError, match="can't read"):
        gone.read()


def link(target: Path, at: Path) -> None:
    try:
        at.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("this host doesn't let the tests make symbolic links")


def test_links_are_followed_but_not_into_a_loop(tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "f", {"real": {"a.txt": b"a"}})
    link(folder / "real", folder / "alias")
    assert [item.path for item in read_folder(folder).walk()] == [
        "/alias",
        "/alias/a.txt",
        "/real",
        "/real/a.txt",
    ]
    link(folder, folder / "real" / "up")
    with pytest.raises(ConfigError, match="a folder that holds it"):
        read_folder(folder)


def test_a_broken_link_is_neither_file_nor_folder(tmp_path: Path) -> None:
    folder = make_tree(tmp_path / "f", {})
    link(tmp_path / "missing", folder / "broken")
    with pytest.raises(ConfigError, match="is neither a file nor a folder"):
        read_folder(folder)


@pytest.mark.parametrize(
    ("path", "found"),
    [
        ("/", "/"),
        ("", "/"),
        ("a//b/", "/a/b"),
        ("//a", "/a"),
        ("/a/./b/../c", "/a/c"),
        ("/..", "/"),
    ],
)
def test_paths_are_from_the_root(path: str, found: str) -> None:
    assert normal(path) == found


def test_a_view_folds_names_it_treats_as_one() -> None:
    entries = [Entry("/Dir", "Dir", "dir", 0), Entry("/Dir/File.txt", "File.txt", "file", 2)]
    view = TreeView(entries, lambda entry: b"hi", str.upper)
    assert view.stat("/dir/FILE.TXT").path == "/Dir/File.txt"
    assert view.read("/DIR/file.txt") == b"hi"
    assert [entry.path for entry in view.walk()] == ["/Dir", "/Dir/File.txt"]
    exact = TreeView(entries, lambda entry: b"hi")
    assert not exact.exists("/dir")
