"""Board definitions: ``<name>/board.yaml`` under a board root.

A board definition is data: the Renode platform description, the SoC family
whose driver knows how to boot it, the firmware artifacts, the non-volatile
storage, the interfaces a test talks to, and optionally the C# models and the
plugin the board brings along. Adding a board means adding a directory under
a board root; the runtime does not change.

Board roots, searched in order: the project's ``[paths] boards`` (jig.toml),
the directories in ``$JIG_BOARD_PATH`` (separated by ``os.pathsep``), then the
directories the ``jig.boards`` entry-point group names (an entry point loads a
directory, a list of directories, or a function returning either). A board's
name is its directory's name; one name under two roots is an error that names
both.

board.yaml, schema 2::

    schema: 2
    name: example-board              # the directory's name
    description: what the board is
    root: ../..                      # see below; default: the nearest .git above
    soc: zynq7000                    # the SoC family (its driver boots the board)
    soc_driver: drivers.py:MyDriver  # optional: the driver class ("module:Class")
    soc_platform: xilinx/zynq7000    # optional: a platform Jig ships, loaded first
    platform: example-board.repl     # the board's platform description
    performance_mips: 100
    zynq7000: {...}                  # settings for the SoC driver, named after `soc`
    firmware: {app: build/app.elf}   # firmware images by role
    boot: {default: jtag}
    storage: {flash0: {peripheral: qspi.flash0, size: 0x1000000}}
    interfaces:
      console: {kind: uart, peripheral: uart1, baud: 115200, eol: cr}
      can: {kind: can, peripheral: can0, bitrate: 1000000}
    sensors: {supply: {adc: adc0, channel: 2}}
    rtc: rtc0
    power: {initial: on, restart: [flash0]}
    clocks: {uart1.clockFrequency: uart_ref}
    register_maps: {block.registerMap: {package: hdl/regs_pkg.sv}}
    options: {...}                   # per-machine choices a plan makes
    models: ["../../models/*.cs"]    # C# models the board needs
    plugin: plugin.py                # a .py file or a package directory
    csp: {...}                       # sections the bundled extensions define

Paths:

* ``root`` is relative to this file (or absolute). Without it, the root is
  the nearest directory at or above the board's that holds a ``.git`` file or
  directory (a submodule checkout has a ``.git`` file), else the board's own
  directory.
* Relative to the root: ``firmware``, ``register_maps``, an option's
  ``cosim`` files and the files SoC settings name (``zynq7000.xsa``).
* Relative to this file: ``platform`` and an option's ``platform``,
  ``models`` (globs of ``.cs`` files) and ``plugin``.

Changes from schema 1, which Jig no longer reads: ``root``, ``soc_driver``,
``soc_platform``, ``models``, ``plugin`` (instead of ``dsl``), ``power.initial``
and a UART interface's ``eol`` are new; repository paths resolve against the
board's root instead of the project root; ``csp`` (address, housekeeping and
RDP options) is the CSP extension's section and its ``housekeeping`` is a
table ``{port, request, codec}`` instead of a layout file; unknown keys are
errors.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml

from jig import project
from jig.errors import ConfigError
from jig.system import registry
from jig.system.renode_shim import ModelSet

SCHEMA = 2
INTERFACE_KINDS = ("uart", "can")
POWER_STATES = ("on", "off")
ENV_BOARD_PATH = "JIG_BOARD_PATH"
ENTRY_POINTS = "jig.boards"
DEFAULT_EOL = "\r"
_EOL = {"cr": "\r", "lf": "\n", "crlf": "\r\n", "\\r": "\r", "\\n": "\n", "\\r\\n": "\r\n"}
_GLOB = frozenset("*?[")

_CORE_KEYS = (
    "schema",
    "name",
    "description",
    "root",
    "soc",
    "soc_driver",
    "soc_platform",
    "platform",
    "performance_mips",
    "register_maps",
    "options",
    "firmware",
    "boot",
    "storage",
    "interfaces",
    "sensors",
    "rtc",
    "power",
    "clocks",
    "models",
    "plugin",
)
_REPLACED = {"dsl": "plugin (a .py file or a package directory, relative to this file)"}
_INTERFACE_KEYS = {
    "uart": ("kind", "peripheral", "baud", "eol"),
    "can": ("kind", "peripheral", "bitrate"),
}


@dataclass(frozen=True)
class Interface:
    name: str
    kind: str
    peripheral: str
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def eol(self) -> str:
        """What ends a line sent to this UART (``eol``; default CR)."""
        return str(self.settings.get("eol", DEFAULT_EOL))


@dataclass(frozen=True)
class Storage:
    name: str
    peripheral: str
    size: int


@dataclass(frozen=True)
class Sensor:
    """An analog input a test can drive: one ADC channel, in volts at the pin."""

    name: str
    adc: str  # Renode peripheral of the converter
    channel: int


@dataclass(frozen=True)
class RegisterMapSource:
    target: str  # "<peripheral>.<constructor parameter>"
    package: Path
    module: Path | None


@dataclass(frozen=True)
class CosimSpec:
    """An RTL model Renode loads as a library in place of a peripheral model.

    ``build`` is a script run as ``build <output-dir> --clock-mhz <N>`` that
    leaves ``library`` in the output directory; the clock comes from the
    peripheral's ``frequency`` in the option's platform description. The build
    is cached by the contents of ``build``, the files ``sources`` (a Verilator
    file list) names, and ``inputs`` (files, or directories taken whole).
    ``context`` is the peripheral's SimulationContext, with ``{workdir}`` the
    machine's work directory. ``root`` is the board's root: the build runs
    there and the file list's entries are relative to it."""

    peripheral: str
    build: Path
    sources: Path
    inputs: tuple[Path, ...]
    library: str
    context: str = ""
    root: Path | None = None


