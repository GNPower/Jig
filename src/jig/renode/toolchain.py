"""Compatibility names for the first command line, and older managed installs.

``jig.renode.install`` (``jig renode install``) and ``jig.renode.discover``
replaced the pinned-artifact installer that lived here. The names below keep
``jig setup`` and the first ``jig doctor`` working until the command line
moves to ``jig.renode.commands``.

Managed installs made before the move live in
``<cache>/tools/renode-<version>-<host>`` with a marker file; discovery still
finds them, and one recorded under the marker's older name is adopted once by
writing the current marker beside it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from jig import project
from jig.renode.assets import ASSETS, DEFAULT_VERSION
from jig.renode.assets import host_platform as _platform

log = logging.getLogger(__name__)

ENV_RENODE = "JIG_RENODE"
"""A Renode executable, or the directory holding it (see ``jig.renode.discover``)."""

RENODE_VERSION = DEFAULT_VERSION

_MARKER = ".jig-install.json"
_LEGACY_MARKER = ".testpoint-install.json"  # older name: accepted once, rewritten

LEGACY_HOSTS = {"windows": "win-x64", "linux": "linux-x64"}
"""Host names of the older layout, by platform key of the current one."""


@dataclass(frozen=True)
class Artifact:
    """A pinned Renode archive under the older names (``jig setup``)."""

    name: str
    version: str
    host: str
    url: str
    sha256: str
    size: int
    executable: str

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @property
    def install_name(self) -> str:
        return f"{self.name}-{self.version}-{self.host}"


RENODE: dict[str, Artifact] = {
    host: Artifact(
        name="renode",
        version=asset.version,
        host=host,
        url=asset.url,
        sha256=asset.sha256,
        size=asset.size,
        executable=asset.executable,
    )
    for host, key in LEGACY_HOSTS.items()
    for asset in ASSETS
    if asset.platform == key and asset.version == DEFAULT_VERSION
}


def host_platform() -> str:
    """``windows`` or ``linux``: the older host names (``jig setup --host``)."""
    key = _platform()
    for host, platform_key in LEGACY_HOSTS.items():
        if platform_key == key:
            return host
    return key


def legacy_dir(version: str, host: str) -> Path:
    """Where the older installer put Renode ``version`` for ``host``."""
    return project.tools_dir() / f"renode-{version}-{host}"


def recorded_digest(marker: Path) -> str | None:
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    digest = recorded.get("sha256") if isinstance(recorded, dict) else None
    return digest if isinstance(digest, str) else None


def legacy_install_ok(directory: Path, sha256: str, executable: str) -> bool:
    """Whether ``directory`` holds an older managed install of the archive
    ``sha256``; adopts a marker under the older name once."""
    if not (directory / executable).is_file():
        return False
    marker = directory / _MARKER
    legacy = directory / _LEGACY_MARKER
    if not marker.is_file() and recorded_digest(legacy) == sha256:
        marker.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
        log.info("adopted %s (recorded as %s)", directory, _LEGACY_MARKER)
    return recorded_digest(marker) == sha256


def install(artifact: Artifact, *, force: bool = False) -> Path:
    """Install ``artifact`` with ``jig.renode.install`` and return its executable.

    Downloads need consent: a yes at the terminal prompt, or JIG_ASSUME_YES=1."""
    from jig.renode import install as installer
    from jig.renode.assets import find_asset

    asset = find_asset(artifact.version, LEGACY_HOSTS.get(artifact.host, artifact.host))
    managed = installer.install(
        asset, force=force, allow_download=installer.consent(asset, installer.install_dir(asset))
    )
    return managed.executable


def renode_executable(*, provision: bool = False) -> Path:
    """The Renode that discovery picks (``jig.renode.discover.find_renode``)."""
    from jig.errors import ToolchainError
    from jig.renode.discover import find_renode, load_project_config

    try:
        return find_renode(explicit=None, config=load_project_config()).executable
    except ToolchainError:
        if not provision or os.environ.get(ENV_RENODE):
            raise
    return install(RENODE[host_platform()])
