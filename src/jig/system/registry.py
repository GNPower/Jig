"""What plugins and the bundled extensions registered (internal).

``jig.plugin`` is the public face of this module: a board plugin's
``register(api)`` and an extension's call the ``PluginApi`` methods, which
stage registrations here and commit them when ``register`` returns. The board
loader, the simulation and the interpreter then ask this module for board
settings, plan functions, handle members, housekeeping codecs, node kinds, bus
observers, protocol layers, device families and family hooks.

Board plugins are loaded once per board definition and process. Each plugin
source (a ``.py`` file, or a package directory holding ``__init__.py``) gets
a module name of its own, derived from its path, and a package of its own: a
file plugin's siblings and a package plugin's submodules import relatively
(``from . import codec``), and nothing is added to ``sys.path``, so modules of
two boards' plugins never shadow each other.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import importlib.machinery
import importlib.util
import inspect
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from jig.errors import ConfigError

if TYPE_CHECKING:
    from jig.system.boards import Board

HANDLE_TYPES = ("board", "node", "bus")

# Extensions that ship with Jig, registered before the first board loads.
BUNDLED_EXTENSIONS = ("jig.ext.csp.extension",)

PLUGIN_ENTRY = "register"
API_ATTRIBUTE = "JIG_PLUGIN_API"  # optional: the plugin API version a plugin was written for


@dataclass(frozen=True)
class Owner:
    """Who registered something: a bundled extension or a board's plugin."""

    kind: str  # "extension" or "board"
    name: str  # extension module, or board name
    source: Path | None = None  # the board file, for a board

    def __str__(self) -> str:
        if self.kind == "extension":
            return f"extension {self.name}"
        return f"board {self.name} ({self.source})"


@dataclass(frozen=True)
class Entry:
    owner: Owner
    value: Any


@dataclass(frozen=True)
class Member:
    """A registered handle member: ``function(handle, ...)``, called when a
    plan calls the member, or when it reads it for an ``attribute`` member."""

    function: Callable[..., Any]
    attribute: bool = False


@dataclass
class Staged:
    """Registrations one ``register(api)`` call made, committed together."""

    owner: Owner
    sections: dict[str, Any] = field(default_factory=dict)
    functions: dict[str, Any] = field(default_factory=dict)
    members: dict[tuple[str, str], Any] = field(default_factory=dict)
    codecs: dict[str, Any] = field(default_factory=dict)
    node_kinds: dict[str, Any] = field(default_factory=dict)
    bus_observers: dict[str, Any] = field(default_factory=dict)
    protocols: dict[str, Any] = field(default_factory=dict)
    device_families: dict[str, Any] = field(default_factory=dict)
    hooks: dict[str, Any] = field(default_factory=dict)


def same(first: Any, second: Any) -> bool:
    """One registration made twice: the same object (a plugin file two boards
    share), or the same function of the same module loaded again."""
    if first is second:
        return True
    if isinstance(first, Member) and isinstance(second, Member):
        return first.attribute == second.attribute and same(first.function, second.function)
    if dataclasses.is_dataclass(first) and type(first) is type(second):
        return bool(first == second)
    return bool(
        getattr(first, "__qualname__", None) is not None
        and getattr(first, "__qualname__", None) == getattr(second, "__qualname__", None)
        and getattr(first, "__module__", None) == getattr(second, "__module__", None)
    )


