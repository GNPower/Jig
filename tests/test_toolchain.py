"""The compatibility names of ``jig.renode.toolchain`` (``jig setup``, the first ``jig doctor``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig.errors import ToolchainError
from jig.renode import discover, toolchain

from .renode_support import fake_renode


def test_the_older_names_carry_the_pins() -> None:
    assert toolchain.RENODE_VERSION == "1.17.0"
    windows = toolchain.RENODE["windows"]
    assert windows.size == 106_735_414
    assert windows.url.endswith("/v1.17.0/renode-1.17.0.windows-portable.zip")
    assert windows.install_name == "renode-1.17.0-windows"
    assert toolchain.RENODE["linux"].filename == "renode-1.17.0.linux-portable.tar.gz"
    assert toolchain.host_platform() in ("windows", "linux", "osx-arm64", "linux-arm64")


def test_renode_executable_goes_through_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JIG_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(discover, "standard_locations", lambda: [])
    monkeypatch.delenv("RENODE_ROOT", raising=False)
    launcher = fake_renode(tmp_path / "renode")
    monkeypatch.setenv("JIG_RENODE", str(launcher.parent))
    assert toolchain.renode_executable() == launcher
    monkeypatch.delenv("JIG_RENODE")
    with pytest.raises(ToolchainError, match="jig renode install"):
        toolchain.renode_executable()
