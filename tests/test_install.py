"""Managed Renode installs: the pinned table, archive checks, unpacking, consent and locking."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import pytest

from jig.errors import ToolchainError
from jig.renode import assets, install, toolchain
from jig.renode.assets import Asset

# A local copy of renode-1.17.0.windows-portable.zip, for the slow --from test.
CACHED_ZIP = Path(os.environ.get("JIG_TEST_RENODE_ZIP", "renode-1.17.0.windows-portable.zip"))


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _tar(path: Path, members: dict[str, bytes], mode: int = 0o755) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))
    return path


def _asset_for(path: Path, platform: str, executable: str, version: str = "9.9.0") -> Asset:
    data = path.read_bytes()
    kind = "windows-portable.zip" if path.suffix == ".zip" else "linux-portable.tar.gz"
    return Asset(
        version=version,
        platform=platform,
        name=f"renode-{version}.{kind}",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        executable=executable,
    )


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> list[Asset]:
    """The table of pinned assets, extended by the test."""
    table = list(assets.ASSETS)
    monkeypatch.setattr(install, "ASSETS", table)
    return table


def test_the_table_pins_four_platforms_of_the_release() -> None:
    by_platform = {a.platform: a for a in assets.ASSETS if a.version == "1.17.0"}
    assert sorted(by_platform) == ["linux-arm64", "linux-x64", "osx-arm64", "win-x64"]
    windows = by_platform["win-x64"]
    assert windows.size == 106_735_414
    assert windows.sha256.startswith("18bcf145")
    assert windows.url == (
        "https://github.com/renode/renode/releases/download/v1.17.0/"
        "renode-1.17.0.windows-portable.zip"
    )
    assert by_platform["linux-x64"].sha256.startswith("4ba7c68b")
    assert by_platform["linux-arm64"].size == 61_595_258
    assert by_platform["osx-arm64"].name.endswith(".dmg")
    for asset in assets.ASSETS:
        assert len(asset.sha256) == 64 and int(asset.sha256, 16) >= 0
        assert asset.url.startswith("https://github.com/renode/renode/releases/download/v")


def test_platform_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = {
        ("Windows", "AMD64"): "win-x64",
        ("Linux", "x86_64"): "linux-x64",
        ("Linux", "aarch64"): "linux-arm64",
        ("Darwin", "arm64"): "osx-arm64",
    }
    for (system, machine), key in cases.items():
        monkeypatch.setattr(assets.platform, "system", lambda s=system: s)
        monkeypatch.setattr(assets.platform, "machine", lambda m=machine: m)
        assert assets.host_platform() == key
    monkeypatch.setattr(assets.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(assets.platform, "machine", lambda: "x86_64")
    with pytest.raises(ToolchainError, match="no portable build for Darwin/x86_64"):
        assets.host_platform()
    with pytest.raises(ToolchainError, match=r"no pinned Renode 1\.12\.0"):
        assets.find_asset("1.12.0", "linux-x64")


def test_install_from_a_zip_verifies_unpacks_and_marks(tmp_path: Path, pinned: list[Asset]) -> None:
    archive = _zip(
        tmp_path / "renode.zip",
        {
            "renode_9.9.0-portable/renode.exe": b"MZ",
            "renode_9.9.0-portable/licenses/renode-license": b"MIT",
        },
    )
    asset = _asset_for(archive, "win-x64", "renode.exe")
    pinned.append(asset)
    managed = install.install(asset, source=archive)
    assert managed.directory == install.renode_dir() / "9.9.0-win-x64"
    assert managed.executable == managed.directory / "renode.exe"
    assert (managed.directory / "licenses" / "renode-license").read_bytes() == b"MIT"
    marker = json.loads((managed.directory / install.MARKER).read_text(encoding="utf-8"))
    assert marker["sha256"] == asset.sha256 and marker["size"] == asset.size
    assert marker["source"] == str(archive)
    assert install.find_managed(asset) == managed
    assert not list(install.renode_dir().glob(".9.9.0-win-x64.*")), "staging left behind"
    # Installing again finds the verified install and leaves it alone.
    (managed.directory / "keep").write_text("x", encoding="utf-8")
    assert install.install(asset, source=archive).directory == managed.directory
    assert (managed.directory / "keep").exists()
    # --force replaces it.
    install.install(asset, source=archive, force=True)
    assert not (managed.directory / "keep").exists()


def test_install_from_a_tarball_keeps_the_executable_runnable(
    tmp_path: Path, pinned: list[Asset]
) -> None:
    archive = _tar(
        tmp_path / "renode.tar.gz",
        {"renode_9.9.0-portable/renode": b"#!/bin/sh\n", "renode_9.9.0-portable/Renode.dll": b""},
    )
    asset = _asset_for(archive, "linux-x64", "renode")
    pinned.append(asset)
    managed = install.install(asset, source=archive)
    assert managed.executable.read_bytes() == b"#!/bin/sh\n"
    if os.name != "nt":
        assert os.access(managed.executable, os.X_OK)
    assert install.managed_installs("linux-x64")[0].asset == asset


def test_a_checksum_mismatch_is_refused(tmp_path: Path, pinned: list[Asset]) -> None:
    archive = _zip(tmp_path / "renode.zip", {"renode_9.9.0-portable/renode.exe": b"MZ"})
    asset = _asset_for(archive, "win-x64", "renode.exe")
    data = bytearray(archive.read_bytes())
    data[-1] ^= 0xFF  # same size, other bytes
    tampered = tmp_path / "tampered.zip"
    tampered.write_bytes(bytes(data))
    with pytest.raises(ToolchainError, match=r"is not renode-9\.9\.0.*sha256"):
        install.install(asset, source=tampered)
    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(bytes(data[:-10]))
    with pytest.raises(ToolchainError, match=r"bytes, the pinned archive has"):
        install.install(asset, source=truncated)
    assert not install.install_dir(asset).exists()


def test_an_archive_member_outside_the_install_is_refused(
    tmp_path: Path, pinned: list[Asset]
) -> None:
    archive = _zip(
        tmp_path / "evil.zip",
        {"renode_9.9.0-portable/renode.exe": b"MZ", "../outside.txt": b"x"},
    )
    asset = _asset_for(archive, "win-x64", "renode.exe")
    with pytest.raises(ToolchainError, match="escapes the install directory"):
        install.install(asset, source=archive)
    assert not (install.renode_dir() / "outside.txt").exists()
    assert not install.install_dir(asset).exists()


def test_a_download_needs_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    asset = assets.find_asset("1.17.0", "linux-x64")

    def no_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("downloaded without consent")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    with pytest.raises(install.ConsentError, match="--yes"):
        install.install(asset)
    monkeypatch.delenv("JIG_ASSUME_YES", raising=False)
    assert not install.consent(asset, install.install_dir(asset))  # no terminal under pytest
    monkeypatch.setenv("JIG_ASSUME_YES", "1")
    assert install.consent(asset, install.install_dir(asset))


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_a_download_streams_through_the_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: list[Asset]
) -> None:
    archive = _zip(tmp_path / "renode.zip", {"renode_9.9.0-portable/renode.exe": b"MZ"})
    asset = _asset_for(archive, "win-x64", "renode.exe")
    pinned.append(asset)
    requests: list[str] = []
    attempts = {"count": 0}

    def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        requests.append(request.full_url)
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("connection reset")
        return _Response(archive.read_bytes())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(install.time, "sleep", lambda _seconds: None)
    messages: list[str] = []
    managed = install.install(asset, allow_download=True, progress=messages.append)
    assert requests == [asset.url, asset.url]
    assert managed.executable.is_file()
    assert install.archive_path(asset).read_bytes() == archive.read_bytes()
    assert any("100%" in m for m in messages)
    # A wrong download is deleted and nothing is installed.
    install.remove(asset)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Response(b"x" * asset.size))
    with pytest.raises(ToolchainError, match="nothing was installed"):
        install.install(asset, allow_download=True, progress=messages.append)
    assert not install.archive_path(asset).exists()
    assert not install.install_dir(asset).exists()


def test_installs_take_turns(tmp_path: Path) -> None:
    outcome: list[str] = []
    with install.install_lock():

        def second() -> None:
            try:
                with install.install_lock(timeout=0.3):
                    outcome.append("entered")
            except ToolchainError as exc:
                outcome.append(str(exc))

        thread = threading.Thread(target=second)
        thread.start()
        thread.join()
    assert len(outcome) == 1 and outcome[0].startswith("another Jig process holds")
    with install.install_lock(timeout=0.3):
        pass


def test_remove_deletes_the_install_and_its_archive(tmp_path: Path, pinned: list[Asset]) -> None:
    archive = _zip(tmp_path / "renode.zip", {"renode_9.9.0-portable/renode.exe": b"MZ"})
    asset = _asset_for(archive, "win-x64", "renode.exe")
    pinned.append(asset)
    install.install(asset, source=archive)
    install.archive_path(asset).parent.mkdir(parents=True, exist_ok=True)
    install.archive_path(asset).write_bytes(archive.read_bytes())
    removed = install.remove(asset)
    assert removed == [install.install_dir(asset), install.archive_path(asset)]
    assert install.find_managed(asset) is None
    assert install.remove(asset) == []


def test_an_install_of_the_older_installer_is_adopted_once() -> None:
    asset = assets.find_asset("1.17.0", "win-x64")
    directory = toolchain.legacy_dir("1.17.0", "windows")
    directory.mkdir(parents=True)
    (directory / "renode.exe").write_bytes(b"MZ")
    legacy_marker = directory / toolchain._LEGACY_MARKER
    legacy_marker.write_text(json.dumps({"sha256": asset.sha256}), encoding="utf-8")
    found = install.managed_installs("win-x64")
    assert [m.directory for m in found] == [directory] and found[0].legacy
    adopted = json.loads((directory / install.MARKER).read_text(encoding="utf-8"))
    assert adopted["sha256"] == asset.sha256
    legacy_marker.write_text(json.dumps({"sha256": "0" * 64}), encoding="utf-8")
    (directory / install.MARKER).unlink()
    assert install.managed_installs("win-x64") == []


@pytest.mark.slow
def test_install_from_the_cached_windows_portable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--from`` with the real 1.17.0 Windows portable (106.7 MB): no download."""
    if not CACHED_ZIP.is_file():
        pytest.skip(f"no cached archive at {CACHED_ZIP} (set JIG_TEST_RENODE_ZIP)")
    monkeypatch.delenv("JIG_RENODE", raising=False)
    monkeypatch.setenv("JIG_CONFIG_DIR", str(tmp_path / "config"))
    asset = assets.find_asset("1.17.0", "win-x64")
    managed = install.install(asset, source=CACHED_ZIP)
    assert managed.executable == install.install_dir(asset) / "renode.exe"
    assert (managed.directory / "Renode.dll").is_file()
    assert (managed.directory / ".renode-root").is_file()
    assert install.find_managed(asset) == managed
    if assets.host_platform() == "win-x64":
        from jig.renode import discover

        renode = discover.find_renode(explicit=None, config=None)
        assert renode.executable == managed.executable and renode.source == "managed"
        assert renode.status == "tested"
