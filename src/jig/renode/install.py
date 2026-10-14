"""Jig-managed Renode installs: ``jig renode install``, ``list`` and ``remove``.

An install lives in ``<cache>/renode/<version>-<platform>`` (the cache is
``$JIG_CACHE_DIR`` or the platform's user cache directory) and is trusted
only when its marker file, written last, records the size and SHA-256 that
``jig.renode.assets`` pins for that archive. Archives come from the Renode
release on GitHub and nowhere else, or from a local file (``--from``), and
are checked against the same pins before anything is unpacked.

Installing streams the download through SHA-256 with three attempts, unpacks
into a staging directory beside the target after checking every member path,
then renames the staged tree into place (with retries: on Windows, scanners
briefly lock fresh files). A file lock serialises installs and removals that
share a cache. The library never prompts: a download needs
``allow_download=True``, which the command line sets after the user agrees
(a terminal prompt, ``--yes`` or ``JIG_ASSUME_YES=1``).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from packaging.version import Version

from jig import __version__, project
from jig.errors import ToolchainError
from jig.renode import toolchain
from jig.renode.assets import ASSETS, Asset, host_platform

log = logging.getLogger(__name__)

ENV_ASSUME_YES = "JIG_ASSUME_YES"
MARKER = ".jig-install.json"
LICENSE_NOTE = (
    "Renode is MIT-licensed; its package also holds components under LGPL-2.1 (tlib) and "
    "LGPL-3.0 (NetMQ, Packet.Net). The license texts are in the install's licenses folder."
)

_CHUNK = 1 << 20
_RETRIES = 3
_LOCK_TIMEOUT = 900.0
# An unpacked install takes about three times its archive (the Windows
# portable: 106.7 MB packed, 306.9 MB unpacked); the archive stays cached.
_SPACE_FACTOR = 4


class ConsentError(ToolchainError):
    """A download needs the user's agreement first."""


@dataclass(frozen=True)
class ManagedInstall:
    """A verified install in the cache."""

    asset: Asset
    directory: Path
    executable: Path
    legacy: bool = False
    """Made by the older installer (``<cache>/tools``)."""


def renode_dir() -> Path:
    """``<cache>/renode``: managed installs, their archives and the probe cache."""
    return project.cache_dir() / "renode"


def downloads_dir() -> Path:
    return renode_dir() / "downloads"


def install_dir(asset: Asset) -> Path:
    return renode_dir() / asset.key


def archive_path(asset: Asset) -> Path:
    return downloads_dir() / asset.name


def assume_yes() -> bool:
    """Whether ``JIG_ASSUME_YES`` grants consent to downloads."""
    return os.environ.get(ENV_ASSUME_YES, "").strip().lower() in ("1", "yes", "true")


