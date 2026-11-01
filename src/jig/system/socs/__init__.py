"""Device families: how a device is described, generated, built and booted.

A board file names each device's family (``device zynq: zynq7000(...)``).
The family's driver checks the device's arguments, writes the family's part
of the platform description (its platform, clocks, straps, memories), maps
the device's pins to Renode connections, writes the per-run overlay, and runs
the boot flows the family supports.

``driver_for(board)`` finds the driver class, in this order:

1. the device's ``driver: "module:Class"`` setting, where ``module`` is an
   importable module or a ``.py`` file relative to the board file;
2. a family the board's plugin registered (``api.device_family``);
3. the ``jig.device_families`` entry-point group, where Jig registers
   ``zynq7000`` and ``stm32`` and other distributions can add families.

A driver class takes no constructor arguments. What a board's plugin adds for
one machine (the Zynq-7000 image hooks, for instance) reaches the driver
through ``MachineConfig.hooks``, keyed by family.

``SocDriver`` lists the methods every driver has. The platform description
and the board file's checks use these when the driver defines them:

``arguments(values, where)``
    The device's arguments, checked; returns them as the driver keeps them.
``platform_lines(board, device)``
    The family's part of the description: its platform, clocks, straps,
    memories and programmable logic. A driver without it writes none, and a
    board of its family can't run.
``programmable_logic``
    True when ``platform_lines`` places the device's ``ip`` lines; for any
    other family an ``ip`` line is an error.
``pin(device, ref)``
    A device pin (``mio[7]``, ``pa[9]``) as a ``jig.system.generate.Pin``.
``analog(device, name)``
    How one of the device's own converters takes a sensor's voltage:
    ``generate.ANALOG_INPUTS`` or ``generate.CHANNEL_SOURCE``.
``interface_kind(board, device, name)``
    What a name of the device is at the end of a link (``uart``, ``can``,
    ``spi``), for stack checks and link ends; None when the family can't
    say, and a ConfigError for a name the family rejects.
``required_files(config)``
    The files a boot needs that the device's ``firmware`` does not list (a
    file its own arguments name, say) as a mapping from what each is to its
    path. Before Renode starts, ``Simulation.start`` checks that these exist
    along with the board's firmware files, storage images and register-map
    sources.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import import_module, metadata
from pathlib import Path
from typing import Any, Protocol

from jig.errors import ConfigError
from jig.renode.session import RenodeSession
from jig.system import registry
from jig.system.boards import Board, RegisterMapSource

ENTRY_POINTS = "jig.device_families"

# The families Jig ships, for a source tree that was never installed (and so
# has no entry points); an installed jig-dsl registers the same two.
BUILTIN = {
    "zynq7000": "jig.ext.zynq7000.driver:Zynq7000Driver",
    "stm32": "jig.system.socs.stm32:Stm32Driver",
}


@dataclass
class MachineConfig:
    """One board instance in a simulation, as the driver needs it."""

    name: str
    board: Board
    boot: str
    performance_mips: int
    workdir: Path
    storage_images: dict[str, Path] = field(default_factory=dict)  # initial contents per storage
    options: dict[str, Any] = field(default_factory=dict)  # board option -> chosen value
    hooks: Mapping[str, Any] = field(default_factory=dict)  # family -> what the plugin added
    power_cycled: tuple[str, ...] | None = None  # found by ``power_cycled``

    def register_map_sources(self) -> tuple[RegisterMapSource, ...]:
        """The register maps the board's ``airhdl(...)`` properties name."""
        return tuple(self.board.register_maps)


# One IronPython expression (self is Renode's monitor): the names among
# NAMES of the current machine's peripherals whose class implements the
# IPowerCycled contract, joined by commas. It sends no monitor command.
_POWER_CYCLED = (
    "(lambda names, known: ','.join(n for n in names "
    "if (n in known or 'sysbus.' + n in known) "
    "and any(i.Name == 'IPowerCycled' for i in "
    "self.Machine[n if n in known else 'sysbus.' + n].GetType().GetInterfaces())))"
    "({names!r}, list(self.Machine.GetAllNames()))"
)


def power_cycled(config: MachineConfig, session: RenodeSession) -> tuple[str, ...]:
    """The board's peripherals that restart with its supply: those not on
    ``backup_supply`` whose model implements IPowerCycled, asked of the
    machine once."""
    if config.power_cycled is None:
        candidates = list(config.board.power_restart)
        found: tuple[str, ...] = ()
        if candidates:
            session.select_machine(config.name)
            reply = session.python(_POWER_CYCLED.format(names=candidates)).strip()
            found = tuple(name for name in reply.split(",") if name)
        config.power_cycled = found
    return config.power_cycled


