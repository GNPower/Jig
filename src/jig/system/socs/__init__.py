"""SoC drivers: how a board family is built into a Renode machine and booted.

A board definition names its SoC family (``soc: zynq7000``). The driver for
that family generates the artifacts the platform needs (register programs,
register maps, flash images), writes the per-run platform overlay, and runs the
boot flows the family supports.

``driver_for(board)`` finds the driver class, in this order:

1. ``soc_driver: module:Class`` in the board's board.yaml, where ``module`` is
   an importable module or a ``.py`` file relative to the board.yaml;
2. a driver the board's plugin registered for its family (``api.soc_driver``);
3. the ``jig.socs`` entry-point group, where Jig registers ``zynq7000`` and
   ``stm32`` and other distributions can add families.

A driver class takes no constructor arguments. What a board's plugin adds for
one machine (the Zynq-7000 image hooks, for instance) reaches the driver
through ``MachineConfig.hooks``, keyed by family.

A driver may also define ``required_files(config)``, returning the files a
boot needs that board.yaml's ``firmware`` does not list (a file its own
settings name, say) as a mapping from what each is to its path. Before Renode
starts, ``Simulation.start`` checks that these exist along with the board's
firmware files, storage images and register-map sources.
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
from jig.system.boards import Board, OptionChoice, RegisterMapSource

ENTRY_POINTS = "jig.socs"

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
    options: dict[str, str] = field(default_factory=dict)  # board option -> chosen value
    hooks: Mapping[str, Any] = field(default_factory=dict)  # family -> what the plugin added

    def choices(self) -> list[OptionChoice]:
        """The chosen value of every board option, in the board's order."""
        return [self.board.options[name].choice(value) for name, value in self.options.items()]

    def register_map_sources(self) -> tuple[RegisterMapSource, ...]:
        """The board's register maps plus those its chosen options add."""
        sources = list(self.board.register_maps)
        for choice in self.choices():
            sources.extend(choice.register_maps)
        return tuple(sources)


class SocDriver(Protocol):
    name: str
    boot_modes: tuple[str, ...]

    def overlay(self, config: MachineConfig) -> str:
        """Platform-description text loaded after the board's own file."""

    def configure(self, config: MachineConfig, session: RenodeSession) -> None:
        """Runs after the platform loads: straps, storage images, ..."""

    def boot(self, config: MachineConfig, session: RenodeSession) -> None:
        """Brings the machine to the point where emulation can start, with its
        cores running (or held as the boot flow leaves them). Runs when the
        board first gets power and again on ``Machine.boot()``, which a plan
        calls after a power cycle, so it runs on a machine that has run."""

    def check(self, board: Board) -> list[str]:
        """Problems with the board definition this SoC can detect without Renode."""

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
    """The SoC families the entry points (and the built-in table) offer."""
    return sorted(set(BUILTIN) | set(_entry_points()))


def _family_factory(soc: str) -> Callable[[], Any]:
    entries = _entry_points().get(soc, [])
    values = sorted({entry.value for entry in entries})
    if len(values) > 1:
        raise ConfigError(f"SoC family {soc!r} is registered twice: {', '.join(values)}")
    if entries:
        try:
            factory: Callable[[], Any] = entries[0].load()
        except Exception as exc:
            raise ConfigError(f"SoC family {soc!r} ({entries[0].value}): {exc}") from exc
        return factory
    if soc in BUILTIN:
        loaded: Callable[[], Any] = _load_object(BUILTIN[soc], None, f"SoC family {soc!r}")
        return loaded
    raise ConfigError(f"no SoC driver for {soc!r} (supported: {', '.join(families())})")


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


def driver_for(board: Board | str) -> SocDriver:
    """The driver for a board (see the module documentation for the order),
    or for a family name."""
    if isinstance(board, str):
        return _instantiate(_family_factory(board), f"SoC family {board!r}")
    what = f"board {board.name}: soc_driver"
    if board.soc_driver is not None:
        return _instantiate(_load_object(board.soc_driver, board.directory, what), what)
    entry = registry.default().soc_driver_for(board)
    if entry is not None:
        return _instantiate(entry.value, f"board {board.name}: {entry.owner}'s driver")
    return _instantiate(_family_factory(board.soc), f"SoC family {board.soc!r}")