def consent(asset: Asset, target: Path) -> bool:
    """Ask at the terminal whether to download ``asset`` into ``target``;
    ``JIG_ASSUME_YES=1`` answers yes, and without a terminal the answer is no."""
    if assume_yes():
        return True
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    print(describe_download(asset, target))
    answer = input("Download and install it? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def describe_download(asset: Asset, target: Path) -> str:
    return (
        f"Renode {asset.version} for {asset.platform}\n"
        f"  from:    {asset.url}\n"
        f"  size:    {asset.size / 1e6:.1f} MB ({asset.size:,} bytes)\n"
        f"  sha256:  {asset.sha256}\n"
        f"  into:    {target}\n"
        f"  license: {LICENSE_NOTE}"
    )


# -------------------------------------------------------------------- markers


def _read_marker(directory: Path) -> dict[str, object] | None:
    try:
        data = json.loads((directory / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _verified(asset: Asset, directory: Path) -> ManagedInstall | None:
    marker = _read_marker(directory)
    if marker is None or marker.get("sha256") != asset.sha256 or marker.get("size") != asset.size:
        return None
    relative = marker.get("executable", asset.executable)
    if not isinstance(relative, str):
        return None
    executable = directory / relative
    return ManagedInstall(asset, directory, executable) if executable.is_file() else None


def managed_installs(platform_key: str | None = None) -> list[ManagedInstall]:
    """Verified installs for ``platform_key`` (default: this host), newest first."""
    try:
        key = platform_key or host_platform()
    except ToolchainError:
        return []
    found: list[ManagedInstall] = []
    for asset in ASSETS:
        if asset.platform != key:
            continue
        managed = _verified(asset, install_dir(asset))
        if managed is not None:
            found.append(managed)
        found.extend(_legacy(asset))
    return sorted(found, key=lambda m: Version(m.asset.version), reverse=True)


def _legacy(asset: Asset) -> Iterator[ManagedInstall]:
    for host, key in toolchain.LEGACY_HOSTS.items():
        if key != asset.platform:
            continue
        directory = toolchain.legacy_dir(asset.version, host)
        if toolchain.legacy_install_ok(directory, asset.sha256, asset.executable):
            yield ManagedInstall(asset, directory, directory / asset.executable, legacy=True)


def find_managed(asset: Asset) -> ManagedInstall | None:
    for managed in managed_installs(asset.platform):
        if managed.asset == asset:
            return managed
    return None


# ----------------------------------------------------------------------- lock

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def install_lock(timeout: float = _LOCK_TIMEOUT) -> Iterator[None]:
    """Hold ``<cache>/renode/.lock`` (installs and removals take turns)."""
    path = renode_dir() / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _try_lock(fd)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise ToolchainError(f"another Jig process holds {path}") from None
                time.sleep(0.5)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


# ------------------------------------------------------------ archives & files


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_archive(asset: Asset, path: Path) -> None:
    """Raise unless ``path`` has the pinned size and SHA-256 of ``asset``."""
    if not path.is_file():
        raise ToolchainError(f"{path}: no such file")
    size = path.stat().st_size
    if size != asset.size:
        raise ToolchainError(
            f"{path} is not {asset.name}: {size:,} bytes, the pinned archive has {asset.size:,}"
        )
    digest = sha256_file(path)
    if digest != asset.sha256:
        raise ToolchainError(
            f"{path} is not {asset.name}: sha256 {digest}, the pinned archive has {asset.sha256}"
        )


def _archive_ok(asset: Asset, path: Path) -> bool:
    try:
        verify_archive(asset, path)
    except ToolchainError:
        return False
    return True


def _download(asset: Asset, dest: Path, progress: Callable[[str], None]) -> None:
    """Stream ``asset`` to ``dest``, verifying size and digest before the rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(asset.url, headers={"User-Agent": f"jig-dsl/{__version__}"})
    last_error: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        digest = hashlib.sha256()
        received = 0
        next_report = 0.1
        try:
            progress(f"downloading {asset.url} (attempt {attempt} of {_RETRIES})")
            with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as out:
                for block in iter(lambda: response.read(_CHUNK), b""):
                    out.write(block)
                    digest.update(block)
                    received += len(block)
                    if received > asset.size:
                        break
                    while received / asset.size >= next_report and next_report <= 1.0:
                        progress(f"  {100 * next_report:3.0f}% of {asset.size / 1e6:.1f} MB")
                        next_report += 0.1
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            last_error = exc
            log.warning("download failed: %s", exc)
            time.sleep(2 * attempt)
            continue
        if received != asset.size or digest.hexdigest() != asset.sha256:
            part.unlink(missing_ok=True)
            raise ToolchainError(
                f"{asset.url}: expected {asset.size:,} bytes with sha256 {asset.sha256}, got "
                f"{received:,} bytes with sha256 {digest.hexdigest()}; nothing was installed"
            )
        part.replace(dest)
        return
    part.unlink(missing_ok=True)
    raise ToolchainError(f"could not download {asset.url}: {last_error}")


def _check_member(dest: Path, name: str) -> None:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
        raise ToolchainError(f"archive member escapes the install directory: {name!r}")
    target = (dest / pure).resolve()
    if dest != target and dest not in target.parents:
        raise ToolchainError(f"archive member escapes the install directory: {name!r}")


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            _check_member(dest, name)
        zf.extractall(dest)


def _extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
            return
        for member in tf.getmembers():  # Python without extraction filters (3.11.0-3.11.3)
            _check_member(dest, member.name)
            if member.issym() or member.islnk():
                _check_member(dest, str(PurePosixPath(member.name).parent / member.linkname))
        tf.extractall(dest)


def _extract_dmg(archive: Path, dest: Path) -> None:
    if platform.system() != "Darwin":
        raise ToolchainError(f"{archive.name} is a macOS disk image; install it on macOS")
    mount = Path(tempfile.mkdtemp(prefix="jig-renode-dmg-"))
    try:
        subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
             "-mountpoint", str(mount), str(archive)],
            check=True, capture_output=True,
        )  # fmt: skip
        try:
            apps = sorted(mount.glob("*.app"))
            if not apps:
                raise ToolchainError(f"{archive.name} holds no .app bundle")
            shutil.copytree(apps[0], dest / apps[0].name, symlinks=True)
        finally:
            subprocess.run(["hdiutil", "detach", str(mount)], check=False, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise ToolchainError(f"hdiutil could not open {archive}: {exc.stderr!r}") from None
    finally:
        shutil.rmtree(mount, ignore_errors=True)


def _extract(asset: Asset, archive: Path, dest: Path) -> None:
    dest = dest.resolve()
    if asset.name.endswith(".zip"):
        _extract_zip(archive, dest)
    elif asset.name.endswith((".tar.gz", ".tgz")):
        _extract_tar(archive, dest)
    elif asset.name.endswith(".dmg"):
        _extract_dmg(archive, dest)
    else:
        raise ToolchainError(f"unknown archive type: {asset.name}")


def _retry(action: Callable[[], object]) -> None:
    """Run a rename or removal again while Windows reports the file busy."""
    for attempt in range(10):
        try:
            action()
            return
        except PermissionError:
            time.sleep(0.5 * (attempt + 1))
    action()


def _rmtree(path: Path) -> None:
    if path.exists():
        _retry(lambda: shutil.rmtree(path))


def _find_executable(root: Path, asset: Asset) -> Path:
    expected = root / asset.executable
    if expected.is_file():
        return expected
    if asset.platform.startswith("osx"):
        for candidate in sorted(root.glob("*.app/Contents/MacOS/*")):
            if candidate.name.lower() == "renode" and candidate.is_file():
                return candidate
    raise ToolchainError(f"{asset.name}: {asset.executable} missing after unpacking")


# -------------------------------------------------------------------- install


def needs_download(asset: Asset) -> bool:
    """Whether installing ``asset`` without ``--from`` has to download it."""
    return not _archive_ok(asset, archive_path(asset))


def install(
    asset: Asset,
    *,
    source: Path | None = None,
    force: bool = False,
    allow_download: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ManagedInstall:
    """Install ``asset`` (from ``source`` when given) and return it.

    An existing verified install is returned unless ``force``. Without
    ``source``, a cached archive that verifies is reused; otherwise the
    archive is downloaded, which needs ``allow_download``."""
    report = progress or (lambda text: log.info("%s", text))
    target = install_dir(asset)
    with install_lock():
        existing = _verified(asset, target)
        if existing is not None and not force:
            return existing
        if source is not None:
            report(f"checking {source}")
            verify_archive(asset, source)
            archive = source
        else:
            archive = archive_path(asset)
            if not _archive_ok(asset, archive):
                if not allow_download:
                    raise ConsentError(
                        f"installing Renode {asset.version} downloads {asset.size / 1e6:.1f} MB "
                        f"from {asset.url}; run `jig renode install --yes` (or set "
                        f"{ENV_ASSUME_YES}=1) to agree, or pass --from with the archive"
                    )
                _check_space(asset)
                _download(asset, archive, report)
        renode_dir().mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{asset.key}.", dir=renode_dir()))
        try:
            report(f"unpacking {asset.name} into {target}")
            _extract(asset, archive, staging)
            entries = list(staging.iterdir())
            root = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
            if asset.name.endswith(".dmg"):
                root = staging  # the .app bundle is the install's content
            executable = _find_executable(root, asset)
            relative = executable.relative_to(root).as_posix()
            if not asset.platform.startswith("win"):
                executable.chmod(
                    executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                )
            _rmtree(target)
            _retry(lambda: root.rename(target))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        marker = {
            "version": asset.version,
            "platform": asset.platform,
            "asset": asset.name,
            "url": asset.url,
            "size": asset.size,
            "sha256": asset.sha256,
            "executable": relative,
            "source": str(source) if source is not None else asset.url,
        }
        (target / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    managed = ManagedInstall(asset, target, target / relative)
    report(f"installed Renode {asset.version} ({asset.platform}) at {target}")
    return managed


def _check_space(asset: Asset) -> None:
    renode_dir().mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(renode_dir()).free
    needed = _SPACE_FACTOR * asset.size
    if free < needed:
        raise ToolchainError(
            f"{renode_dir()} has {free / 1e6:.0f} MB free; installing {asset.name} needs about "
            f"{needed / 1e6:.0f} MB"
        )


def remove(asset: Asset, *, keep_archive: bool = False) -> list[Path]:
    """Remove the managed install of ``asset`` (and its cached archive); return what went."""
    removed: list[Path] = []
    with install_lock():
        target = install_dir(asset)
        if target.exists():
            _rmtree(target)
            removed.append(target)
        archive = archive_path(asset)
        if not keep_archive and archive.exists():
            archive.unlink()
            removed.append(archive)
    return removed
