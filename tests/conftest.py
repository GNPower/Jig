"""Shared fixtures.

Every test runs with its own cache and output directories, with
``tests/fixtures`` (the fixture project) as the project unless it chooses
another one, and with a plugin registry of its own.
"""

from __future__ import annotations

import shutil
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from jig.system import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PS7_INIT = FIXTURES / "hdl" / "ps7_init.tcl"
BITSTREAM = bytes(range(256)) * 4  # stands in for a .bit file; nothing parses it


@pytest.fixture(autouse=True)
def _isolated_paths(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    base = tmp_path_factory.mktemp("jig-paths")
    monkeypatch.setenv("JIG_PROJECT", str(FIXTURES))
    monkeypatch.setenv("JIG_CACHE_DIR", str(base / "cache"))
    monkeypatch.setenv("JIG_OUTPUT", str(base / "out"))
    monkeypatch.delenv("JIG_KEEP_RUNS", raising=False)
    monkeypatch.delenv("JIG_BOARD_PATH", raising=False)
    yield


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[registry.Registry]:
    """A plugin registry of the test's own: what one test's plugins
    registered never reaches another."""
    fresh = registry.Registry()
    monkeypatch.setattr(registry, "REGISTRY", fresh)
    yield fresh


def make_xsa(destination: Path, tcl: Path = PS7_INIT) -> Path:
    """An XSA as far as Jig reads one: a zip holding ps7_init.tcl and a bitstream."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        archive.write(tcl, "ps7_init.tcl")
        archive.writestr("example.bit", BITSTREAM)
    return destination


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A writable copy of tests/fixtures as the project root, with its XSA built."""
    root = tmp_path / "project"
    shutil.copytree(FIXTURES, root)
    make_xsa(root / "build" / "example.xsa")
    monkeypatch.setenv("JIG_PROJECT", str(root))
    return root
