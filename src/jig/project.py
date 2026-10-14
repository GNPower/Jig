"""Projects: where a run finds its boards, libraries and settings, and where it
puts its output.

A project is a directory holding ``jig.toml``. Jig takes it from
``$JIG_PROJECT`` (the directory, or the file itself), else from the nearest
``jig.toml`` above the plan being run, else from the nearest one above the
working directory; without any, the working directory is a project with the
default settings. ``jig.toml``::

    [project]
    name = "example"              # default: the directory's name

    [paths]
    boards = ["boards"]           # board roots, searched in this order
    lib = ["lib"]                 # directories plan imports search

    [renode]
    path = "../tools/renode"      # a Renode executable or the directory holding it
    version = ">=1.17.0,<1.18"    # the Renode versions the project accepts (PEP 440)

    [defaults]
    board = "example-board"       # the board of a plan that declares no system
    options = { pl = "rtl" }      # board options, for every board that has them

Every table and key is optional; an unknown one is an error. Paths are
relative to the file that names them. Without ``[paths] boards``, the board
root is ``<project>/boards`` when that directory exists.

A ``jig.local.toml`` beside ``jig.toml`` holds one developer's overrides (local
checkouts as board roots, a Renode build) and stays out of version control.
It merges on top of ``jig.toml``: tables merge key by key, and any other value,
lists included, replaces the one in ``jig.toml``.

Run output goes to ``$JIG_OUTPUT``, else ``<project>/out``. Downloads, the
managed Renode and built artifacts go to the per-user cache, ``$JIG_CACHE_DIR``
or the platform's cache directory. Files that ship inside the package (the
grammar, the plan library, the C# models, the platform descriptions) are read
through ``importlib.resources``, so they resolve the same way from a source
checkout and from an installed wheel.
"""

from __future__ import annotations

import contextlib
import os
import re
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import platformdirs

from jig.errors import ConfigError

ENV_PROJECT = "JIG_PROJECT"
ENV_OUTPUT = "JIG_OUTPUT"
ENV_CACHE = "JIG_CACHE_DIR"
MARKER = "jig.toml"
LOCAL = "jig.local.toml"

# Table -> key -> (kind, description) of every setting jig.toml may hold.
_SCHEMA: dict[str, dict[str, str]] = {
    "project": {"name": "text"},
    "paths": {"boards": "paths", "lib": "paths"},
    "renode": {"path": "path", "version": "specifier"},
    "defaults": {"board": "text", "options": "options"},
}

# One PEP 440 clause: an operator and a version (wildcards allowed with == and !=).
_CLAUSE = re.compile(r"\s*(~=|===|==|!=|<=|>=|<|>)\s*[0-9][0-9A-Za-z.+!*-]*\s*")


@dataclass(frozen=True)
class Project:
    """A project's settings, with every path absolute."""

    root: Path
    file: Path | None = None  # jig.toml; None for a directory without one
    name: str = ""
    boards: tuple[Path, ...] = ()  # [paths] boards
    boards_declared: bool = False  # whether [paths] boards was given
    lib: tuple[Path, ...] = ()  # [paths] lib
    renode_path: Path | None = None  # [renode] path
    renode_version: str | None = None  # [renode] version
    default_board: str | None = None  # [defaults] board
    default_options: Mapping[str, str] = field(default_factory=dict)  # [defaults] options
    local: Path | None = None  # the jig.local.toml merged in, if any

    def board_roots(self) -> list[Path]:
        """``[paths] boards``, or ``<root>/boards`` when the table names none and
        that directory exists."""
        if self.boards_declared:
            return list(self.boards)
        default = self.root / "boards"
        return [default] if default.is_dir() else []


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).resolve() if value else None


# ------------------------------------------------------------------ jig.toml


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def _option_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _check(data: dict[str, Any], path: Path) -> None:
    """Every table and key known, every value of its kind."""
    for table, entries in data.items():
        if table not in _SCHEMA:
            raise ConfigError(f"{path}: unknown table [{table}] (known: {', '.join(_SCHEMA)})")
        if not isinstance(entries, dict):
            raise ConfigError(f"{path}: {table} must be a table ([{table}])")
        known = _SCHEMA[table]
        for key, value in entries.items():
            where = f"{path}: {table}.{key}"
            kind = known.get(key)
            if kind is None:
                raise ConfigError(f"{where} is not a setting (known: {', '.join(known)})")
            if kind in ("text", "path") and (not isinstance(value, str) or not value):
                raise ConfigError(f"{where} must be a non-empty string")
            if kind == "paths" and (
                not isinstance(value, list) or not all(isinstance(v, str) and v for v in value)
            ):
                raise ConfigError(f'{where} must be a list of paths, e.g. ["boards"]')
            if kind == "specifier" and (
                not isinstance(value, str)
                or not all(_CLAUSE.fullmatch(c) for c in value.split(","))
            ):
                raise ConfigError(
                    f'{where} must be a version specifier such as ">=1.17.0,<1.18", not {value!r}'
                )
            if kind == "options":
                if not isinstance(value, dict):
                    raise ConfigError(f"{where} must be a table of option values")
                for name, item in value.items():
                    if _option_value(item) is None:
                        raise ConfigError(f"{where}.{name} must be a string, number or boolean")


