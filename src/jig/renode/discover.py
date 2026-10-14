"""Finding a Renode to run.

Sources, in order:

1. ``--renode PATH`` (``explicit``);
2. ``$JIG_RENODE``;
3. ``jig.toml``: ``[renode] path`` (``RenodeConfig.path``);
4. the user config file, ``<user config dir>/config.toml``: ``[renode] path``
   (``$JIG_CONFIG_DIR`` overrides the directory);
5. Jig-managed installs (``jig renode install``), newest first;
6. every ``renode`` on ``PATH``, in ``PATH`` order;
7. the standard install locations of Renode's own packages;
8. ``$RENODE_ROOT``.

Sources 1 to 4 are explicit: the first one that is set is used, and it fails
loudly when it names no Renode or a Renode outside the requirement. The
others are implicit: the first install whose ``renode --version`` satisfies
the requirement wins, and the rest are skipped with the reason recorded.

The requirement is the project's ``[renode] version`` (a PEP 440 specifier),
and in any case the supported range, ``>=1.17.0,<1.18``; Jig is tested with
the 1.17.0 release builds. ``allow_untested`` (``--allow-untested-renode``)
lifts the supported range, never the project's own requirement.

``renode --version`` takes about 0.2 s, so each result is cached in
``<cache>/renode/probes.json``, keyed by the executable's real path, size and
modification time (and those of ``Renode.dll`` when it can be found).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from jig import project
from jig.errors import ConfigError, ToolchainError

log = logging.getLogger(__name__)

ENV_RENODE = "JIG_RENODE"
ENV_CONFIG_DIR = "JIG_CONFIG_DIR"
ENV_RENODE_ROOT = "RENODE_ROOT"
CONFIG_FILE = "config.toml"

SUPPORTED = SpecifierSet(">=1.17.0,<1.18")
"""Renode versions this Jig release works with."""

TESTED_BUILDS = frozenset({"1.17.0+20260906gitf1dd1b4af", "1.17.0+20260907gitf1dd1b4af"})
"""The builds Jig's tests ran on: the 1.17.0 release's Windows and Linux portables."""

TESTED_VERSION = "1.17.0"

_PROBE_CACHE = "probes.json"
_VERSION_TIMEOUT = 60.0


@dataclass(frozen=True)
class RenodeConfig:
    """A project's Renode settings (``jig.toml`` ``[renode]``)."""

    path: Path | None = None
    """A Renode executable or the directory holding it."""
    version: str | None = None
    """A PEP 440 specifier the Renode version must satisfy, e.g. ``==1.17.0``."""

    @classmethod
    def from_table(cls, table: Mapping[str, object], base: Path) -> RenodeConfig:
        """Read a ``[renode]`` table; a relative ``path`` is relative to ``base``."""
        unknown = sorted(set(table) - {"path", "version"})
        if unknown:
            raise ConfigError(f"[renode]: unknown keys {', '.join(unknown)} (known: path, version)")
        path = table.get("path")
        version = table.get("version")
        if path is not None and not isinstance(path, str):
            raise ConfigError("[renode] path must be a string")
        if version is not None and not isinstance(version, str):
            raise ConfigError("[renode] version must be a string such as '==1.17.0'")
        if version is not None:
            requirement(version)  # validate now, not at first use
        resolved = None
        if path:
            candidate = Path(path).expanduser()
            resolved = candidate if candidate.is_absolute() else base / candidate
        return cls(path=resolved, version=version or None)


@dataclass(frozen=True)
class RenodeInstall:
    """One Renode installation and what ``renode --version`` said about it."""

    executable: Path
    root: Path
    """Renode's root directory, the one holding ``.renode-root``, ``platforms``
    and ``plugins`` (the executable's own directory when there is none)."""
    version: str
    build: str
    runtime: str
    build_type: str = ""
    source: str = ""
    """The discovery source that found it."""
    assemblies: Path | None = None
    """The directory holding Renode.dll and Infrastructure.dll, when found
    (the root of a portable package; ``<root>/bin`` of a deb or rpm install)."""

    @property
    def status(self) -> str:
        """``tested``, ``supported`` (in range, another build) or ``unsupported``."""
        return classify(self.version, self.build)

    @property
    def has_assemblies(self) -> bool:
        """Whether Renode.dll and Infrastructure.dll were found (SDK builds need them)."""
        directory = self.assemblies
        return directory is not None and all(
            (directory / name).is_file() for name in ("Renode.dll", "Infrastructure.dll")
        )

    def describe(self) -> str:
        return f"Renode {self.version} ({self.build}, {self.runtime}) at {self.executable}"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "executable": str(self.executable),
            "root": str(self.root),
            "assemblies": None if self.assemblies is None else str(self.assemblies),
            "version": self.version,
            "build": self.build,
            "build_type": self.build_type,
            "runtime": self.runtime,
            "status": self.status,
            "source": self.source,
        }


