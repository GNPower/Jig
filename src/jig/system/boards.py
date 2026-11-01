"""Boards: ``<name>/board.jig`` under a board root.

A board file describes one PCB in the Jig language: the devices that run
code on it (each one Renode machine), the peripherals wired to them, the
links between them and the links that leave the board, the analog inputs a
plan drives, and the options a system picks from. ``jig.system.boardfile``
reads it; this module finds board files and holds what a board file
evaluates to (``Board``). Adding a board means adding a directory under a
board root; the runtime does not change.

Board roots, searched in order: the project's ``[paths] boards`` (jig.toml),
the directories in ``$JIG_BOARD_PATH`` (separated by ``os.pathsep``), then the
directories the ``jig.boards`` entry-point group names (an entry point loads a
directory, a list of directories, or a function returning either). A board's
name is its directory's name, and its board file's ``board "<name>"`` must
match it; one name under two roots is an error that names both.

Paths in a board file:

* ``root`` is relative to the board file (or absolute). Without it, the root
  is the nearest directory at or above the board's that holds a ``.git`` file
  or directory (a submodule checkout has a ``.git`` file), else the board's
  own directory.
* Relative to the root: firmware, the XSA, airhdl sources and co-simulation
  inputs.
* Relative to the board file: ``models`` (globs of ``.cs`` files),
  ``plugin``, ``repl`` fragments and a ``build(...)`` firmware's script and
  sources.

``Board`` keeps the fields board.yaml boards had for the board's one device
(``firmware``, ``boot``, ``storage``, ``interfaces``, ``sensors``, ``rtc``,
``soc``, ``soc_settings`` and the rest), so drivers and plugins written for
them keep working, and adds the board model: ``devices``, ``peripherals``,
``links``, ``sensor_specs``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jig import project
from jig.errors import ConfigError
from jig.system.model import Device, Link, OptionSpec, Peripheral, SensorSpec
from jig.system.renode_shim import ModelSet

if TYPE_CHECKING:
    from jig.system.boardfile import BoardFile

BOARD_FILE = "board.jig"
POWER_STATES = ("on", "off")
ENV_BOARD_PATH = "JIG_BOARD_PATH"
ENTRY_POINTS = "jig.boards"
DEFAULT_EOL = "\r"
DEFAULT_MIPS = 100


@dataclass(frozen=True)
class Interface:
    """An external link of the board, as board.yaml called it: its name, kind
    (``uart`` or ``can``), the device interface it joins (``peripheral``, the
    first when a CAN link joins several), its settings (``baud``, ``frame``,
    ``bitrate``, and ``eol`` and ``prompt`` from the ``cli`` layer of the
    interface's stack) and the layers of that stack."""

    name: str
    kind: str
    peripheral: str
    settings: dict[str, Any] = field(default_factory=dict)
    peripherals: tuple[str, ...] = ()
    device: str = ""
    stack: tuple[str, ...] = ()

    @property
    def eol(self) -> str:
        """What ends a line sent to this UART (the ``cli`` layer's ``eol``; default CR)."""
        return str(self.settings.get("eol", DEFAULT_EOL))

    @property
    def prompt(self) -> str | None:
        """The regex the ``cli`` layer's prompt makes (text matches as
        written); None when the layer names no prompt."""
        from jig.lang.values import as_pattern  # jig.lang imports this module

        value = self.settings.get("prompt")
        return None if value is None else as_pattern(value)


@dataclass(frozen=True)
class Storage:
    """A non-volatile memory that keeps its image file across resets: its name
    (the peripheral's), its name in the machine's monitor, and its size (the
    peripheral's ``size`` property; None when it gives none)."""

    name: str
    peripheral: str
    size: int | None


@dataclass(frozen=True)
class Sensor:
    """An analog input a test can drive: one converter channel, in volts at
    the pin. ``adc`` is the converter's name in the machine's monitor;
    ``source`` names the ``Analog.ADCChannelSource`` Jig placed on the
    channel when the converter takes one, instead of implementing
    IAnalogInputs."""

    name: str
    adc: str
    channel: int
    initial: float | None = None
    source: str | None = None


@dataclass(frozen=True)
class RegisterMapSource:
    target: str  # "<peripheral>.<constructor parameter>"
    package: Path
    module: Path | None


@dataclass(frozen=True)
class CosimSpec:
    """An RTL model Renode loads as a library in place of a peripheral model.

    ``build`` is a script run as ``build <output-dir> --clock-mhz <N>`` that
    leaves ``library`` in the output directory; the clock is the
    peripheral's ``frequency``. The build is cached by the contents of
    ``build``, the files ``sources`` (a Verilator file list) names, and
    ``inputs`` (files, or directories taken whole). ``context`` is the
    peripheral's SimulationContext. ``root`` is the board's root: the build
    runs there and the file list's entries are relative to it."""

    peripheral: str
    build: Path
    sources: Path
    inputs: tuple[Path, ...]
    library: str
    context: str = ""
    root: Path | None = None
    frequency: int | None = None


@dataclass(frozen=True)
class Board:
    """A board file evaluated for one set of option values.

    The fields up to ``sections`` are board.yaml's, for the board's one
    device; the rest is the board model."""

    name: str
    description: str
    soc: str  # the device's family
    directory: Path  # the directory holding board.jig
    performance_mips: int
    soc_settings: dict[str, Any]  # the device's arguments
    register_maps: tuple[RegisterMapSource, ...]
    firmware: dict[str, Path]
    boot: dict[str, Any]  # {"default": mode, mode: settings}
    storage: tuple[Storage, ...]
    interfaces: tuple[Interface, ...]  # the external UART and CAN links
    source: Path = Path()  # board.jig itself
    root: Path = Path()
    soc_driver: str | None = None  # the device's driver setting
    sensors: tuple[Sensor, ...] = ()
    power_restart: tuple[str, ...] = ()  # peripherals off the backup supply
    power_initial: str = "on"
    models: tuple[Path, ...] = ()  # .cs files
    plugin: Path | None = None  # a .py file or a package directory
    options: dict[str, OptionSpec] = field(default_factory=dict)
    rtc: str | None = None  # the real-time clock (SetDateTime/GetDateTime)
    sections: dict[str, Any] = field(default_factory=dict)  # extension settings, parsed
    # The board model.
    devices: tuple[Device, ...] = ()
    peripherals: tuple[Peripheral, ...] = ()
    links: tuple[Link, ...] = ()
    sensor_specs: tuple[SensorSpec, ...] = ()
    backup_supply: tuple[str, ...] = ()
    repl: tuple[Path, ...] = ()  # hand-written platform fragments
    option_values: dict[str, Any] = field(default_factory=dict)
    workdir: str = ""
    cosim: tuple[CosimSpec, ...] = ()
    file: BoardFile | None = field(default=None, compare=False, repr=False)

    @property
    def device(self) -> Device:
        """The board's one device."""
        return self.devices[0]

    @property
    def default_boot(self) -> str:
        return str(self.boot.get("default", "jtag"))

    @property
    def boot_modes(self) -> tuple[str, ...]:
        """The boot modes a system may pick: the device's boot lines, or the
        family's modes when it has none (empty: the family decides)."""
        return tuple(mode.name for mode in self.device.boot) if self.devices else ()

    def section(self, name: str) -> Any:
        """An extension's parsed setting, or the settings of the device's
        protocol instance of that layer (``section("csp")``); None when the
        board has neither."""
        if name in self.sections:
            return self.sections[name]
        found = [
            layer for device in self.devices for layer in _layers(device) if layer.name == name
        ]
        if not found:
            return None
        if len(found) > 1:
            raise ConfigError(f"board {self.name} runs {len(found)} {name} instances")
        return found[0].settings

    def resolve_options(self, chosen: Mapping[str, Any] | None) -> dict[str, Any]:
        """Every option's value: the chosen one where given, else its default."""
        chosen = dict(chosen or {})
        unknown = set(chosen) - set(self.options)
        if unknown:
            known = ", ".join(self.options) or "none"
            raise ConfigError(
                f"board {self.name} has no option {', '.join(sorted(unknown))} (has: {known})"
            )
        where = f"board {self.name}"
        return {
            name: option.accept(chosen[name], where) if name in chosen else option.default
            for name, option in self.options.items()
        }

    def interface(self, name: str) -> Interface:
        for interface in self.interfaces:
            if interface.name == name:
                return interface
        known = ", ".join(i.name for i in self.interfaces)
        raise ConfigError(f"board {self.name} has no external link {name!r} (has: {known})")

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

    def link(self, name: str) -> Link:
        for link in self.links:
            if link.name == name:
                return link
        known = ", ".join(link.name for link in self.links) or "none"
        raise ConfigError(f"board {self.name} has no link {name!r} (has: {known})")

    def peripheral(self, name: str) -> Peripheral:
        for peripheral in self.peripherals:
            if peripheral.name == name:
                return peripheral
        known = ", ".join(p.name for p in self.peripherals) or "none"
        raise ConfigError(f"board {self.name} has no peripheral {name!r} (has: {known})")

    def cli_links(self) -> list[str]:
        """The external UART links whose device interface runs ``cli``."""
        return [i.name for i in self.interfaces if i.kind == "uart" and "cli" in i.stack]

    def evaluate(
        self, options: Mapping[str, Any] | None = None, workdir: str | None = None
    ) -> Board:
        """The same board file for other option values or another machine
        directory."""
        if self.file is None:
            raise ConfigError(f"board {self.name} was not read from a board file")
        return self.file.evaluate(options, workdir)


def _layers(device: Device) -> Iterator[Any]:
    for instance in device.protocols:
        yield instance.layer
    for stack in device.interfaces:
        for layer in stack.layers:
            if layer.instance is None:
                yield layer


@dataclass(frozen=True)
class SectionContext:
    """What an extension's setting parser knows about the file it reads."""

    board: str
    where: str  # "<board file>: <setting>", for error messages
    directory: Path
    root: Path


SectionParser = Callable[[Any, SectionContext], Any]


def default_root(directory: Path) -> Path:
    """The nearest directory at or above ``directory`` holding ``.git`` (a
    directory, or the file a submodule or worktree checkout has), else
    ``directory`` itself."""
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return directory


# ------------------------------------------------------------------ loading


def definition(path: Path) -> BoardFile:
    """The board file at ``path``, parsed but not evaluated."""
    from jig.system.boardfile import BoardFile

    return BoardFile(path)


def load(path: Path, options: Mapping[str, Any] | None = None, workdir: str | None = None) -> Board:
    """Read and evaluate one board file (``board.jig``) with the given option
    values (each option's default otherwise) and machine directory (a
    placeholder otherwise)."""
    return definition(path).evaluate(options, workdir)


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
    path: Path  # its board.jig
    origin: str

    @property
    def directory(self) -> Path:
        return self.path.parent


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
        for path in sorted(board_root.path.glob(f"*/{BOARD_FILE}")):
            path = path.resolve()
            name = path.parent.name
            earlier = found.get(name)
            if earlier is None:
                found[name] = Found(name, path, board_root.origin)
            elif earlier.path != path:
                raise ConfigError(
                    f"board {name!r} is defined twice: {earlier.path} ({earlier.origin}) "
                    f"and {path} ({board_root.origin})"
                )
    return dict(sorted(found.items()))


def available(chosen: project.Project | None = None) -> list[str]:
    """Names of the boards on the search path."""
    return list(catalog(chosen))


def locate(name: str, chosen: project.Project | None = None) -> Found:
    """Where the board called ``name`` is on the search path."""
    boards = catalog(chosen)
    entry = boards.get(name)
    if entry is None:
        raise ConfigError(f"no board named {name!r} (available: {', '.join(boards) or 'none'})")
    return entry


def board_file(name: str, chosen: project.Project | None = None) -> BoardFile:
    """The board file of the board called ``name``, parsed, not evaluated."""
    entry = locate(name, chosen)
    found = definition(entry.path)
    if found.name != name:
        raise ConfigError(f"{entry.path}: board {found.name!r} differs from its directory {name!r}")
    return found


def find(
    name: str,
    chosen: project.Project | None = None,
    *,
    options: Mapping[str, Any] | None = None,
    workdir: str | None = None,
) -> Board:
    """The board called ``name`` on the search path, evaluated with the
    project's default options (jig.toml ``[defaults] options``), then
    ``options``."""
    found = board_file(name, chosen)
    current = chosen or project.current()
    values: dict[str, Any] = {
        option: value
        for option, value in current.default_options.items()
        if option in found.options
    }
    values.update(options or {})
    return found.evaluate(values, workdir)


def library_dir(name: str) -> Path:
    """A board's plan library: ``<board directory>/lib``."""
    return locate(name).directory / "lib"


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
