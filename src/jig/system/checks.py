"""What ``jig check`` finds wrong with a board without Renode.

* device, peripheral and link names a plan could not reach because a board
  member has the name (``reset``, a plugin's member);
* the device's family and its arguments (``jig.system.socs.driver_for``),
  and what the family's driver checks (firmware files, the XSA, register
  maps);
* every protocol stack: each layer known, each running on what the layer or
  interface below it gives ("csp needs packets and uart0 gives a byte
  stream: put kiss between them");
* what the protocol layers and extension settings say about themselves
  (a housekeeping codec no loaded plugin registered);
* the platform description Jig writes for the device;
* the board's partitions (``storage_problems``): each one's target and
  format (formats the board's plugin registers count), the format's
  arguments, alignment to a ``block_size`` or ``sector_size`` argument,
  bounds and overlaps; then what composing each storage device and device
  memory finds: missing contents, files larger than their partition,
  folders the format can't build, prebuilt images the format's ``check``
  refuses. A storage device composes with the erased value 0xFF, the
  ``IStorage`` default, and without the family's boot image. An STM32
  driver adds each segment of a prebuilt ELF that lands on a partition in
  flash.

A board file that does not load (a device as an SPI or QSPI target, a name
declared twice) never gets this far: ``jig check`` reports its error.
``jig check --contracts`` adds the checks that need the machine built in
Renode (``jig.contracts``), which composes the storage with each model's own
erased value.
"""

from __future__ import annotations

from typing import Any

from jig.errors import ConfigError
from jig.storage import compose, layout
from jig.system import generate, protocols, registry
from jig.system.boards import Board


def problems(board: Board) -> list[str]:
    """Everything wrong with ``board`` that shows without Renode; its plugin
    loads first, so the families and layers it registers count."""
    from jig.lang.library import name_collisions
    from jig.system.socs import driver_for

    plugins = registry.default()
    plugins.load_board(board)
    found = name_collisions(board, plugins.member_names("board", [board.source]))
    try:
        driver = driver_for(board)
    except ConfigError as exc:
        return [*found, str(exc)]
    check = getattr(driver, "check", None)
    if callable(check):
        found += list(check(board))
    found += stack_problems(board)
    found += storage_problems(board, driver)
    for settings in _settings(board):
        report = getattr(settings, "problems", None)
        if callable(report):
            found += list(report())
    try:
        generate.description(board, driver)
    except (ConfigError, OSError) as exc:
        found.append(f"platform description: {exc}")
    return found


def storage_problems(board: Board, driver: Any) -> list[str]:
    """The board's partitions resolved, then each target they write
    composed (see the module documentation)."""
    try:
        resolved = layout.for_board(board, driver)
    except ConfigError as exc:
        return [f"storage: {exc}"]
    if resolved.problems:
        return list(resolved.problems)
    found: list[str] = []
    for name, target in resolved.targets.items():
        if not resolved.written(name):
            continue
        erased = 0xFF if target.memory is None else target.memory.fill
        found += compose.composition_problems(resolved, name, erased=erased)
    return found


def stack_problems(board: Board) -> list[str]:
    """Each interface's stack on what the interface gives."""
    kinds = protocols.known()
    found: list[str] = []
    for device in board.devices:
        for instance in device.protocols:
            found += _unknown(instance.layer.name, instance.where, kinds)
            found += _arguments(instance.layer, instance.where, kinds)
        for stack in device.interfaces:
            unknown = [name for name in stack.names if name not in kinds]
            if unknown:
                found += [p for name in unknown for p in _unknown(name, stack.where, kinds)]
                continue
            for layer in stack.layers:
                if layer.instance is None:
                    found += _arguments(layer, stack.where, kinds)
            try:
                gives = _gives(board, device.name, stack.interface)
            except ConfigError as exc:
                found.append(f"{stack.where}: interface {stack.interface}: {exc}")
                continue
            if gives is None:
                found.append(
                    f"{stack.where}: interface {stack.interface}: a protocol stack runs on a "
                    "UART or CAN interface"
                )
                continue
            try:
                protocols.check(stack.names, gives, stack.interface, kinds)
            except ConfigError as exc:
                found.append(f"{stack.where}: {exc}")
    return found


def _unknown(name: str, where: str, kinds: dict[str, protocols.LayerKind]) -> list[str]:
    if name in kinds:
        return []
    return [f"{where}: no protocol layer {name!r} (known: {', '.join(sorted(kinds))})"]


def _arguments(layer: Any, where: str, kinds: dict[str, protocols.LayerKind]) -> list[str]:
    """A layer whose kind loaded with the board's plugin: its arguments, read now."""
    kind = kinds.get(layer.name)
    if kind is None or layer.settings is not None or kind.parse is None:
        return []
    try:
        kind.settings(layer.arguments, f"{where}: {layer.name}")
    except ConfigError as exc:
        return [str(exc)]
    return []


def _gives(board: Board, device: str, interface: str) -> str | None:
    """What a device interface gives the bottom of its stack: what the link
    it is on carries, else what the family says the interface is. A name
    the family rejects (an instance of the block design Jig has no model
    for) raises its ConfigError."""
    from jig.system.socs import driver_for

    driver = driver_for(board)
    kind = generate.interface_kind(board, board.device, driver, interface)
    for link in board.links:
        if any(m.device == device and m.name == interface for m in link.members):
            return protocols.LINK_GIVES.get(link.kind)
    return protocols.LINK_GIVES.get(kind) if kind is not None else None


def _settings(board: Board) -> list[Any]:
    found: list[Any] = list(board.sections.values())
    for device in board.devices:
        found += [instance.layer.settings for instance in device.protocols]
        found += [
            layer.settings
            for stack in device.interfaces
            for layer in stack.layers
            if layer.instance is None
        ]
    return found