@dataclass
class Candidate:
    """What one discovery source offered."""

    source: str
    explicit: bool
    path: Path | None = None
    install: RenodeInstall | None = None
    problem: str | None = None
    """Why the source gave no usable Renode (``None`` when it did)."""
    selected: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "explicit": self.explicit,
            "path": None if self.path is None else str(self.path),
            "install": None if self.install is None else self.install.as_dict(),
            "problem": self.problem,
            "selected": self.selected,
        }


@dataclass
class Survey:
    """Every discovery source in order, and the winner."""

    candidates: list[Candidate] = field(default_factory=list)
    requirement: SpecifierSet = SUPPORTED
    explicit: Candidate | None = None
    """The explicit source in use, if any is set."""

    @property
    def selected(self) -> RenodeInstall | None:
        for candidate in self.candidates:
            if candidate.selected:
                return candidate.install
        return None


# ------------------------------------------------------------------ versions


def requirement(specifier: str | None) -> SpecifierSet:
    """A project requirement as a specifier set."""
    if not specifier:
        return SUPPORTED
    try:
        return SpecifierSet(specifier)
    except InvalidSpecifier:
        raise ConfigError(
            f"[renode] version {specifier!r} is not a PEP 440 specifier (e.g. '==1.17.0')"
        ) from None


def classify(version: str, build: str) -> str:
    """``tested``, ``supported`` or ``unsupported`` (see the module documentation)."""
    try:
        parsed = Version(version)
    except InvalidVersion:
        return "unsupported"
    if parsed not in SUPPORTED:
        return "unsupported"
    return "tested" if build in TESTED_BUILDS else "supported"


def build_version(install: RenodeInstall) -> Version:
    """The install's version for requirements: its build string when that is a
    PEP 440 version of the same release (``1.17.0+20260906gitf1dd1b4af``, so a
    project can pin a build), else the plain version."""
    version = Version(install.version)
    try:
        build = Version(install.build)
    except InvalidVersion:
        return version
    return build if build.public == version.public else version


def _problem(
    install: RenodeInstall, wanted: SpecifierSet | None, allow_untested: bool
) -> str | None:
    try:
        version = build_version(install)
    except InvalidVersion:
        return f"unreadable version {install.version!r}"
    if wanted is not None and not wanted.contains(version, prereleases=True):
        return f"Renode {version} does not satisfy the project's requirement {wanted}"
    if version not in SUPPORTED and not allow_untested:
        return (
            f"Renode {version} is outside the supported range {SUPPORTED} (tested: "
            f"{TESTED_VERSION}); pass --allow-untested-renode to use it anyway"
        )
    return None


# -------------------------------------------------------------------- probing

_VERSION_LINE = re.compile(r"^\s*Renode v(?P<version>\d+\.\d+\.\d+)\s*$", re.MULTILINE)
_FIELD = re.compile(r"^\s*(?P<key>build|build type|runtime):\s*(?P<value>.+?)\s*$", re.MULTILINE)


def parse_version_output(text: str) -> dict[str, str]:
    """The fields of ``renode --version`` (``Renode vX.Y.Z`` and three
    ``key: value`` lines); raises ``ToolchainError`` without a version line."""
    match = _VERSION_LINE.search(text)
    if match is None:
        raise ToolchainError(f"not Renode's --version output: {text.strip()[:200]!r}")
    fields = {m.group("key"): m.group("value") for m in _FIELD.finditer(text)}
    return {
        "version": match.group("version"),
        "build": fields.get("build", match.group("version")),
        "build_type": fields.get("build type", ""),
        "runtime": fields.get("runtime", ""),
    }


def executable_names() -> tuple[str, ...]:
    """File names a Renode launcher has on this OS."""
    if os.name == "nt":
        return ("renode.exe", "renode.bat", "renode.cmd")
    return ("renode",)


def _as_executable(path: Path) -> Path | None:
    """``path`` itself when it is a file, else a Renode launcher inside it."""
    if path.is_file():
        return path
    if path.is_dir():
        for name in executable_names():
            if (path / name).is_file():
                return path / name
        app = path / "Contents" / "MacOS" / "renode"  # a macOS Renode.app
        if app.is_file():
            return app
    return None


_DLL_IN_SCRIPT = re.compile(r"""(?P<path>[^\s"'=:;]*?)[\\/]Renode\.dll""")


