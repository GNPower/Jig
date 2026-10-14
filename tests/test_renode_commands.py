"""``jig doctor``, ``jig renode``, ``jig models`` and ``jig cosim``, mounted as alpha's parser
mounts them, without a real Renode."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tarfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

from jig.renode import assets, commands, discover, doctor, install, models
from jig.renode.assets import Asset

from .renode_support import fake_renode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jig")
    subparsers = parser.add_subparsers(dest="command", required=True)
    commands.add_commands(subparsers)
    return parser


def _run(*argv: str) -> int:
    args = _parser().parse_args(argv)
    result = args.func(args)
    assert isinstance(result, int)
    return result


@pytest.fixture(autouse=True)
def _quiet_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("JIG_RENODE", "RENODE_ROOT", "JIG_ASSUME_YES"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("JIG_CONFIG_DIR", str(tmp_path / "user-config"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setattr(discover, "standard_locations", lambda: [])


@pytest.fixture
def foreign_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Asset, Path]:
    """A pinned archive for the platform this host is not (installing it runs nothing)."""
    other = "linux-x64" if os.name == "nt" else "win-x64"
    archive = tmp_path / ("renode.tar.gz" if other == "linux-x64" else "renode.zip")
    executable = "renode" if other == "linux-x64" else "renode.exe"
    member = f"renode_9.9.0-portable/{executable}"
    if other == "linux-x64":
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo(member)
            info.size = 2
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(b"#!"))
        name = "renode-9.9.0.linux-portable.tar.gz"
    else:
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(member, b"MZ")
        name = "renode-9.9.0.windows-portable.zip"
    data = archive.read_bytes()
    asset = Asset("9.9.0", other, name, len(data), hashlib.sha256(data).hexdigest(), executable)
    table = (*assets.ASSETS, asset)
    monkeypatch.setattr(assets, "ASSETS", table)
    monkeypatch.setattr(install, "ASSETS", table)
    return asset, archive


def test_the_commands_parse() -> None:
    parser = _parser()
    args = parser.parse_args(["renode", "install", "1.17.0", "--from", "r.zip", "--yes"])
    assert args.func is commands.run
    assert (args.version, args.source, args.yes, args.force) == (
        "1.17.0",
        Path("r.zip"),
        True,
        False,
    )
    args = parser.parse_args(
        ["doctor", "--json", "--board", "a", "--renode", "r", "--allow-untested-renode"]
    )
    assert (args.json, args.board, args.renode, args.allow_untested_renode) == (
        True,
        ["a"],
        Path("r"),
        True,
    )
    args = parser.parse_args(["models", "check", "--sdk", "--board", "a", "--board", "b"])
    assert args.sdk and args.board == ["a", "b"]
    args = parser.parse_args(["models", "ide", "--board", "a", "-o", "x.csproj"])
    assert args.output == Path("x.csproj")
    args = parser.parse_args(
        ["cosim", "build", "out", "--top", "t", "--sources", "s.f", "--clock-mhz", "10",
         "-G", "WIDTH=8", "--irq", "irq"]
    )  # fmt: skip
    assert (args.top, args.clock_mhz, args.parameters, args.interrupts) == (
        "t",
        10,
        ["WIDTH=8"],
        ["irq"],
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["renode"])


def test_a_download_without_consent_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("downloaded without consent")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    assert _run("renode", "install", "--platform", "linux-arm64") == commands.EXIT_USAGE
    error = capsys.readouterr().err
    assert "--yes" in error and "JIG_ASSUME_YES=1" in error


def test_install_list_and_remove(
    foreign_asset: tuple[Asset, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    asset, archive = foreign_asset
    assert (
        _run("renode", "install", "9.9.0", "--platform", asset.platform, "--from", str(archive))
        == 0
    )
    out = capsys.readouterr()
    assert out.out.strip().endswith(asset.executable)
    assert "LGPL" in out.err
    assert _run("renode", "list", "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(row["version"], row["platform"]) for row in listed] == [("9.9.0", asset.platform)]
    assert _run("renode", "install", "9.9.0", "--platform", asset.platform) == 0
    assert "is installed" in capsys.readouterr().out
    assert _run("renode", "remove", "9.9.0", "--platform", asset.platform) == 0
    assert "removed" in capsys.readouterr().out
    assert _run("renode", "remove", "9.9.0", "--platform", asset.platform) == 1
    assert _run("renode", "list") == 0
    assert "no Jig-managed Renode" in capsys.readouterr().out


def test_a_tampered_archive_fails_as_infrastructure(
    tmp_path: Path, foreign_asset: tuple[Asset, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    asset, archive = foreign_asset
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    tampered = tmp_path / "tampered"
    tampered.write_bytes(bytes(data))
    code = _run("renode", "install", "9.9.0", "--platform", asset.platform, "--from", str(tampered))
    assert code == commands.EXIT_INFRASTRUCTURE
    assert "sha256" in capsys.readouterr().err


def test_path_prints_the_renode_discovery_picks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("renode", "path") == commands.EXIT_INFRASTRUCTURE
    assert "jig renode install" in capsys.readouterr().err
    launcher = fake_renode(tmp_path / "renode")
    monkeypatch.setenv("JIG_RENODE", str(launcher))
    assert _run("renode", "path") == 0
    out = capsys.readouterr()
    assert out.out.strip() == str(launcher)
    assert "tested, from JIG_RENODE" in out.err


def test_models_ide_writes_a_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    launcher = fake_renode(tmp_path / "renode")
    for name in models.SDK_REFERENCES:
        (launcher.parent / f"{name}.dll").write_bytes(b"")
    board = tmp_path / "board"
    (board / "models").mkdir(parents=True)
    (board / "models" / "Part.cs").write_text("namespace X { }\n", encoding="utf-8")
    assert _run("models", "ide", "--board", str(board), "--renode", str(launcher)) == 0
    written = Path(capsys.readouterr().out.strip())
    assert written == board / "models" / "Board.Models.csproj"
    links = [e.get("Link") for e in ET.parse(written).iter("Compile")]
    assert "board/Part.cs" in links and "jig/Harness/Bridge.cs" in links


def test_doctor_reports_each_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = fake_renode(tmp_path / "renode")
    report = doctor.run(explicit=launcher, config=None, start=False)
    names = [check.name for check in report.checks]
    assert names == ["python", "platform", "jig", "paths", "discovery", "version", "managed",
                     "dotnet", "disk"]  # fmt: skip
    assert report.ok
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["discovery"] == statuses["version"] == "pass"
    json.dumps(report.as_dict(), default=str)
    missing = doctor.run(explicit=tmp_path / "nothing", config=None, start=False)
    assert not missing.ok
    assert (
        "no Renode executable" in next(c for c in missing.checks if c.name == "discovery").summary
    )


def test_doctor_fails_when_renode_does_not_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    launcher = fake_renode(tmp_path / "renode")  # answers --version, then exits
    assert _run("doctor", "--json", "--renode", str(launcher)) == commands.EXIT_PROBLEMS
    report = json.loads(capsys.readouterr().out)
    assert not report["ok"]
    start = next(c for c in report["checks"] if c["name"] == "start-up")
    assert start["status"] == "fail" and "exited" in start["summary"]
