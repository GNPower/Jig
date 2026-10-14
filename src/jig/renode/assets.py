"""The Renode release assets Jig installs, pinned by size and SHA-256.

The table comes from the digests GitHub publishes for the release
(``gh api repos/renode/renode/releases/tags/v1.17.0``, field ``digest`` of
each asset), copied here once; Jig never queries the GitHub API at run time.
Asset names are listed in full rather than built from a template: 1.16.x
shipped Mono builds under the names 1.17.0 uses for .NET builds.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass

from jig.errors import ToolchainError

RELEASES = "https://github.com/renode/renode/releases/download"

PLATFORMS = ("win-x64", "linux-x64", "linux-arm64", "osx-arm64")


@dataclass(frozen=True)
class Asset:
    """One Renode release archive for one platform."""

    version: str
    platform: str
    name: str
    size: int
    sha256: str
    executable: str
    """The launcher, relative to the install directory (the archive's top folder)."""

    @property
    def url(self) -> str:
        return f"{RELEASES}/v{self.version}/{self.name}"

    @property
    def key(self) -> str:
        """``<version>-<platform>``, the install's directory name."""
        return f"{self.version}-{self.platform}"


ASSETS: tuple[Asset, ...] = (
    Asset(
        version="1.17.0",
        platform="win-x64",
        name="renode-1.17.0.windows-portable.zip",
        size=106_735_414,
        sha256="18bcf145422039e8702c3e5f9864c7787561bfd5deb7a3ee6e168ecf19e2b9db",
        executable="renode.exe",
    ),
    Asset(
        version="1.17.0",
        platform="linux-x64",
        name="renode-1.17.0.linux-portable.tar.gz",
        size=63_459_129,
        sha256="4ba7c68b59e2447f188ef4b4b112fcccd2802582c460beedceb63b84e5605a5f",
        executable="renode",
    ),
    Asset(
        version="1.17.0",
        platform="linux-arm64",
        name="renode-1.17.0.linux-arm64-portable.tar.gz",
        size=61_595_258,
        sha256="e9051578411441bfb47b77de0381cfba3badce03663367fab561472fd7807f1f",
        executable="renode",
    ),
    Asset(
        version="1.17.0",
        platform="osx-arm64",
        name="renode-1.17.0.osx-arm64-portable.dmg",
        size=75_720_019,
        sha256="63b1fb691207f503cea937e4ec8fad3e068a517d0abac3c24c0206c62f6c4d12",
        executable="Renode.app/Contents/MacOS/renode",
    ),
)

DEFAULT_VERSION = "1.17.0"


def versions() -> list[str]:
    """Versions with assets, newest first."""
    from packaging.version import Version

    return sorted({a.version for a in ASSETS}, key=Version, reverse=True)


def host_platform() -> str:
    """This host's platform key: ``win-x64``, ``linux-x64``, ``linux-arm64`` or ``osx-arm64``."""
    system = platform.system()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    intel = machine in ("x86_64", "amd64")
    if system == "Windows" and intel:
        return "win-x64"
    if system == "Linux" and (intel or arm):
        return "linux-arm64" if arm else "linux-x64"
    if system == "Darwin" and arm:
        return "osx-arm64"
    raise ToolchainError(
        f"Renode publishes no portable build for {system}/{machine}; install Renode "
        "yourself and point JIG_RENODE at it"
    )


def find_asset(version: str | None = None, platform_key: str | None = None) -> Asset:
    """The asset for ``version`` (default: the newest) on ``platform_key`` (default: this host)."""
    wanted_version = version or versions()[0]
    wanted_platform = platform_key or host_platform()
    for asset in ASSETS:
        if asset.version == wanted_version and asset.platform == wanted_platform:
            return asset
    known = ", ".join(sorted({f"{a.version} ({a.platform})" for a in ASSETS}))
    raise ToolchainError(
        f"Jig has no pinned Renode {wanted_version} for {wanted_platform}; it knows {known}"
    )
