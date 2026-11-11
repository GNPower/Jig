"""A device's own memories: the ones a device family names, and reading and
writing their bytes in a running machine.

A family's driver names its memories with ``memories(board)``: an STM32's
``flash`` and ``sram``, ``sram1`` to ``sram5``; a Zynq-7000's ``ocm`` and the
block RAM of its programmable logic. A partition's ``on`` names one as
``DEVICE.MEMORY`` (``on: mcu.flash``), and a system's storage map as a quoted
key (``"mcu.flash"``). When Jig writes what a memory receives depends on its
kind:

``NON_VOLATILE``
    once, before the first boot; what the firmware programs stays, as on
    silicon.
``VOLATILE``
    at every power-on, as the memory's contents at power-on.
``PL_BLOCK_RAM``
    each time the programmable logic is configured, as a bitstream's
    initial values are.

The family's driver does the writing at those points (``MemoryImage``). A
memory is one or more of the machine's ``Memory.MappedMemory`` peripherals
(``Piece``): a Zynq-7000's OCM is ``ocmLow``, its first 192 KiB, and
``ocmHigh``, its last 64 KiB. ``read`` and ``write`` reach the pieces
through IronPython in the machine (the bridge's ``python`` request), with
the bytes in a file, so a megabyte costs two file copies and no monitor
text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jig.errors import ConfigError

NON_VOLATILE = "non-volatile"
VOLATILE = "volatile"
PL_BLOCK_RAM = "PL block RAM"
KINDS = (NON_VOLATILE, VOLATILE, PL_BLOCK_RAM)


class MachineSession(Protocol):
    """The part of ``jig.renode.session.RenodeSession`` reading and writing
    use."""

    def select_machine(self, name: str) -> None: ...

    def python(self, code: str) -> str: ...


@dataclass(frozen=True)
class Piece:
    """One ``Memory.MappedMemory`` of the machine (``peripheral``, its name on
    the system bus) holding ``size`` bytes of a memory from ``offset``."""

    peripheral: str
    offset: int
    size: int


@dataclass(frozen=True)
class DeviceMemory:
    """A memory of the device itself: its name (``flash``, ``ocm``), size and
    kind, the byte a partition's format leaves where it writes nothing
    (``fill``: 0xFF for flash, 0 for RAM, which the machine clears to 0), the
    pieces it is made of (one, named like the memory, unless given) and the
    bus address of its first byte when it sits in one range of the bus."""

    name: str
    size: int
    kind: str
    fill: int = 0x00
    pieces: tuple[Piece, ...] = ()
    address: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ConfigError(f"memory {self.name}: kind {self.kind!r} is not one of {KINDS}")
        if not 0 <= self.fill <= 0xFF:
            raise ConfigError(f"memory {self.name}: fill is a byte, not {self.fill}")
        if not self.pieces:
            object.__setattr__(self, "pieces", (Piece(self.name, 0, self.size),))
        start = 0
        for piece in self.pieces:
            if piece.offset != start or piece.size <= 0:
                raise ConfigError(f"memory {self.name}: its pieces leave a gap at 0x{start:X}")
            start += piece.size
        if start != self.size:
            raise ConfigError(f"memory {self.name}: its pieces hold {start} of {self.size} bytes")

    def spans(self, offset: int, count: int) -> list[tuple[Piece, int, int, int]]:
        """Where bytes ``offset`` to ``offset + count`` lie: (piece, offset in
        the piece, offset in those bytes, length) for each piece they touch."""
        if offset < 0 or count < 0 or offset + count > self.size:
            raise ConfigError(
                f"memory {self.name}: 0x{offset:X} + {count} bytes runs past its {self.size} bytes"
            )
        found = []
        for piece in self.pieces:
            low = max(offset, piece.offset)
            high = min(offset + count, piece.offset + piece.size)
            if low < high:
                found.append((piece, low - piece.offset, low - offset, high - low))
        return found


@dataclass(frozen=True)
class RegionFile:
    """Bytes a memory receives: all of ``path``, from ``offset``. ``label``
    says where they came from (``partition config``, a file of the system's
    storage map)."""

    offset: int
    size: int
    path: Path
    label: str

    @property
    def end(self) -> int:
        return self.offset + self.size


@dataclass(frozen=True)
class MemoryImage:
    """What one memory receives, in the order Jig writes it."""

    memory: DeviceMemory
    regions: tuple[RegionFile, ...] = field(default_factory=tuple)


def _machine_name(peripheral: str) -> str:
    return peripheral if peripheral.startswith("sysbus.") else f"sysbus.{peripheral}"


def read(
    session: MachineSession,
    machine: str,
    memory: DeviceMemory,
    offset: int,
    count: int,
    directory: Path,
) -> bytes:
    """``count`` bytes of ``memory`` from ``offset``, as the machine holds them
    now. Each piece's bytes pass through a file in ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    session.select_machine(machine)
    data = bytearray()
    for index, (piece, start, _skip, length) in enumerate(memory.spans(offset, count)):
        scratch = directory / f".{memory.name}-{index}.read"
        session.python(
            "__import__('System').IO.File.WriteAllBytes("
            f"{scratch.as_posix()!r}, "
            f"self.Machine[{_machine_name(piece.peripheral)!r}].ReadBytes({start}, {length}))"
        )
        try:
            data += scratch.read_bytes()
        finally:
            scratch.unlink(missing_ok=True)
    if len(data) != count:
        raise ConfigError(
            f"{machine}: reading {count} bytes of {memory.name} at 0x{offset:X} gave {len(data)}"
        )
    return bytes(data)


def write(
    session: MachineSession, machine: str, memory: DeviceMemory, offset: int, path: Path
) -> None:
    """All of the file ``path`` into ``memory`` from ``offset``."""
    size = path.stat().st_size
    session.select_machine(machine)
    for piece, start, skip, length in memory.spans(offset, size):
        session.python(
            f"(lambda data: self.Machine[{_machine_name(piece.peripheral)!r}]"
            f".WriteBytes({start}, data, {skip}, {length}))"
            f"(__import__('System').IO.File.ReadAllBytes({path.as_posix()!r}))"
        )


def load(
    session: MachineSession, machine: str, image: MemoryImage, kinds: Sequence[str] = KINDS
) -> None:
    """Write each region of ``image``, in order, when the memory's kind is
    one of ``kinds``."""
    if image.memory.kind not in kinds:
        return
    for region in image.regions:
        write(session, machine, image.memory, region.offset, region.path)