@dataclass(frozen=True)
class OptionChoice:
    """One value of a board option: what it adds to the machine."""

    name: str
    description: str
    platform: Path | None  # a platform-description fragment loaded after the board's
    register_maps: tuple[RegisterMapSource, ...]
    commands: tuple[str, ...]  # monitor commands after loading, {workdir} expanded
    cosim: CosimSpec | None


@dataclass(frozen=True)
class BoardOption:
    """A choice a plan makes per machine, e.g. which model stands in for a block."""

    name: str
    default: str
    choices: dict[str, OptionChoice]

    def choice(self, value: str) -> OptionChoice:
        if value not in self.choices:
            raise ConfigError(
                f"option {self.name}: {value!r} is not one of {', '.join(self.choices)}"
            )
        return self.choices[value]


@dataclass(frozen=True)
class Board:
    name: str
    description: str
    soc: str
    directory: Path  # the directory holding board.yaml
    platform: Path
    performance_mips: int
    soc_settings: dict[str, Any]
    register_maps: tuple[RegisterMapSource, ...]
    firmware: dict[str, Path]
    boot: dict[str, Any]
    storage: tuple[Storage, ...]
    interfaces: tuple[Interface, ...]
    clocks: dict[str, str]
    source: Path = Path()  # board.yaml itself
    root: Path = Path()
    soc_platform: str | None = None
    soc_driver: str | None = None
    sensors: tuple[Sensor, ...] = ()
    power_restart: tuple[str, ...] = ()  # peripherals that restart with the board's supply
    power_initial: str = "on"
    models: tuple[Path, ...] = ()  # .cs files
    plugin: Path | None = None  # a .py file or a package directory
    options: dict[str, BoardOption] = field(default_factory=dict)
    rtc: str | None = None  # the real-time clock peripheral (SetDateTime/GetDateTime)
    sections: dict[str, Any] = field(default_factory=dict)  # extension sections, parsed

    @property
    def default_boot(self) -> str:
        return str(self.boot.get("default", "jtag"))

    def section(self, name: str) -> Any:
        """An extension's parsed section (``csp``), or None when the file has none."""
        return self.sections.get(name)

    def resolve_options(self, chosen: dict[str, str] | None) -> dict[str, str]:
        """Every option's value: the chosen one where given, else its default."""
        chosen = dict(chosen or {})
        unknown = set(chosen) - set(self.options)
        if unknown:
            known = ", ".join(self.options) or "none"
            raise ConfigError(
                f"board {self.name} has no option {', '.join(sorted(unknown))} (has: {known})"
            )
        resolved = {}
        for name, option in self.options.items():
            value = str(chosen.get(name, option.default))
            option.choice(value)
            resolved[name] = value
        return resolved

    def interface(self, name: str) -> Interface:
        for interface in self.interfaces:
            if interface.name == name:
                return interface
        known = ", ".join(i.name for i in self.interfaces)
        raise ConfigError(f"board {self.name} has no interface {name!r} (has: {known})")

    def firmware_file(self, role: str) -> Path:
        if role not in self.firmware:
            raise ConfigError(f"board {self.name} names no {role!r} firmware")
        return self.firmware[role]

    def sensor(self, name: str) -> Sensor:
        for sensor in self.sensors:
            if sensor.name == name:
                return sensor
        known = ", ".join(s.name for s in self.sensors) or "none"
        raise ConfigError(f"board {self.name} has no sensor {name!r} (has: {known})")