class Registry:
    def __init__(self) -> None:
        self.sections: dict[str, Entry] = {}
        # One entry per owner: a plugin file two boards share registers once
        # for each, so either board alone in a system finds what it added.
        self.functions: dict[str, list[Entry]] = {}
        self.members: dict[tuple[str, str], list[Entry]] = {}
        self.codecs: dict[str, list[Entry]] = {}
        self.node_kinds: dict[str, list[Entry]] = {}
        self.bus_observers: dict[str, list[Entry]] = {}
        self.protocols: dict[str, list[Entry]] = {}
        self.device_families: dict[str, list[Entry]] = {}
        self.hooks: dict[tuple[Path, str], Entry] = {}
        self.boards: dict[Path, Owner] = {}  # board file -> owner, once its plugin ran
        self._modules: dict[Path, ModuleType] = {}
        self._extensions = False
        self._loading_extensions = False

    # --------------------------------------------------------------- commits

    def commit(self, staged: Staged) -> None:
        """Add one ``register`` call's registrations, or none of them when one
        clashes with an earlier registration."""
        owner = staged.owner

        def clash(kind: str, name: str, earlier: Entry) -> ConfigError:
            return ConfigError(
                f"{owner} registers {kind} {name!r}, which {earlier.owner} registered already"
            )

        for name, value in staged.sections.items():
            earlier = self.sections.get(name)
            if earlier is not None and not same(earlier.value, value):
                raise clash("board setting", name, earlier)
        for registry_map, staged_map, kind in (
            (self.codecs, staged.codecs, "housekeeping codec"),
            (self.node_kinds, staged.node_kinds, "node kind"),
            (self.bus_observers, staged.bus_observers, "bus observer"),
            (self.protocols, staged.protocols, "protocol layer"),
        ):
            for name, value in staged_map.items():
                for earlier in registry_map.get(name, []):
                    if not same(earlier.value, value):
                        raise clash(kind, name, earlier)
        for (handle_type, name), value in staged.members.items():
            for earlier in self.members.get((handle_type, name), []):
                global_clash = owner.kind == "extension" or earlier.owner.kind == "extension"
                if (global_clash or earlier.owner.source == owner.source) and not same(
                    earlier.value, value
                ):
                    raise clash(f"{handle_type} member", name, earlier)
        for name, value in staged.functions.items():
            for earlier in self.functions.get(name, []):
                if earlier.owner.kind == "extension" and not same(earlier.value, value):
                    raise clash("function", name, earlier)

        def entry(value: Any) -> Entry:
            return Entry(owner, value)

        def add(table: dict[Any, list[Entry]], key: Any, value: Any) -> None:
            entries = table.setdefault(key, [])
            if not any(e.owner == owner for e in entries):
                entries.append(entry(value))

        for name, value in staged.sections.items():
            self.sections[name] = entry(value)
        for table, staged_table in (
            (self.codecs, staged.codecs),
            (self.node_kinds, staged.node_kinds),
            (self.bus_observers, staged.bus_observers),
            (self.protocols, staged.protocols),
            (self.members, staged.members),
            (self.functions, staged.functions),
            (self.device_families, staged.device_families),
        ):
            for key, value in staged_table.items():
                add(table, key, value)
        if owner.source is not None:
            for family, value in staged.hooks.items():
                self.hooks[(owner.source, family)] = entry(value)

    # ------------------------------------------------------------ extensions

    def ensure_extensions(self) -> None:
        """Register the bundled extensions (once per registry)."""
        if self._extensions or self._loading_extensions:
            return
        from jig.plugin import ExtensionApi  # the public API module imports the system

        self._loading_extensions = True
        try:
            for name in BUNDLED_EXTENSIONS:
                module = importlib.import_module(name)
                api = ExtensionApi(Owner("extension", name.rsplit(".", 1)[0]))
                getattr(module, PLUGIN_ENTRY)(api)
                self.commit(api.staged)
            self._extensions = True
        finally:
            self._loading_extensions = False

    # ---------------------------------------------------------- board plugins

    def load_board(self, board: Board) -> Owner:
        """Run the board's plugin (the board file's ``plugin`` setting) unless
        it ran for this board file already; returns the board's owner record."""
        self.ensure_extensions()
        known = self.boards.get(board.source)
        if known is not None:
            return known
        owner = Owner("board", board.name, board.source)
        if board.plugin is not None:
            from jig.plugin import API_VERSION, PluginApi

            module = self._module(board)
            wanted = getattr(module, API_ATTRIBUTE, API_VERSION)
            if wanted != API_VERSION:
                raise ConfigError(
                    f"board {board.name}: its plugin is written for plugin API {wanted!r}; "
                    f"this Jig provides {API_VERSION}"
                )
            register = getattr(module, PLUGIN_ENTRY, None)
            if not callable(register):
                raise ConfigError(
                    f"board {board.name}: plugin {board.plugin} defines no register(api)"
                )
            api = PluginApi(owner, board)
            try:
                register(api)
            except ConfigError:
                raise
            except Exception as exc:
                raise ConfigError(
                    f"board {board.name}: plugin {board.plugin}: register(api) failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            self.commit(api.staged)
        self.boards[board.source] = owner
        return owner

    def _module(self, board: Board) -> ModuleType:
        assert board.plugin is not None
        source = board.plugin
        module = self._modules.get(source)
        if module is None:
            module = load_plugin_module(source, board.name)
            self._modules[source] = module
        return module

    # --------------------------------------------------------------- lookups

    def board_functions(self, board: Board) -> dict[str, Entry]:
        """The plan functions the board's plugin added."""
        found: dict[str, Entry] = {}
        for name, entries in self.functions.items():
            for entry in entries:
                if entry.owner.source == board.source:
                    found[name] = entry
        return found

    def extension_functions(self) -> dict[str, Entry]:
        return {
            name: entry
            for name, entries in self.functions.items()
            for entry in entries
            if entry.owner.kind == "extension"
        }

    def member_entries(self, handle_type: str, name: str) -> list[Entry]:
        return list(self.members.get((handle_type, name), []))

    def member_names(self, handle_type: str, sources: Iterable[Path | None]) -> list[str]:
        """Names of the members extensions and the given boards' plugins added."""
        wanted = set(sources)
        return sorted(
            name
            for (kind, name), entries in self.members.items()
            if kind == handle_type
            and any(e.owner.kind == "extension" or e.owner.source in wanted for e in entries)
        )

    def codec(self, name: str) -> Entry:
        """The codec registered under ``name``, by any plugin loaded so far."""
        entries = self.codecs.get(name)
        if not entries:
            known = ", ".join(sorted(self.codecs)) or "none"
            raise ConfigError(f"no housekeeping codec {name!r} is registered (registered: {known})")
        return entries[0]

    @staticmethod
    def _for(table: dict[str, list[Entry]], sources: Iterable[Path | None]) -> dict[str, Entry]:
        """Per name, the entry of an extension or of one of the boards."""
        wanted = set(sources)
        found: dict[str, Entry] = {}
        for name, entries in table.items():
            for entry in entries:
                if entry.owner.kind == "extension" or entry.owner.source in wanted:
                    found[name] = entry
                    break
        return found

    def node_kinds_for(self, sources: Iterable[Path | None]) -> dict[str, Entry]:
        return self._for(self.node_kinds, sources)

    def bus_observers_for(self, sources: Iterable[Path | None]) -> dict[str, Entry]:
        return self._for(self.bus_observers, sources)

    def protocols_for(self, sources: Iterable[Path | None] | None) -> dict[str, Entry]:
        """Protocol layers by name: the extensions' and those of the given
        boards' plugins, or of every plugin loaded so far (``None``)."""
        if sources is None:
            return {name: entries[0] for name, entries in self.protocols.items() if entries}
        return self._for(self.protocols, sources)

    def family_for(self, board: Board) -> Entry | None:
        """The device family the board's plugin registered for its device."""
        for entry in self.device_families.get(board.soc, []):
            if entry.owner.source == board.source:
                return entry
        return None

    def hooks_for(self, board: Board) -> dict[str, Any]:
        return {
            family: entry.value
            for (source, family), entry in self.hooks.items()
            if source == board.source
        }


# ------------------------------------------------------------ module loading

_SLUG = re.compile(r"[^A-Za-z0-9]+")


def module_name(source: Path) -> str:
    """The module name a plugin source loads under: unique per path."""
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:10]
    stem = source.name if source.is_dir() else source.stem
    return f"jig_plugin_{_SLUG.sub('_', stem).strip('_').lower()}_{digest}"


