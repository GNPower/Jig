"""External CSP nodes: the ground station on a KISS link, other subsystems on CAN.

A ``CspNode`` is a CSP endpoint the harness runs outside the emulated boards. It
speaks libcsp 1.6's wire formats (``jig.ext.csp.protocol``) over
a UART carrying KISS or over a CAN bus with the CAN fragmentation protocol.

Traffic goes through connections, as in libcsp: ``connect`` opens one to a
port of another node from an ephemeral source port, plain or reliable (RDP,
``jig.ext.csp.rdp``), with or without the CRC32 option. ``send_to`` sends one
packet without a connection, from the source port and at the priority given
(csp_sendto). Packets that match no connection (housekeeping a board
publishes, a reply to ``send_to``) are kept in ``received`` and ``receive``
waits for one. Every wait is a coroutine on the simulation's scheduler, so
several exchanges can run at once and every timeout counts simulated seconds.

``cmp_ident``, ``cmp_route_set`` and ``cmp_clock`` are the CMP client
(``jig.ext.csp.cmp``).
"""

from __future__ import annotations

import bisect
import itertools
import logging
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from jig.errors import JigError
from jig.ext.csp import cmp
from jig.ext.csp import protocol as csp
from jig.ext.csp.rdp import RdpConnection, RdpOptions, State
from jig.runtime.scheduler import Scheduler, WaitTimeoutError
from jig.transports.can import CanBusPort, CanFrame
from jig.transports.uart import UartPort

log = logging.getLogger(__name__)

_T = TypeVar("_T")

# libcsp hands out outgoing ports above port_max_bind (24 by default).
EPHEMERAL_PORTS = range(25, csp.MAX_PORT + 1)


class CspError(JigError):
    """A CSP exchange failed: timeout, reset, refused connection."""


@dataclass(frozen=True)
class Received:
    packet: csp.Packet
    time_ns: int  # simulated time the packet was complete (last byte or frame)

    @property
    def time(self) -> float:
        return self.time_ns / 1e9


@dataclass
class NodeStats:
    sent: int = 0
    received: int = 0
    crc_errors: int = 0
    retransmissions: int = 0
    unmatched: int = 0
    pings_answered: int = 0
    lost_sent: int = 0  # packets sent while the link was down
    lost_bytes: int = 0  # bytes that arrived while the link was down


@dataclass
class _Trace:
    entries: list[tuple[int, str, csp.CspId, int]] = field(default_factory=list)
    limit: int = 200_000

    def add(self, time_ns: int, direction: str, ident: csp.CspId, length: int) -> None:
        if len(self.entries) < self.limit:
            self.entries.append((time_ns, direction, ident, length))


