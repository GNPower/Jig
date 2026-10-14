"""Finding Renode: the source order, version checks and the probe cache, on fake installs."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from jig.errors import ConfigError, ToolchainError
from jig.renode import discover, install
from jig.renode.assets import find_asset
from jig.renode.discover import RenodeConfig, find_renode, survey

from .renode_support import LAUNCHER, WINDOWS, calls, fake_renode

WINDOWS_VERSION = """\
Renode v1.17.0
  build: 1.17.0+20260906gitf1dd1b4af
  build type: Release
  runtime: .NET 8.0.10
"""


@pytest.fixture(autouse=True)
def _no_ambient_renode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery sees only what each test puts in place."""
    for variable in ("JIG_RENODE", "RENODE_ROOT", "JIG_ASSUME_YES"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("JIG_CONFIG_DIR", str(tmp_path / "user-config"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setattr(discover, "standard_locations", lambda: [])


def test_the_version_output_is_parsed() -> None:
    fields = discover.parse_version_output(WINDOWS_VERSION)
    assert fields == {
        "version": "1.17.0",
        "build": "1.17.0+20260906gitf1dd1b4af",
        "build_type": "Release",
        "runtime": ".NET 8.0.10",
    }
    with pytest.raises(ToolchainError, match="not Renode's --version output"):
        discover.parse_version_output("Python 3.13.16")


def test_builds_are_classified() -> None:
    assert discover.classify("1.17.0", "1.17.0+20260906gitf1dd1b4af") == "tested"
    assert discover.classify("1.17.0", "1.17.0+20260907gitf1dd1b4af") == "tested"
    assert discover.classify("1.17.0", "1.17.0+20261001gitabcdef012") == "supported"
    assert discover.classify("1.17.3", "1.17.3") == "supported"
    assert discover.classify("1.16.1", "1.16.1") == "unsupported"
    assert discover.classify("1.18.0", "1.18.0") == "unsupported"


def test_a_project_table_is_read_relative_to_its_file(tmp_path: Path) -> None:
    config = RenodeConfig.from_table({"path": "tools/renode", "version": "==1.17.0"}, tmp_path)
    assert config.path == tmp_path / "tools" / "renode"
    assert config.version == "==1.17.0"
    assert RenodeConfig.from_table({}, tmp_path) == RenodeConfig()
    with pytest.raises(ConfigError, match="not a PEP 440 specifier"):
        RenodeConfig.from_table({"version": "1.17"}, tmp_path)
    with pytest.raises(ConfigError, match="unknown keys verison"):
        RenodeConfig.from_table({"verison": "==1.17.0"}, tmp_path)


def test_jig_toml_and_the_user_config_file_are_read(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "jig.toml").write_text(
        '[renode]\npath = "renode-dir"\nversion = ">=1.17,<1.18"\n', encoding="utf-8"
    )
    config = discover.load_project_config(project)
    assert config == RenodeConfig(path=project / "renode-dir", version=">=1.17,<1.18")
    user = discover.user_config_file()
    assert user == tmp_path / "user-config" / "config.toml"
    user.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    user.write_text(f"[renode]\npath = '{elsewhere.as_posix()}'\n", encoding="utf-8")
    loaded = discover.user_config()
    assert loaded is not None and loaded.path == elsewhere


def test_an_explicit_path_wins_and_fails_loudly(tmp_path: Path) -> None:
    good = fake_renode(tmp_path / "good")
    old = fake_renode(tmp_path / "old", "1.16.1")
    assert find_renode(explicit=good, config=None).executable == good
    assert find_renode(explicit=good.parent, config=None).source == "--renode"
    with pytest.raises(ToolchainError, match=r"--renode: no Renode executable there"):
        find_renode(explicit=tmp_path / "missing", config=None)
    with pytest.raises(ToolchainError, match=r"outside the supported range.*allow-untested"):
        find_renode(explicit=old, config=None)
    accepted = find_renode(explicit=old, config=None, allow_untested=True)
    assert accepted.version == "1.16.1" and accepted.status == "unsupported"


def test_explicit_sources_take_turns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from_env = fake_renode(tmp_path / "env")
    from_project = fake_renode(tmp_path / "project")
    from_user = fake_renode(tmp_path / "user")
    user = discover.user_config_file()
    user.parent.mkdir(parents=True)
    user.write_text(f"[renode]\npath = '{from_user.parent.as_posix()}'\n", encoding="utf-8")
    assert find_renode(explicit=None, config=None).executable == from_user
    config = RenodeConfig(path=from_project)
    assert find_renode(explicit=None, config=config).executable == from_project
    monkeypatch.setenv("JIG_RENODE", str(from_env))
    found = survey(explicit=None, config=config)
    assert found.selected is not None and found.selected.executable == from_env
    states = {c.source: c.problem for c in found.candidates}
    assert states["--renode"] == "not set"
    assert states["jig.toml"] == "not used: JIG_RENODE is set"
    # An explicit source that is broken does not fall through to the next one.
    monkeypatch.setenv("JIG_RENODE", str(tmp_path / "nowhere"))
    with pytest.raises(ToolchainError, match="JIG_RENODE: no Renode executable"):
        find_renode(explicit=None, config=config)


def test_implicit_sources_take_the_first_usable_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = fake_renode(tmp_path / "path-a", "1.16.1")
    nightly = fake_renode(tmp_path / "path-b", build="1.17.0+20261001gitabcdef012")
    release = fake_renode(tmp_path / "path-c")
    monkeypatch.setenv("PATH", os.pathsep.join(str(p.parent) for p in (old, nightly, release)))
    found = find_renode(explicit=None, config=None)
    assert found.executable == nightly and found.source == "PATH"
    assert found.status == "supported"
    # A project requirement narrows the choice.
    pinned = find_renode(
        explicit=None, config=RenodeConfig(version="==1.17.0+20260906gitf1dd1b4af")
    )
    assert pinned.executable == release
    with pytest.raises(ToolchainError, match=r"no usable Renode found \(requirement ==1\.18\.0\)"):
        find_renode(explicit=None, config=RenodeConfig(version="==1.18.0"))
    report = survey(explicit=None, config=None)
    problems = [c.problem for c in report.candidates if not c.explicit]
    assert problems[0] is not None and "outside the supported range" in problems[0]
    assert problems[1] is None
    assert problems[2] is not None and problems[2].startswith("not used:")


def test_managed_installs_come_before_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    on_path = fake_renode(tmp_path / "on-path")
    monkeypatch.setenv("PATH", str(on_path.parent))
    asset = find_asset("1.17.0")
    managed_dir = install.install_dir(asset)
    managed = fake_renode(managed_dir)
    marker = {"version": "1.17.0", "platform": asset.platform, "size": asset.size}
    marker["executable"] = managed.name  # a script stands in for the real launcher
    (managed_dir / install.MARKER).write_text(
        json.dumps({**marker, "sha256": "0" * 64}), encoding="utf-8"
    )
    # A marker with another digest is not trusted.
    assert find_renode(explicit=None, config=None).executable == on_path
    (managed_dir / install.MARKER).write_text(
        json.dumps({**marker, "sha256": asset.sha256}), encoding="utf-8"
    )
    found = find_renode(explicit=None, config=None)
    assert found.executable == managed and found.source == "managed"


def test_standard_locations_and_renode_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard = fake_renode(tmp_path / "Program Files" / "Renode")
    monkeypatch.setattr(discover, "standard_locations", lambda: [standard])
    assert find_renode(explicit=None, config=None).source == "standard location"
    monkeypatch.setattr(discover, "standard_locations", lambda: [])
    root = fake_renode(tmp_path / "renode-root")
    monkeypatch.setenv("RENODE_ROOT", str(root.parent))
    found = find_renode(explicit=None, config=None)
    assert found.executable == root and found.source == "RENODE_ROOT"


def test_nothing_found_points_at_the_installer() -> None:
    with pytest.raises(ToolchainError, match=r"jig renode install"):
        find_renode(explicit=None, config=None)


def test_probes_are_cached_by_path_size_and_time(tmp_path: Path) -> None:
    launcher = fake_renode(tmp_path / "cached")
    first = discover.probe(launcher)
    second = discover.probe(launcher)
    assert first.version == second.version == "1.17.0"
    assert calls(launcher) == 1
    later = time.time() + 5
    os.utime(launcher, (later, later))
    discover.probe(launcher)
    assert calls(launcher) == 2


def test_a_failing_launcher_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = fake_renode(tmp_path / "broken", exit_code=3)
    good = fake_renode(tmp_path / "good")
    monkeypatch.setenv("PATH", os.pathsep.join([str(broken.parent), str(good.parent)]))
    report = survey(explicit=None, config=None, stop_at_first=True)
    assert report.selected is not None and report.selected.executable == good
    assert "exited with 3" in str(report.candidates[-2].problem)


def test_the_root_is_where_renode_keeps_its_marker(tmp_path: Path) -> None:
    portable = fake_renode(tmp_path / "portable")
    (portable.parent / discover.ROOT_MARKER).write_text("x", encoding="utf-8")
    found = discover.probe(portable)
    assert found.root == portable.parent
    assert found.assemblies == portable.parent and found.has_assemblies


@pytest.mark.skipif(WINDOWS, reason="a launcher script naming Renode.dll, as the deb installs")
def test_a_launcher_script_leads_to_the_assemblies(tmp_path: Path) -> None:
    opt = tmp_path / "opt" / "renode"
    (opt / "bin").mkdir(parents=True)
    (opt / "bin" / "Renode.dll").write_bytes(b"")
    (opt / "bin" / "Infrastructure.dll").write_bytes(b"")
    (opt / discover.ROOT_MARKER).write_text("x", encoding="utf-8")
    bin_dir = tmp_path / "usr" / "bin"
    launcher = fake_renode(bin_dir)
    (bin_dir / "Renode.dll").unlink()
    (bin_dir / "Infrastructure.dll").unlink()
    text = launcher.read_text(encoding="ascii")
    launcher.write_text(text + f"# exec dotnet {opt}/bin/Renode.dll\n", encoding="ascii")
    found = discover.probe(launcher)
    assert found.assemblies == opt / "bin"
    assert found.root == opt


@pytest.mark.skipif(WINDOWS, reason="symbolic links need privileges on Windows")
def test_symlinks_are_resolved_before_looking_for_renode_dll(tmp_path: Path) -> None:
    real = fake_renode(tmp_path / "real")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    (link_dir / LAUNCHER).symlink_to(real)
    found = discover.probe(link_dir / LAUNCHER)
    assert found.executable == link_dir / LAUNCHER
    assert found.assemblies == real.parent
