"""CAN transport: the harness node on a Jig ``CanBus``.

A ``CanBus`` (the C# model ``Jig.Harness.CanBus``) joins CAN controllers of
one or more machines. It decides acknowledgment and bit-rate validity the way
a physical bus does, records every frame with its simulated time, and lets the
harness inject frames as an external node (a CAN adapter on the bench). The
adapter sends queued frames back to back at the bus bit rate.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from jig.errors import RenodeError
from jig.renode.session import RenodeSession

log = logging.getLogger(__name__)

_MAX_FRAMES_PER_WRITE = 256
RECENT_FRAMES = 10_000  # frames a port keeps in memory; the recorder keeps the packets


@dataclass(frozen=True)
class CanFrame:
    id: int
    data: bytes
    extended: bool = True
    remote: bool = False
    time_us: int = 0  # simulated time the frame finished on the wire
    sender: str = ""  # "harness" or the transmitting controller


def parse_frames(text: str) -> list[CanFrame]:
    """Parse ``CanBus Read`` output: ``us:IDhex:flags@sender:data`` entries."""
    frames = []
    for entry in text.split():
        stamp, ident, rest = entry.split(":", 2)
        flags, _, rest = rest.partition("@")
        sender, _, data = rest.rpartition(":")
        frames.append(
            CanFrame(
                id=int(ident, 16),
                data=bytes.fromhex(data),
                extended="E" in flags,
                remote="R" in flags,
                time_us=int(stamp),
                sender=sender,
            )
        )
    return frames


def parse_collected(line: str) -> tuple[str, list[CanFrame]]:
    """Parse a ``C <name> <frames>`` collection line."""
    _kind, name, *entries = line.split(" ")
    try:
        return name, parse_frames(" ".join(entries))
    except ValueError as exc:
        raise RenodeError(f"malformed CAN collection line {line[:80]!r}") from exc


class CanBusPort:
    """The harness's node on one CAN bus."""

    def __init__(self, session: RenodeSession, name: str) -> None:
        self._session = session
        self.name = name
        self.frame_count = 0  # every frame seen on the bus
        self.recent: deque[CanFrame] = deque(maxlen=RECENT_FRAMES)  # the latest, for diagnostics
        self._listeners: list[Callable[[CanFrame], None]] = []

    @classmethod
    def create(cls, session: RenodeSession, name: str, bitrate: float) -> CanBusPort:
        session.execute(f'emulation CreateCanBus "{name}" {bitrate:g}')
        return cls(session, name)

    def connect(self, machine: str, peripheral: str) -> None:
        self._session.select_machine(machine)
        self._session.execute(f"connector Connect sysbus.{peripheral} {self.name}")

    def set_acknowledging(self, enabled: bool) -> None:
        """Whether the harness node acknowledges frames (it does by default)."""
        self._session.execute(f"{self.name} HarnessAcknowledges {str(enabled).lower()}")

    def subscribe(self, listener: Callable[[CanFrame], None]) -> None:
        """Call ``listener`` with every frame on the bus, the harness's own included."""
        self._listeners.append(listener)

    def feed(self, frames: list[CanFrame]) -> None:
        """Deliver frames the harness collected after a step."""
        for frame in frames:
            self.frame_count += 1
            self.recent.append(frame)
            for listener in self._listeners:
                listener(frame)

    def send(
        self, frame_id: int, data: bytes, *, extended: bool = True, remote: bool = False
    ) -> bool:
        """Queue one frame on the harness adapter; True when some node acknowledges."""
        flags = f"{str(extended).lower()} {str(remote).lower()}"
        reply = self._session.execute(
            f'{self.name} Write {frame_id:#x} "{data.hex()}" {flags}'
        ).strip()
        return self._acknowledged(reply)

    def send_frames(self, frames: Iterable[tuple[int, bytes]], *, extended: bool = True) -> bool:
        """Queue several frames in order in as few monitor commands as possible."""
        entries = [
            f"{frame_id:X}:{'E' if extended else ''}:{data.hex()}" for frame_id, data in frames
        ]
        acknowledged = True
        for start in range(0, len(entries), _MAX_FRAMES_PER_WRITE):
            chunk = " ".join(entries[start : start + _MAX_FRAMES_PER_WRITE])
            reply = self._session.execute(f'{self.name} WriteFrames "{chunk}"').strip()
            acknowledged = self._acknowledged(reply) and acknowledged
        return acknowledged

    def _acknowledged(self, reply: str) -> bool:
        if reply not in ("ack", "noack"):
            raise RenodeError(f"{self.name}: unexpected reply to a write: {reply!r}")
        return reply == "ack"