class Connection:
    """One CSP connection from a harness node (csp_conn_t)."""

    def __init__(
        self,
        node: CspNode,
        destination: int,
        dport: int,
        sport: int,
        *,
        priority: int,
        rdp: RdpConnection | None,
        crc: bool,
    ) -> None:
        self.node = node
        self.destination = destination
        self.dport = dport
        self.sport = sport
        self.priority = priority
        self.rdp = rdp
        self.crc = crc
        self.inbox: deque[bytes] = deque()
        self.user_closed = False
        self.last_rx_ns = 0  # simulated time the latest packet arrived

    def __repr__(self) -> str:
        kind = "RDP" if self.rdp else "plain"
        route = f"{self.node.address}:{self.sport} -> {self.destination}:{self.dport}"
        return f"<{kind} connection {route}>"

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.destination, self.dport, self.sport)

    @property
    def flags(self) -> int:
        return (csp.FLAG_RDP if self.rdp else 0) | (csp.FLAG_CRC32 if self.crc else 0)

    def ident(self, priority: int | None = None) -> csp.CspId:
        return csp.CspId(
            self.node.address,
            self.destination,
            self.dport,
            self.sport,
            priority=self.priority if priority is None else priority,
            flags=self.flags,
        )

    @property
    def finished(self) -> bool:
        """Closed by us and, for RDP, done with the closing handshake."""
        return self.user_closed and (self.rdp is None or self.rdp.state is State.CLOSED)

    # ------------------------------------------------------------- traffic

    async def send(self, data: bytes, timeout: float | None = None) -> None:
        """Send one packet; on RDP, wait for room in the window first."""
        if self.user_closed:
            raise CspError(f"{self}: send after close")
        rdp = self.rdp
        if rdp is None:
            self.node._transmit(self.ident(), data, crc=self.crc)
            return
        limit = timeout if timeout is not None else rdp.options.conn_timeout_ms / 1000
        await self.node.scheduler.wait_for(
            lambda: True if rdp.can_write or rdp.state is not State.OPEN else None,
            limit,
            error=lambda: CspError(f"{self}: RDP window stayed full for {limit:g} s"),
        )
        if rdp.state is not State.OPEN:
            raise CspError(f"{self}: connection {rdp.state.name.lower()} ({rdp.reset_reason})")
        rdp.write(data, self.node.now_ms)
        self.node._flush(self)

    async def read(self, timeout: float) -> bytes:
        """The next packet's payload; raises CspError on timeout or reset."""
        rdp = self.rdp

        def ready() -> bool | None:
            if self.inbox:
                return True
            if rdp is not None and (rdp.closed_by_peer or rdp.state is State.CLOSED):
                return True
            return None

        await self.node.scheduler.wait_for(
            ready,
            timeout,
            error=lambda: CspError(f"{self}: no reply within {timeout:g} s"),
        )
        if self.inbox:
            return self.inbox.popleft()
        assert rdp is not None
        reason = "closed by the peer" if rdp.closed_by_peer else rdp.reset_reason or "closed"
        raise CspError(f"{self}: connection {reason}")

    async def transaction(self, data: bytes, timeout: float) -> bytes:
        await self.send(data, timeout)
        return await self.read(timeout)

    async def close(self, drain: float = 2.0) -> None:
        """Close as libcsp does (RST, then CLOSE-WAIT), after giving data still
        in flight up to ``drain`` seconds to be acknowledged."""
        if self.user_closed:
            return
        rdp = self.rdp
        if rdp is not None and rdp.state is State.OPEN and rdp.unacknowledged:
            try:
                await self.node.scheduler.wait_for(
                    lambda: (
                        True if rdp.unacknowledged == 0 or rdp.state is not State.OPEN else None
                    ),
                    drain,
                )
            except WaitTimeoutError:
                log.debug("%s: closing with %d segments unacknowledged", self, rdp.unacknowledged)
        self.user_closed = True
        if rdp is not None:
            rdp.close(self.node.now_ms)
            self.node._flush(self)
        self.node._forget_if_finished(self)

    # ------------------------------------------------------------ reception

    def _on_payload(self, data: bytes, time_ns: int) -> None:
        self.last_rx_ns = time_ns
        rdp = self.rdp
        if rdp is None:
            self.inbox.append(data)
            return
        rdp.on_segment(data, self.node.now_ms)
        while rdp.received:
            self.inbox.append(rdp.received.popleft())
        self.node._flush(self)


