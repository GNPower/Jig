"""Watching a CAN bus: every CSP packet on it, whoever sent it and whoever it
was for.

The harness's adapter sees every frame on a bus, so the observer reassembles
CFP fragments from all senders and keeps the packets, which lets a plan check
traffic between two emulated boards that never involves a harness node. It
keeps the most recent packets in memory (``limit``) and counts every route
(source, destination, destination port); listeners (the run recorder) get
every packet as it completes.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass

from jig.ext.csp import protocol as csp
from jig.transports.can import CanBusPort, CanFrame

DEFAULT_LIMIT = 20_000


@dataclass(frozen=True)
class Observed:
    time_ns: int  # when the packet's last frame was on the bus
    id: csp.CspId
    data: bytes  # payload, CRC-32 trailer removed when the packet carries a valid one
    crc_ok: bool = True  # False: flagged CRC-32 that does not match (data kept as sent)


class BusObserver:
    """Every CSP packet on one CAN bus."""

    def __init__(self, port: CanBusPort, limit: int = DEFAULT_LIMIT) -> None:
        self.bus = port.name
        self.packets: deque[Observed] = deque(maxlen=limit)
        self.routes: Counter[tuple[int, int, int]] = Counter()  # (src, dst, dport) -> packets
        self.total = 0
        self.crc_errors = 0  # packets whose CRC-32 trailer does not match
        self._reassembler = csp.CfpReassembler()
        self._listeners: list[Callable[[Observed], None]] = []
        port.subscribe(self._on_frame)

    @property
    def dropped(self) -> int:
        """Fragments that did not complete a packet (lost or out of order)."""
        return self._reassembler.dropped

    def subscribe(self, listener: Callable[[Observed], None]) -> None:
        self._listeners.append(listener)

    def select(
        self,
        *,
        src: int | None = None,
        dst: int | None = None,
        port: int | None = None,
        since_ns: int | None = None,
    ) -> list[Observed]:
        """Kept packets matching every filter given (``port`` is the destination port)."""
        return [
            p
            for p in self.packets
            if (src is None or p.id.src == src)
            and (dst is None or p.id.dst == dst)
            and (port is None or p.id.dport == port)
            and (since_ns is None or p.time_ns >= since_ns)
        ]

    def _on_frame(self, frame: CanFrame) -> None:
        if not frame.extended or frame.remote:
            return
        packet = self._reassembler.feed(frame.id, frame.data)
        if packet is None:
            return
        data, crc_ok = packet.data, True
        if packet.id.flags & csp.FLAG_CRC32:
            stripped = csp.crc32_strip(packet.data)
            if stripped is None:
                crc_ok = False
                self.crc_errors += 1
            else:
                data = stripped
        observed = Observed(frame.time_us * 1000, packet.id, data, crc_ok)
        self.packets.append(observed)
        self.routes[(packet.id.src, packet.id.dst, packet.id.dport)] += 1
        self.total += 1
        for listener in self._listeners:
            listener(observed)