def _merge(base: dict[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load(file: Path) -> Project:
    """The project ``file`` (a jig.toml) describes, with its jig.local.toml merged in."""
    file = file.resolve()
    root = file.parent
    data = _read_toml(file)
    _check(data, file)
    local = root / LOCAL
    local_file: Path | None = None
    if local.is_file():
        overrides = _read_toml(local)
        _check(overrides, local)
        data = _merge(data, overrides)
        local_file = local

    def paths(values: Sequence[str], where: str) -> tuple[Path, ...]:
        resolved = []
        for value in values:
            path = (root / value).resolve()
            if not path.is_dir():
                raise ConfigError(f"{file}: {where}: {path} is not a directory")
            resolved.append(path)
        return tuple(resolved)

    table = data.get("project", {})
    path_table = data.get("paths", {})
    renode = data.get("renode", {})
    defaults = data.get("defaults", {})
    options = {str(k): str(_option_value(v)) for k, v in defaults.get("options", {}).items()}
    return Project(
        root=root,
        file=file,
        name=str(table.get("name", root.name)),
        boards=paths(path_table.get("boards", []), "paths.boards"),
        boards_declared="boards" in path_table,
        lib=paths(path_table.get("lib", []), "paths.lib"),
        renode_path=(root / renode["path"]).resolve() if "path" in renode else None,
        renode_version=renode.get("version"),
        default_board=defaults.get("board"),
        default_options=options,
        local=local_file,
    )


# ----------------------------------------------------------------- discovery


def _nearest(origin: Path) -> Path | None:
    start = origin if origin.is_dir() else origin.parent
    for directory in (start, *start.parents):
        candidate = directory / MARKER
        if candidate.is_file():
            return candidate
    return None


def discover(start: Path | None = None) -> Project:
    """The project for a run: ``$JIG_PROJECT``, else the nearest jig.toml above
    ``start`` (a plan file or a directory), else above the working directory,
    else the working directory with the default settings."""
    explicit = os.environ.get(ENV_PROJECT)
    if explicit:
        path = Path(explicit).resolve()
        if path.is_file():
            return load(path)
        if not path.is_dir():
            raise ConfigError(f"{ENV_PROJECT}={path} is not a directory")
        marker = path / MARKER
        return load(marker) if marker.is_file() else Project(root=path, name=path.name)
    cwd = Path.cwd().resolve()
    for origin in (start.resolve() if start is not None else None, cwd):
        if origin is None:
            continue
        found = _nearest(origin)
        if found is not None:
            return load(found)
    return Project(root=cwd, name=cwd.name)


_active: ContextVar[Project | None] = ContextVar("jig_project", default=None)


def current() -> Project:
    """The project a run activated (``activate``), else the one ``discover`` finds."""
    project = _active.get()
    return project if project is not None else discover()


@contextlib.contextmanager
def activate(project: Project) -> Iterator[Project]:
    """Make ``project`` the current one for the duration of the block."""
    token = _active.set(project)
    try:
        yield project
    finally:
        _active.reset(token)


def root() -> Path:
    """The current project's root directory."""
    return current().root


# --------------------------------------------------------------- directories


def output_dir() -> Path:
    """Run directories and reports."""
    return _env_path(ENV_OUTPUT) or root() / "out"


def cache_dir() -> Path:
    """Per-user cache: downloads, the managed Renode and built artifacts."""
    return _env_path(ENV_CACHE) or Path(platformdirs.user_cache_dir("jig", appauthor=False))


def downloads_dir() -> Path:
    return cache_dir() / "downloads"


def tools_dir() -> Path:
    """Managed tool installs (Renode)."""
    return cache_dir() / "tools"


def artifacts_dir() -> Path:
    """The content-addressed artifact cache (``jig.artifacts``)."""
    return cache_dir() / "artifacts"


def package_file(*parts: str) -> Path:
    """A file or directory shipped inside the ``jig`` package, as a filesystem path."""
    resource = resources.files("jig").joinpath(*parts)
    if not isinstance(resource, Path):
        raise ConfigError(
            f"jig/{'/'.join(parts)} is not on the filesystem; install jig-dsl unzipped"
        )
    return resource


def platform_file(name: str) -> Path:
    """A platform description Jig ships, by name: ``xilinx/zynq7000``."""
    from jig.renode import platforms

    return platforms.path(name)


# ------------------------------------------------------- host-independent text


def path_anchors(project: Project | None = None) -> dict[str, str]:
    """Absolute directories a run's text may name, with the label that stands
    for each in host-independent text: the output directory and the cache as
    ``<out>`` and ``<cache>``, the project root as the empty prefix."""
    chosen = project or current()
    anchors = {str(chosen.root): ""}
    output = _env_path(ENV_OUTPUT) or chosen.root / "out"
    anchors[str(output)] = "<out>"
    anchors[str(cache_dir())] = "<cache>"
    return anchors


def relativize(text: str, anchors: Mapping[str, str]) -> str:
    """``text`` with every absolute path under one of ``anchors`` rewritten as
    its label plus the rest of the path in forward slashes, so the same run on
    another host (or in another checkout) reads the same. The longest anchor
    wins; a path under the empty label becomes relative."""
    for anchor in sorted(anchors, key=len, reverse=True):
        label = anchors[anchor]
        parts = [re.escape(part) for part in re.split(r"[\\/]+", anchor) if part]
        if not parts:
            continue
        # Separators may be either slash, doubled where a repr escaped them.
        prefix = r"[\\/]+" if anchor[:1] in "\\/" else ""
        pattern = re.compile(
            prefix + r"[\\/]+".join(parts) + r"(?P<rest>(?:[\\/][^\s'\"]*)?)(?=$|[\s'\"])",
            re.IGNORECASE if os.name == "nt" else 0,
        )

        def replace(match: re.Match[str], label: str = label) -> str:
            rest = re.sub(r"[\\/]+", "/", match.group("rest"))
            if label:
                return label + rest
            return rest.lstrip("/") or "."

        text = pattern.sub(replace, text)
    return text