def load_plugin_module(source: Path, board: str) -> ModuleType:
    """Import a plugin from a ``.py`` file or a package directory.

    A package loads as a package named ``module_name(source)``. A file loads
    as the module ``<file stem>`` inside a package of that name whose path is
    the file's directory, so ``from . import sibling`` reaches the modules
    beside it."""
    name = module_name(source)
    if source.is_dir():
        init = source / "__init__.py"
        if not init.is_file():
            raise ConfigError(f"board {board}: plugin package {source} has no __init__.py")
        spec = importlib.util.spec_from_file_location(
            name, init, submodule_search_locations=[str(source)]
        )
        return _execute(spec, name, board, source)
    if source.suffix != ".py" or not source.is_file():
        raise ConfigError(f"board {board}: plugin {source} is not a .py file or a package")
    if name not in sys.modules:
        package_spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
        package_spec.submodule_search_locations = [str(source.parent)]
        sys.modules[name] = importlib.util.module_from_spec(package_spec)
    full = f"{name}.{_SLUG.sub('_', source.stem)}"
    spec = importlib.util.spec_from_file_location(full, source)
    return _execute(spec, full, board, source)


def _execute(
    spec: importlib.machinery.ModuleSpec | None, name: str, board: str, source: Path
) -> ModuleType:
    if spec is None or spec.loader is None:
        raise ConfigError(f"board {board}: cannot load plugin {source}")
    existing = sys.modules.get(name)
    if existing is not None and getattr(existing, "__file__", None) == spec.origin:
        return existing
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs, as an import would: relative imports, dataclasses
    # and pickling look the module up by name.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise ConfigError(
            f"board {board}: importing plugin {source} failed: {type(exc).__name__}: {exc}"
        ) from exc
    return module


def callable_arity(function: Callable[..., Any]) -> int | None:
    """How many positional arguments ``function`` takes at most (None: any)."""
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return None
    count = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return None
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            count += 1
    return count


REGISTRY = Registry()


def default() -> Registry:
    """The registry the process uses."""
    return REGISTRY


def extension_sections() -> Mapping[str, Entry]:
    REGISTRY.ensure_extensions()
    return REGISTRY.sections