@dataclass(frozen=True)
class SectionContext:
    """What an extension's section parser knows about the file it reads."""

    board: str
    where: str  # "<board.yaml>: <section>", for error messages
    directory: Path
    root: Path


SectionParser = Callable[[Any, SectionContext], Any]


# ------------------------------------------------------------------ parsing


def _require(data: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ConfigError(f"{where}: missing '{key}'")
    return data[key]


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping")
    return value


def _known(data: Mapping[str, Any], keys: Iterable[str], where: str) -> None:
    allowed = tuple(keys)
    unknown = [str(k) for k in data if k not in allowed]
    if unknown:
        raise ConfigError(
            f"{where}: unknown key {', '.join(sorted(unknown))} (known: {', '.join(allowed)})"
        )


def _path(base: Path, value: Any, where: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: expected a path, got {value!r}")
    return (base / value).resolve()


def default_root(directory: Path) -> Path:
    """The nearest directory at or above ``directory`` holding ``.git`` (a
    directory, or the file a submodule or worktree checkout has), else
    ``directory`` itself."""
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return directory


def _register_maps(data: Any, root: Path, where: str) -> tuple[RegisterMapSource, ...]:
    if data is None:
        return ()
    maps = []
    for target, spec in _mapping(data, where).items():
        spec_where = f"{where}.{target}"
        spec = _mapping(spec, spec_where)
        _known(spec, ("package", "module"), spec_where)
        module = spec.get("module")
        maps.append(
            RegisterMapSource(
                target=str(target),
                package=_path(root, spec.get("package"), f"{spec_where}.package"),
                module=_path(root, module, f"{spec_where}.module") if module else None,
            )
        )
    return tuple(maps)


def _cosim(data: Any, root: Path, where: str) -> CosimSpec | None:
    if data is None:
        return None
    data = _mapping(data, where)
    _known(data, ("peripheral", "build", "sources", "inputs", "library", "context"), where)
    build = _path(root, _require(data, "build", where), f"{where}.build")
    sources = _path(root, _require(data, "sources", where), f"{where}.sources")
    for path in (build, sources):
        if not path.is_file():
            raise ConfigError(f"{where}: {path} not found")
    inputs = data.get("inputs") or []
    if not isinstance(inputs, list):
        raise ConfigError(f"{where}.inputs: expected a list of paths")
    return CosimSpec(
        peripheral=str(_require(data, "peripheral", where)),
        build=build,
        sources=sources,
        inputs=tuple(_path(root, p, f"{where}.inputs") for p in inputs),
        library=str(_require(data, "library", where)),
        context=str(data.get("context", "")),
        root=root,
    )


def _options(data: Any, board_dir: Path, root: Path, where: str) -> dict[str, BoardOption]:
    if data is None:
        return {}
    options = {}
    for name, spec in _mapping(data, where).items():
        option_where = f"{where}.{name}"
        spec = _mapping(spec, option_where)
        default = str(_require(spec, "default", option_where))
        choices = {}
        for value, choice in spec.items():
            if value == "default":
                continue
            choice_where = f"{option_where}.{value}"
            choice = _mapping(choice, choice_where)
            _known(
                choice,
                ("description", "platform", "register_maps", "commands", "cosim"),
                choice_where,
            )
            platform = None
            if choice.get("platform"):
                platform = _path(board_dir, choice["platform"], f"{choice_where}.platform")
                if not platform.is_file():
                    raise ConfigError(f"{choice_where}: platform description {platform} not found")
            commands = choice.get("commands") or []
            if not isinstance(commands, list):
                raise ConfigError(f"{choice_where}.commands: expected a list of monitor commands")
            choices[str(value)] = OptionChoice(
                name=str(value),
                description=str(choice.get("description", "")),
                platform=platform,
                register_maps=_register_maps(
                    choice.get("register_maps"), root, f"{choice_where}.register_maps"
                ),
                commands=tuple(str(c) for c in commands),
                cosim=_cosim(choice.get("cosim"), root, f"{choice_where}.cosim"),
            )
        if not choices:
            raise ConfigError(f"{option_where}: no choices")
        if default not in choices:
            raise ConfigError(f"{option_where}: default {default!r} is not one of its choices")
        options[str(name)] = BoardOption(str(name), default, choices)
    return options


def _eol(value: Any, where: str) -> str:
    if isinstance(value, str):
        if value in ("\r", "\n", "\r\n"):
            return value
        named = _EOL.get(value.lower())
        if named is not None:
            return named
    raise ConfigError(f"{where}: eol must be cr, lf or crlf, not {value!r}")


def _interfaces(data: Any, where: str) -> tuple[Interface, ...]:
    interfaces = []
    for interface_name, spec in _mapping(data or {}, where).items():
        spec_where = f"{where}.{interface_name}"
        spec = _mapping(spec, spec_where)
        kind = str(_require(spec, "kind", spec_where))
        if kind not in INTERFACE_KINDS:
            raise ConfigError(
                f"{spec_where}: kind {kind!r} is not one of {', '.join(INTERFACE_KINDS)}"
            )
        _known(spec, _INTERFACE_KEYS[kind], spec_where)
        settings = {k: v for k, v in spec.items() if k not in ("kind", "peripheral")}
        for key in ("baud", "bitrate"):
            if key in settings and (
                not isinstance(settings[key], int)
                or isinstance(settings[key], bool)
                or settings[key] <= 0
            ):
                raise ConfigError(f"{spec_where}.{key} must be a positive integer")
        if "eol" in settings:
            settings["eol"] = _eol(settings["eol"], f"{spec_where}.eol")
        interfaces.append(
            Interface(
                str(interface_name), kind, str(_require(spec, "peripheral", spec_where)), settings
            )
        )
    return tuple(interfaces)


def _split_glob(pattern: str) -> tuple[str, str]:
    """(the leading directories without wildcards, the rest)."""
    parts = Path(pattern).parts
    for index, part in enumerate(parts):
        if _GLOB & set(part):
            return str(Path(*parts[:index])) if index else ".", str(Path(*parts[index:]))
    return pattern, ""


def _models(data: Any, board_dir: Path, where: str) -> tuple[Path, ...]:
    if data is None:
        return ()
    if not isinstance(data, list) or not all(isinstance(p, str) and p for p in data):
        raise ConfigError(f"{where}: expected a list of glob patterns such as ['models/*.cs']")
    files: set[Path] = set()
    for pattern in data:
        base, rest = _split_glob(pattern)
        start = (board_dir / base).resolve()
        matches = [start] if not rest else sorted(start.glob(rest))
        matches = [path for path in matches if path.is_file()]
        if not matches:
            raise ConfigError(f"{where}: {pattern!r} matches no file")
        for path in matches:
            if path.suffix != ".cs":
                raise ConfigError(f"{where}: {pattern!r} matches {path.name}, not a .cs file")
            files.add(path.resolve())
    return tuple(sorted(files))


def _power(data: Any, where: str) -> tuple[tuple[str, ...], str]:
    power = _mapping(data or {}, where)
    _known(power, ("restart", "initial"), where)
    restart = power.get("restart") or []
    if not isinstance(restart, list):
        raise ConfigError(f"{where}.restart must be a list of peripherals")
    initial = power.get("initial", "on")
    if isinstance(initial, bool):  # YAML 1.1 reads a bare on/off as a boolean
        initial = "on" if initial else "off"
    if initial not in POWER_STATES:
        raise ConfigError(f"{where}.initial must be on or off, not {initial!r}")
    return tuple(str(p) for p in restart), str(initial)


def load(path: Path) -> Board:
    """Parse and validate one board.yaml (schema 2)."""
    path = path.resolve()
    where = str(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping at the top level")
    if data.get("schema") != SCHEMA:
        raise ConfigError(
            f"{where}: schema {data.get('schema')!r} is not supported (expected {SCHEMA}; "
            "schema 2 replaced schema 1, see the board.yaml reference)"
        )
    directory = path.parent
    name = str(_require(data, "name", where))
    soc = str(_require(data, "soc", where))

    sections = registry.extension_sections()
    for key, replacement in _REPLACED.items():
        if key in data:
            raise ConfigError(f"{where}: '{key}' is gone in schema 2; use {replacement}")
    _known(data, (*_CORE_KEYS, soc, *sections), where)

    root = directory
    if "root" in data:
        root = _path(directory, data["root"], f"{where}: root")
        if not root.is_dir():
            raise ConfigError(f"{where}: root {root} is not a directory")
    else:
        root = default_root(directory)

    platform = _path(directory, _require(data, "platform", where), f"{where}: platform")
    if not platform.is_file():
        raise ConfigError(f"{where}: platform description {platform} not found")
    soc_platform = data.get("soc_platform")
    if soc_platform is not None:
        from jig.renode import platforms

        if not isinstance(soc_platform, str):
            raise ConfigError(f"{where}: soc_platform must be a name such as xilinx/zynq7000")
        try:
            platforms.path(soc_platform)
        except ConfigError as exc:
            raise ConfigError(f"{where}: soc_platform: {exc}") from None
    soc_driver = data.get("soc_driver")
    if soc_driver is not None and (not isinstance(soc_driver, str) or ":" not in soc_driver):
        raise ConfigError(f"{where}: soc_driver must be 'module:Class' or 'file.py:Class'")

    mips = data.get("performance_mips", 100)
    if not isinstance(mips, int) or isinstance(mips, bool) or mips <= 0:
        raise ConfigError(f"{where}: performance_mips must be a positive integer")

    firmware = {
        str(role): _path(root, value, f"{where}: firmware.{role}")
        for role, value in _mapping(data.get("firmware") or {}, f"{where}: firmware").items()
    }

    storage = []
    for storage_name, spec in _mapping(data.get("storage") or {}, f"{where}: storage").items():
        spec_where = f"{where}: storage.{storage_name}"
        spec = _mapping(spec, spec_where)
        _known(spec, ("peripheral", "size"), spec_where)
        size = _require(spec, "size", spec_where)
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ConfigError(f"{spec_where}: size must be a positive integer")
        storage.append(
            Storage(str(storage_name), str(_require(spec, "peripheral", spec_where)), size)
        )

    boot = _mapping(data.get("boot") or {}, f"{where}: boot")
    soc_settings = _mapping(data.get(soc) or {}, f"{where}: {soc}")

    sensors = []
    for sensor_name, spec in _mapping(data.get("sensors") or {}, f"{where}: sensors").items():
        spec_where = f"{where}: sensors.{sensor_name}"
        spec = _mapping(spec, spec_where)
        _known(spec, ("adc", "channel"), spec_where)
        channel = _require(spec, "channel", spec_where)
        if not isinstance(channel, int) or isinstance(channel, bool) or channel < 0:
            raise ConfigError(f"{spec_where}: channel must be a non-negative integer")
        sensors.append(Sensor(str(sensor_name), str(_require(spec, "adc", spec_where)), channel))

    plugin = None
    if data.get("plugin") is not None:
        plugin = _path(directory, data["plugin"], f"{where}: plugin")
        if not plugin.exists():
            raise ConfigError(f"{where}: plugin {plugin} not found")

    restart, initial = _power(data.get("power"), f"{where}: power")
    clocks = _mapping(data.get("clocks") or {}, f"{where}: clocks")

    parsed_sections: dict[str, Any] = {}
    for section_name, entry in sections.items():
        if section_name in data:
            context = SectionContext(name, f"{where}: {section_name}", directory, root)
            parser: SectionParser = entry.value
            parsed_sections[section_name] = parser(data[section_name], context)

    return Board(
        name=name,
        description=str(data.get("description", "")),
        soc=soc,
        directory=directory,
        platform=platform,
        performance_mips=mips,
        soc_settings=dict(soc_settings),
        register_maps=_register_maps(data.get("register_maps"), root, f"{where}: register_maps"),
        firmware=firmware,
        boot=dict(boot),
        storage=tuple(storage),
        interfaces=_interfaces(data.get("interfaces"), f"{where}: interfaces"),
        clocks={str(k): str(v) for k, v in clocks.items()},
        source=path,
        root=root,
        soc_platform=soc_platform,
        soc_driver=soc_driver,
        sensors=tuple(sensors),
        power_restart=restart,
        power_initial=initial,
        models=_models(data.get("models"), directory, f"{where}: models"),
        plugin=plugin,
        options=_options(data.get("options"), directory, root, f"{where}: options"),
        rtc=str(data["rtc"]) if data.get("rtc") else None,
        sections=parsed_sections,
    )


# --------------------------------------------------------------- search path


@dataclass(frozen=True)
class BoardRoot:
    """A directory holding board directories, and where Jig learned of it."""

    path: Path
    origin: str


@dataclass(frozen=True)
class Found:
    """A board definition on the search path."""

    name: str
    path: Path  # its board.yaml
    origin: str


def _entry_point_roots() -> Iterator[BoardRoot]:
    for entry in metadata.entry_points(group=ENTRY_POINTS):
        origin = f"entry point {entry.name} ({entry.value})"
        try:
            value = entry.load()
            if callable(value):
                value = value()
        except Exception as exc:
            raise ConfigError(f"{origin}: {type(exc).__name__}: {exc}") from exc
        values = [value] if isinstance(value, (str, os.PathLike)) else value
        try:
            paths = [Path(item) for item in values]
        except TypeError:
            raise ConfigError(
                f"{origin}: expected a directory or a list of them, got {value!r}"
            ) from None
        for path in paths:
            yield BoardRoot(path.resolve(), origin)


def search_path(chosen: project.Project | None = None) -> list[BoardRoot]:
    """The board roots, in search order."""
    current = chosen or project.current()
    origin = "jig.toml [paths] boards" if current.boards_declared else "<project>/boards"
    roots = [BoardRoot(path, origin) for path in current.board_roots()]
    for item in os.environ.get(ENV_BOARD_PATH, "").split(os.pathsep):
        if not item.strip():
            continue
        path = Path(item).resolve()
        if not path.is_dir():
            raise ConfigError(f"{ENV_BOARD_PATH}: {path} is not a directory")
        roots.append(BoardRoot(path, ENV_BOARD_PATH))
    roots.extend(_entry_point_roots())
    return roots


def catalog(chosen: project.Project | None = None) -> dict[str, Found]:
    """Every board on the search path by name; a name defined twice is an error."""
    found: dict[str, Found] = {}
    for board_root in search_path(chosen):
        if not board_root.path.is_dir():
            raise ConfigError(f"{board_root.origin}: {board_root.path} is not a directory")
        for definition in sorted(board_root.path.glob("*/board.yaml")):
            definition = definition.resolve()
            name = definition.parent.name
            earlier = found.get(name)
            if earlier is None:
                found[name] = Found(name, definition, board_root.origin)
            elif earlier.path != definition:
                raise ConfigError(
                    f"board {name!r} is defined twice: {earlier.path} ({earlier.origin}) "
                    f"and {definition} ({board_root.origin})"
                )
    return dict(sorted(found.items()))


def available(chosen: project.Project | None = None) -> list[str]:
    """Names of the boards on the search path."""
    return list(catalog(chosen))


def find(name: str, chosen: project.Project | None = None) -> Board:
    """The board called ``name`` on the search path."""
    boards = catalog(chosen)
    entry = boards.get(name)
    if entry is None:
        raise ConfigError(f"no board named {name!r} (available: {', '.join(boards) or 'none'})")
    board = load(entry.path)
    if board.name != name:
        raise ConfigError(f"{entry.path}: name {board.name!r} differs from its directory {name!r}")
    return board


def library_dir(name: str) -> Path:
    """A board's plan library: ``<board directory>/lib``."""
    return find(name).directory / "lib"


# ---------------------------------------------------------------- model sets


def model_sets(boards: Iterable[Board]) -> list[ModelSet]:
    """The boards' C# models as one set per repository (board root), in the
    order the boards come, each file once."""
    by_root: dict[Path, list[Path]] = {}
    seen: set[Path] = set()
    for board in boards:
        for file in board.models:
            if file not in seen:
                seen.add(file)
                by_root.setdefault(board.root, []).append(file)
    sets = []
    names: set[str] = set()
    for root, files in by_root.items():
        name = root.name or "models"
        suffix = 2
        while name in names:
            name = f"{root.name}-{suffix}"
            suffix += 1
        names.add(name)
        sets.append(ModelSet(name=name, root=root, files=tuple(sorted(files))))
    return sets