def assemblies_dir(executable: Path) -> Path | None:
    """The directory holding Renode.dll for ``executable``: beside it, in its
    ``bin``, or named in it when it is a launcher script (deb, rpm, Homebrew)."""
    real = executable.resolve()
    for directory in (real.parent, real.parent / "bin"):
        if (directory / "Renode.dll").is_file():
            return directory
    try:
        if real.stat().st_size > 64 * 1024:
            return None
        text = real.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for match in _DLL_IN_SCRIPT.finditer(text):
        candidate = Path(match.group("path"))
        if candidate.is_absolute() and (candidate / "Renode.dll").is_file():
            return candidate
    return None


def _stamp(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def _cache_path() -> Path:
    return project.cache_dir() / "renode" / _PROBE_CACHE


def _load_cache() -> dict[str, dict[str, object]]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache: Mapping[str, object]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:  # a read-only cache only costs the next probe
        log.debug("could not write %s: %s", path, exc)


def probe(executable: Path, *, source: str = "") -> RenodeInstall:
    """Run ``executable --version`` (or reuse a cached result) and describe the install."""
    real = executable.resolve()
    if not real.is_file():
        raise ToolchainError(f"{executable}: no such file")
    dll_dir = assemblies_dir(executable)
    key = str(real)
    stamp = _stamp(real) + (_stamp(dll_dir / "Renode.dll") if dll_dir is not None else [])
    cache = _load_cache()
    cached = cache.get(key)
    if isinstance(cached, dict) and cached.get("stamp") == stamp:
        fields = cached.get("fields")
        if isinstance(fields, dict) and all(isinstance(v, str) for v in fields.values()):
            return _install(executable, real, dll_dir, fields, source)
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERSION_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolchainError(f"{executable} --version failed: {exc}") from None
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        raise ToolchainError(
            f"{executable} --version exited with {result.returncode}: {output.strip()[:300]}"
        )
    fields = parse_version_output(output)
    cache[key] = {"stamp": stamp, "fields": fields}
    live = {k: v for k, v in cache.items() if Path(k).is_file()}
    _save_cache(live)
    return _install(executable, real, dll_dir, fields, source)


ROOT_MARKER = ".renode-root"


def renode_root(start: Path) -> Path | None:
    """The directory at or above ``start`` holding Renode's ``.renode-root``."""
    for directory in (start, *start.parents):
        if (directory / ROOT_MARKER).is_file():
            return directory
    return None


def _install(
    executable: Path, real: Path, dll_dir: Path | None, fields: Mapping[str, str], source: str
) -> RenodeInstall:
    start = dll_dir or real.parent
    return RenodeInstall(
        executable=executable,
        root=renode_root(start) or start,
        assemblies=dll_dir,
        version=fields["version"],
        build=fields.get("build", fields["version"]),
        runtime=fields.get("runtime", ""),
        build_type=fields.get("build_type", ""),
        source=source,
    )


# -------------------------------------------------------------------- sources


def user_config_file() -> Path:
    """``config.toml`` in the user's Jig config directory."""
    override = os.environ.get(ENV_CONFIG_DIR)
    directory = (
        Path(override) if override else Path(platformdirs.user_config_dir("jig", appauthor=False))
    )
    return directory / CONFIG_FILE


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from None


def _renode_table(path: Path) -> RenodeConfig | None:
    data = _read_toml(path)
    table = data.get("renode")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [renode] must be a table")
    return RenodeConfig.from_table(table, path.parent)


def user_config() -> RenodeConfig | None:
    """The ``[renode]`` table of the user config file, if any."""
    return _renode_table(user_config_file())


def load_project_config(root: Path | None = None) -> RenodeConfig | None:
    """The project's ``[renode]`` settings, if any.

    Without ``root``: from Jig's project model when it has one
    (``jig.project.current()``, which also merges ``jig.local.toml``), else
    from the current project's ``jig.toml``. With ``root``: from
    ``<root>/jig.toml``."""
    current = getattr(project, "current", None)
    if root is None and callable(current):
        settings = current()
        path: Path | None = getattr(settings, "renode_path", None)
        version: str | None = getattr(settings, "renode_version", None)
        if path is None and version is None:
            return None
        return RenodeConfig(path=path, version=version)
    directory = root if root is not None else project.root()
    return _renode_table(directory / project.MARKER)


def standard_locations() -> list[Path]:
    """Where Renode's own installers put it."""
    system = platform.system()
    if system == "Windows":
        locations = []
        for variable in ("ProgramFiles", "ProgramW6432"):
            base = os.environ.get(variable)
            if base:
                locations.append(Path(base) / "Renode" / "renode.exe")
        return list(dict.fromkeys(locations))
    if system == "Darwin":
        return [
            Path("/Applications/Renode.app/Contents/MacOS/renode"),
            Path.home() / "Applications" / "Renode.app" / "Contents" / "MacOS" / "renode",
        ]
    return [Path("/opt/renode/renode")]


def _on_path() -> Iterator[Path]:
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        directory = Path(entry)
        for name in executable_names():
            candidate = directory / name
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                yield candidate
                break


def _managed() -> Iterator[Path]:
    # Imported here: install imports this module.
    from jig.renode import install

    for managed in install.managed_installs():
        yield managed.executable


def _explicit_sources(
    explicit: Path | None, config: RenodeConfig | None
) -> Iterator[tuple[str, Path | None]]:
    yield "--renode", explicit
    value = os.environ.get(ENV_RENODE)
    yield ENV_RENODE, Path(value) if value else None
    yield "jig.toml", config.path if config is not None else None
    user = user_config()
    yield f"user config ({user_config_file()})", user.path if user is not None else None


def _implicit_sources() -> Iterator[tuple[str, Path]]:
    for path in _managed():
        yield "managed", path
    for path in _on_path():
        yield "PATH", path
    for path in standard_locations():
        if path.is_file():
            yield "standard location", path
    root = os.environ.get(ENV_RENODE_ROOT)
    if root:
        executable = _as_executable(Path(root))
        if executable is not None:
            yield ENV_RENODE_ROOT, executable


def survey(
    *,
    explicit: Path | None,
    config: RenodeConfig | None,
    allow_untested: bool = False,
    stop_at_first: bool = False,
) -> Survey:
    """Every discovery source, what it offered and which one wins.

    With ``stop_at_first`` the survey stops at the winner (``find_renode``);
    without it, every implicit source is probed (``jig doctor``)."""
    wanted = requirement(config.version if config is not None else None)
    project_requirement = wanted if config is not None and config.version else None
    result = Survey(requirement=wanted)
    for source, path in _explicit_sources(explicit, config):
        candidate = Candidate(source=source, explicit=True, path=path)
        result.candidates.append(candidate)
        if path is None:
            candidate.problem = "not set"
        elif result.explicit is not None:
            candidate.problem = f"not used: {result.explicit.source} is set"
        else:
            result.explicit = candidate
            _try(candidate, path, project_requirement, allow_untested)
    if result.explicit is not None:
        result.explicit.selected = result.explicit.problem is None
        return result
    seen: set[str] = set()
    winner: Candidate | None = None
    for source, path in _implicit_sources():
        real = str(path.resolve())
        if real in seen:
            continue
        seen.add(real)
        candidate = Candidate(source=source, explicit=False, path=path)
        result.candidates.append(candidate)
        _try(candidate, path, project_requirement, allow_untested)
        if candidate.problem is not None:
            continue
        if winner is None:
            winner = candidate
            candidate.selected = True
            if stop_at_first:
                break
        else:
            candidate.problem = f"not used: {winner.source} {winner.path} comes first"
    return result


def _try(
    candidate: Candidate, path: Path, project_requirement: SpecifierSet | None, allow_untested: bool
) -> None:
    executable = _as_executable(path)
    if executable is None:
        candidate.problem = f"no Renode executable there ({path})"
        return
    try:
        candidate.install = probe(executable, source=candidate.source)
    except ToolchainError as exc:
        candidate.problem = str(exc)
        return
    candidate.problem = _problem(candidate.install, project_requirement, allow_untested)


def find_renode(
    *,
    explicit: Path | None,
    config: RenodeConfig | None,
    allow_untested: bool = False,
) -> RenodeInstall:
    """The Renode to run (see the module documentation for the order)."""
    found = survey(
        explicit=explicit, config=config, allow_untested=allow_untested, stop_at_first=True
    )
    install = found.selected
    if install is not None:
        if install.status == "supported":
            log.warning(
                "%s: Jig is tested with the %s release builds, not %s",
                install.executable,
                TESTED_VERSION,
                install.build,
            )
        elif install.status == "unsupported":
            log.warning("%s is outside the supported range %s", install.describe(), SUPPORTED)
        return install
    if found.explicit is not None:
        raise ToolchainError(f"{found.explicit.source}: {found.explicit.problem}")
    tried = [f"  {c.source}: {c.path} ({c.problem})" for c in found.candidates if not c.explicit]
    detail = ("\nfound but not usable:\n" + "\n".join(tried)) if tried else ""
    raise ToolchainError(
        f"no usable Renode found (requirement {found.requirement}).{detail}\n"
        "Install one with `jig renode install` (Renode 1.17.0 from its GitHub release), "
        f"or point {ENV_RENODE} or --renode at an existing install."
    )