class CspNode:
    """A CSP node outside the boards, attached to one KISS link or one CAN bus."""

    def __init__(
        self,
        name: str,
        address: int,
        scheduler: Scheduler,
        *,
        kiss: UartPort | None = None,
        can: CanBusPort | None = None,
        rdp_options: RdpOptions | None = None,
        seed: int = 0,
    ) -> None:
        if (kiss is None) == (can is None):
            raise ValueError("a CSP node attaches to exactly one KISS link or CAN bus")
        if not 0 <= address <= csp.MAX_ADDRESS:
            raise ValueError(f"CSP address {address} outside 0-{csp.MAX_ADDRESS}")
        self.name = name
        self.address = address
        self.scheduler = scheduler
        self.rdp_options = rdp_options or RdpOptions()
        # The RDP options for a connection to a destination, when they depend
        # on it (a board's csp.rdp); None: rdp_options for every destination.
        self.rdp_for: Callable[[int], RdpOptions] | None = None
        self._kiss = kiss
        self._can = can
        self._kiss_decoder = csp.KissDecoder()
        self._cfp = csp.CfpReassembler()
        self._cfp_ident = itertools.count()
        self._random = random.Random(f"{seed}:{name}:{address}")
        self._next_port = itertools.cycle(EPHEMERAL_PORTS)
        self._connections: dict[tuple[int, int, int], Connection] = {}
        self.received: list[Received] = []  # packets that matched no connection
        self.serve_ping = True  # answer CSP pings on port 1, as a libcsp node does
        self.accept_broadcast = False  # also take packets sent to the broadcast address
        self.link_up = True  # False: out of contact, traffic is lost both ways
        self.stats = NodeStats()
        self.trace = _Trace()
        if kiss is not None:
            kiss.subscribe(self._on_kiss)
        if can is not None:
            can.subscribe(self._on_can)
        scheduler.add_service(self.service)

    def __repr__(self) -> str:
        link = self._kiss.label if self._kiss else self._can.name if self._can else "?"
        return f"<CspNode {self.name} ({self.address}) on {link}>"

    @property
    def link(self) -> str:
        return self._kiss.label if self._kiss else self._can.name if self._can else ""

    @property
    def now_ms(self) -> int:
        return self.scheduler.now_ns // 1_000_000

    # ------------------------------------------------------------ reception

    def set_link(self, up: bool) -> None:
        """Bring the node's link up or down (a ground station's contact window).

        While it is down, what the node sends and what reaches it are lost; a
        frame cut by an outage is lost too, so reassembly starts over."""
        if up == self.link_up:
            return
        self.link_up = up
        self._kiss_decoder = csp.KissDecoder()
        self._cfp = csp.CfpReassembler()

    def _on_kiss(self, data: bytes, stamps: list[int]) -> None:
        if not self.link_up:
            self.stats.lost_bytes += len(data)
            return
        for packet, stamp_us in self._kiss_decoder.feed_timed(data, stamps):
            self._deliver(packet, stamp_us * 1000)

    def _on_can(self, frame: CanFrame) -> None:
        if not frame.extended or frame.remote:
            return
        if not self.link_up:
            self.stats.lost_bytes += len(frame.data)
            return
        src, dst, _type, _remain, _ident = csp.cfp_fields(frame.id)
        if not self._for_us(dst) or src == self.address:
            return
        packet = self._cfp.feed(frame.id, frame.data)
        if packet is not None:
            self._deliver(packet, frame.time_us * 1000)

    def _for_us(self, destination: int) -> bool:
        return destination == self.address or (
            self.accept_broadcast and destination == csp.BROADCAST
        )

    def _deliver(self, packet: csp.Packet, time_ns: int) -> None:
        if not self._for_us(packet.id.dst):
            return
        data = packet.data
        if packet.id.flags & csp.FLAG_CRC32:
            stripped = csp.crc32_strip(data)
            if stripped is None:
                self.stats.crc_errors += 1
                log.warning("%s: CRC32 mismatch on a packet from %s, dropped", self.name, packet.id)
                return
            data = stripped
        self.stats.received += 1
        self.trace.add(time_ns, "rx", packet.id, len(data))
        connection = self._connections.get((packet.id.src, packet.id.sport, packet.id.dport))
        if connection is not None:
            connection._on_payload(data, time_ns)
            return
        if (
            self.serve_ping
            and packet.id.dst == self.address
            and packet.id.dport == csp.PORT_PING
            and not packet.id.flags & csp.FLAG_RDP
        ):
            # csp_service_handler's ping: echo the payload to the sender's port.
            self.stats.pings_answered += 1
            self._transmit(packet.id.reply(), data, crc=bool(packet.id.flags & csp.FLAG_CRC32))
            return
        self.stats.unmatched += 1
        self.received.append(Received(csp.Packet(packet.id, data), time_ns))

    # ---------------------------------------------------------- transmission

    def _transmit(self, ident: csp.CspId, data: bytes, *, crc: bool) -> None:
        payload = csp.crc32_append(data) if crc else data
        packet = csp.Packet(ident, payload)
        self.stats.sent += 1
        self.trace.add(self.scheduler.now_ns, "tx", ident, len(data))
        if not self.link_up:
            self.stats.lost_sent += 1
            return
        if self._kiss is not None:
            self._kiss.write(csp.kiss_encode(packet))
            return
        assert self._can is not None
        frames = csp.cfp_fragments(packet, next(self._cfp_ident) & 0x3FF)
        if not self._can.send_frames(frames):
            log.warning("%s: no node acknowledged the frames of %s", self.name, ident)

    def _flush(self, connection: Connection) -> None:
        rdp = connection.rdp
        if rdp is None:
            return
        while rdp.outgoing:
            segment = rdp.outgoing.pop(0)
            # Control segments go out at high priority at most (csp_rdp_send_cmp).
            self._transmit(
                connection.ident(min(connection.priority, csp.PRIORITY_HIGH)),
                segment,
                crc=connection.crc,
            )

    def service(self) -> None:
        """Scheduler service: RDP timers and pending segments of every connection."""
        now = self.now_ms
        for connection in list(self._connections.values()):
            rdp = connection.rdp
            if rdp is None:
                continue
            before = rdp.retransmissions
            rdp.poll(now)
            self.stats.retransmissions += rdp.retransmissions - before
            while rdp.received:
                connection.inbox.append(rdp.received.popleft())
            self._flush(connection)
            self._forget_if_finished(connection)

    def _forget_if_finished(self, connection: Connection) -> None:
        if connection.finished and self._connections.get(connection.key) is connection:
            del self._connections[connection.key]

    # ---------------------------------------------------------- connections

    def _ephemeral_port(self, destination: int, dport: int) -> int:
        for _ in EPHEMERAL_PORTS:
            port = next(self._next_port)
            if (destination, dport, port) not in self._connections:
                return port
        raise CspError(f"{self.name}: every ephemeral port is in use")

    async def connect(
        self,
        destination: int,
        port: int,
        *,
        rdp: bool = False,
        crc: bool = False,
        priority: int = csp.PRIORITY_NORMAL,
        timeout: float | None = None,
        options: RdpOptions | None = None,
    ) -> Connection:
        """Open a connection (csp_connect). RDP connections complete the handshake
        first and raise CspError if the peer does not answer. Without
        ``options``, an RDP connection takes ``rdp_for(destination)``, else
        ``rdp_options``."""
        sport = self._ephemeral_port(destination, port)
        endpoint = None
        if rdp:
            if options is None:
                options = self.rdp_for(destination) if self.rdp_for else self.rdp_options
            endpoint = RdpConnection(options, iss=self._random.randrange(1 << 16))
        connection = Connection(
            self, destination, port, sport, priority=priority, rdp=endpoint, crc=crc
        )
        self._connections[connection.key] = connection
        if endpoint is None:
            return connection
        endpoint.open_active(self.now_ms)
        self._flush(connection)
        limit = timeout if timeout is not None else endpoint.options.conn_timeout_ms / 1000
        try:
            await self.scheduler.wait_for(
                lambda: True if endpoint.state is not State.SYN_SENT else None,
                limit,
                error=lambda: CspError(
                    f"{self.name}: no RDP answer from node {destination} port {port} "
                    f"within {limit:g} s"
                ),
            )
        except BaseException:
            connection.user_closed = True
            endpoint.close(self.now_ms)
            self._forget_if_finished(connection)
            raise
        if endpoint.state is not State.OPEN:
            connection.user_closed = True
            self._forget_if_finished(connection)
            raise CspError(
                f"{self.name}: RDP connection to node {destination} port {port} refused "
                f"({endpoint.reset_reason or endpoint.state.name})"
            )
        return connection

    # ------------------------------------------------------------- services

    async def ping(
        self, destination: int, payload: bytes = b"jig probe", timeout: float = 2.0
    ) -> float:
        """CSP ping (port 1): the destination echoes the payload.

        Returns the round trip in simulated seconds, from the moment the request
        was handed to the link to the moment the reply's last byte arrived."""
        connection = await self.connect(destination, csp.PORT_PING)
        sent = self.scheduler.now_ns
        try:
            await connection.send(payload)
            reply = await connection.read(timeout)
        finally:
            await connection.close()
        if reply != payload:
            raise CspError(
                f"{self.name}: ping reply from node {destination} carried "
                f"{reply.hex()} instead of {payload.hex()}"
            )
        return (connection.last_rx_ns - sent) / 1e9

    async def request(
        self,
        destination: int,
        port: int,
        data: bytes,
        *,
        timeout: float = 2.0,
        priority: int = csp.PRIORITY_NORMAL,
        crc: bool = False,
    ) -> bytes:
        """One request, one reply on a plain connection (csp_transaction)."""
        connection = await self.connect(destination, port, priority=priority, crc=crc)
        try:
            await connection.send(data)
            return await connection.read(timeout)
        finally:
            await connection.close()

    async def expect(
        self, match: Callable[[csp.Packet], bool], timeout: float, what: str
    ) -> Received:
        """Wait for an unsolicited packet that matches (one arriving from now on)."""
        start = len(self.received)

        def arrived() -> Received | None:
            for entry in self.received[start:]:
                if match(entry.packet):
                    return entry
            return None

        result: Received = await self.scheduler.wait_for(
            arrived,
            timeout,
            error=lambda: CspError(f"{self.name}: {what} not received within {timeout:g} s"),
        )
        return result

    # --------------------------------------------------------- connectionless

    def send_to(
        self,
        destination: int,
        dport: int,
        data: bytes,
        *,
        sport: int | None = None,
        priority: int = csp.PRIORITY_NORMAL,
        crc: bool = False,
    ) -> int:
        """Send one packet without a connection (csp_sendto), from ``sport``
        (an ephemeral port when None); returns the source port used."""
        if sport is None:
            sport = next(self._next_port)
        ident = csp.CspId(
            self.address,
            destination,
            dport,
            sport,
            priority=priority,
            flags=csp.FLAG_CRC32 if crc else 0,
        )
        self._transmit(ident, data, crc=crc)
        return sport

    async def receive(
        self,
        *,
        dport: int | None = None,
        source: int | None = None,
        sport: int | None = None,
        timeout: float = 2.0,
        since_ns: int | None = None,
    ) -> Received:
        """The first packet outside any connection, matching every filter
        given, that arrived at or after ``since_ns`` (default: now). Waits up
        to ``timeout`` simulated seconds for one."""
        start = self.scheduler.now_ns if since_ns is None else since_ns
        first = bisect.bisect_left(self.received, start, key=lambda entry: entry.time_ns)

        def arrived() -> Received | None:
            for entry in self.received[first:]:
                ident = entry.packet.id
                if (
                    (dport is None or ident.dport == dport)
                    and (source is None or ident.src == source)
                    and (sport is None or ident.sport == sport)
                ):
                    return entry
            return None

        filters = ", ".join(
            f"{name} {value}"
            for name, value in (("port", dport), ("source", source), ("sport", sport))
            if value is not None
        )
        what = f"a packet ({filters})" if filters else "a packet"
        result: Received = await self.scheduler.wait_for(
            arrived,
            timeout,
            error=lambda: CspError(f"{self.name}: {what} not received within {timeout:g} s"),
        )
        return result

    # -------------------------------------------------------------------- CMP

    async def cmp_ident(self, destination: int, *, timeout: float = 2.0) -> cmp.Ident:
        """CMP IDENT: the node's hostname, model, revision and build date."""
        reply = await self.request(destination, csp.PORT_CMP, cmp.ident_request(), timeout=timeout)
        return self._cmp(destination, cmp.parse_ident, reply)

    async def cmp_route_set(
        self,
        destination: int,
        target: int,
        interface: str,
        *,
        via: int = cmp.NO_VIA,
        timeout: float = 2.0,
    ) -> None:
        """CMP ROUTE_SET: make ``destination`` route ``target`` through
        ``interface`` (and the next hop ``via``)."""
        request = cmp.route_set_request(target, interface, via)
        reply = await self.request(destination, csp.PORT_CMP, request, timeout=timeout)
        self._cmp(
            destination, lambda data: cmp.check_reply(data, cmp.ROUTE_SET, len(request)), reply
        )

    async def cmp_clock(
        self, destination: int, seconds: int = 0, nanoseconds: int = 0, *, timeout: float = 2.0
    ) -> cmp.Clock:
        """CMP CLOCK: read the node's clock, or set it when ``seconds`` is not 0."""
        request = cmp.clock_request(seconds, nanoseconds)
        reply = await self.request(destination, csp.PORT_CMP, request, timeout=timeout)
        return self._cmp(destination, cmp.parse_clock, reply)

    def _cmp(self, destination: int, parse: Callable[[bytes], _T], reply: bytes) -> _T:
        try:
            return parse(reply)
        except cmp.CmpError as exc:
            raise CspError(f"{self.name}: node {destination}: {exc}") from None