class SocDriver(Protocol):
    """A device family's driver (the name it had when boards had one SoC)."""

    name: str
    boot_modes: tuple[str, ...]

    def overlay(self, config: MachineConfig) -> str:
        """Platform-description text loaded after the generated description."""

    def configure(self, config: MachineConfig, session: RenodeSession) -> None:
        """Runs after the platform loads: straps, storage images, ..."""

    def boot(self, config: MachineConfig, session: RenodeSession) -> None:
        """Brings the machine to the point where emulation can start, with its
        cores running (or held as the boot flow leaves them). Runs when the
        board first gets power and again on ``Machine.boot()``, which a plan
        calls after a power cycle, so it runs on a machine that has run."""

    def check(self, board: Board) -> list[str]:
        """Problems with the board this family can detect without Renode."""

    def reset(self, config: MachineConfig, session: RenodeSession) -> None:
        """Pulse the system-reset input (a reset button)."""

    def power_off(self, config: MachineConfig, session: RenodeSession) -> None:
        """Remove the board's supply: the cores stop, volatile state is lost."""

    def power_on(self, config: MachineConfig, session: RenodeSession) -> None:
        """Restore the supply: chips on the rails restart, the boot sequence runs."""

    def images(self, config: MachineConfig) -> dict[str, Path]:
        """Firmware images by role, as an over-the-air update would send them."""

    def boot_table(
        self, config: MachineConfig, slots: dict[str, dict[int, bytes]], first: dict[str, int]
    ) -> bytes:
        """Boot metadata that populates the given slots and boots the given ones first."""


DeviceFamily = SocDriver


def _load_object(reference: str, base: Path | None, what: str) -> Any:
    """``module:attribute`` or ``file.py:attribute`` (relative to ``base``)."""
    module_name, _, attribute = reference.rpartition(":")
    if not module_name or not attribute:
        raise ConfigError(f"{what}: {reference!r} is not 'module:Class'")
    if module_name.endswith(".py"):
        if base is None:
            raise ConfigError(f"{what}: {reference!r} names a file, so it needs a board")
        path = (base / module_name).resolve()
        if not path.is_file():
            raise ConfigError(f"{what}: {path} not found")
        module = registry.load_plugin_module(path, what)
    else:
        try:
            module = import_module(module_name)
        except ImportError as exc:
            raise ConfigError(f"{what}: cannot import {module_name}: {exc}") from exc
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise ConfigError(f"{what}: {module_name} has no {attribute}") from None


def _entry_points() -> dict[str, list[metadata.EntryPoint]]:
    found: dict[str, list[metadata.EntryPoint]] = {}
    for entry in metadata.entry_points(group=ENTRY_POINTS):
        found.setdefault(entry.name, []).append(entry)
    return found


def families() -> list[str]:
    """The device families the entry points (and the built-in table) offer."""
    return sorted(set(BUILTIN) | set(_entry_points()))


def _family_factory(family: str) -> Callable[[], Any] | None:
    entries = _entry_points().get(family, [])
    values = sorted({entry.value for entry in entries})
    if len(values) > 1:
        raise ConfigError(f"device family {family!r} is registered twice: {', '.join(values)}")
    if entries:
        try:
            factory: Callable[[], Any] = entries[0].load()
        except Exception as exc:
            raise ConfigError(f"device family {family!r} ({entries[0].value}): {exc}") from exc
        return factory
    if family in BUILTIN:
        loaded: Callable[[], Any] = _load_object(BUILTIN[family], None, f"device family {family!r}")
        return loaded
    return None


def _instantiate(factory: Callable[[], Any], what: str) -> SocDriver:
    if not callable(factory):
        raise ConfigError(f"{what}: {factory!r} is not a driver class")
    try:
        driver = factory()
    except Exception as exc:
        raise ConfigError(f"{what}: creating the driver failed: {exc}") from exc
    for attribute in ("boot_modes", "overlay", "configure", "boot"):
        if not hasattr(driver, attribute):
            raise ConfigError(f"{what}: {type(driver).__name__} has no {attribute}")
    found: SocDriver = driver
    return found


def known_family(family: str) -> SocDriver | None:
    """A driver of a family Jig knows without any board's plugin (built in,
    or from the entry points), or None."""
    factory = _family_factory(family)
    if factory is None:
        return None
    return _instantiate(factory, f"device family {family!r}")


def driver_for(board: Board | str) -> SocDriver:
    """The driver for a board's device (see the module documentation for
    the order), or for a family name."""
    if isinstance(board, str):
        found = known_family(board)
        if found is None:
            raise ConfigError(f"no device family {board!r} (known: {', '.join(families())})")
        return found
    family = board.soc
    if board.soc_driver is not None:
        what = f"board {board.name}: driver"
        driver = _instantiate(_load_object(board.soc_driver, board.directory, what), what)
    else:
        entry = registry.default().family_for(board)
        if entry is not None:
            driver = _instantiate(entry.value, f"board {board.name}: {entry.owner}'s family")
        else:
            found = known_family(family)
            if found is None:
                raise ConfigError(
                    f"board {board.name}: no device family {family!r} "
                    f"(known: {', '.join(families())})"
                )
            driver = found
    check = getattr(driver, "arguments", None)
    if callable(check) and board.devices:
        check(board.soc_settings, board.device.where)
    return driver
