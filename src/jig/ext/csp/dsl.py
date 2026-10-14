"""The CSP extension's plan members.

* ``csp(address, link | bus, ...)`` declares a harness node in a system:
  ``node gs: csp(address: 10, link: dut.kiss)``. RDP options given here
  override the target board's ``csp.rdp``, which overrides libcsp's defaults.
* A node (``NodeHandle``) pings, sends requests, opens connections, sends and
  receives connectionless packets with explicit ports and priority, asks CMP
  (``ident``, ``route_set``, ``clock``) and asks for housekeeping
  (``hk``, ``hk_published``), decoded with the target board's codec.
* A bus gets ``packets``, ``traffic`` and ``hk``: what the CSP bus observer
  saw on it, whoever sent it.
* A board gets ``address``, its ``csp.address``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from jig.errors import ConfigError
from jig.ext.csp import protocol as csp
from jig.ext.csp.node import Connection, CspNode
from jig.ext.csp.observer import BusObserver, Observed
from jig.ext.csp.rdp import RdpOptions
from jig.ext.csp.settings import SECTION, CspSettings, HkContext, HkDecoder, Housekeeping
from jig.lang.library import (
    BackgroundBody,
    BackgroundHandle,
    BoardHandle,
    BusHandle,
    DslError,
    InterfaceRef,
    NodeDeclaration,
    SystemContext,
    as_bytes,
    as_seconds,
)
from jig.lang.values import Record, record_of, show, type_name
from jig.runtime.scheduler import Scheduler, seconds_to_ns
from jig.system import registry
from jig.system.system import BusContext

OBSERVER = "csp"
PACKET_HEADER = ["t", "src", "dst", "sport", "dport", "priority", "flags", "length", "crc_ok"]
_PATTERN = bytes(range(256))


def ping_payload(size: Any) -> bytes:
    """``size`` bytes counting up from 0 and wrapping after 255, what a ping
    sends when the plan gives no payload."""
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise DslError(f"ping: size is a whole number of bytes, not {show(size)}")
    return (_PATTERN * (size // len(_PATTERN) + 1))[:size]


# --------------------------------------------------------------- boards


def settings_of(board: BoardHandle) -> CspSettings:
    found = board.definition.section(SECTION)
    return found if isinstance(found, CspSettings) else CspSettings()


def csp_address(board: BoardHandle) -> int:
    """The board's ``csp.address``."""
    address = settings_of(board).address
    if address is None:
        raise ConfigError(f"board {board.definition.name} declares no CSP address (csp.address)")
    return address


def housekeeping_of(board: BoardHandle | None, address: int) -> Housekeeping:
    if board is None:
        raise DslError(f"no housekeeping for node {address}: no board in the system has it")
    spec = settings_of(board).housekeeping
    if spec is None:
        raise DslError(
            f"no housekeeping for {board.name}: board {board.definition.name} "
            "declares no csp.housekeeping"
        )
    return spec


def decoder_for(board: BoardHandle, spec: Housekeeping) -> HkDecoder:
    """A new decoder from the board's housekeeping codec."""
    try:
        entry = registry.default().codec(spec.codec)
    except ConfigError as exc:
        raise DslError(f"board {board.definition.name}: {exc}") from None
    definition = board.definition
    context = HkContext(
        definition.name, definition.directory, definition.root, spec.port, spec.options
    )
    try:
        decoder: HkDecoder = entry.value(context)
    except Exception as exc:
        raise DslError(
            f"housekeeping codec {spec.codec!r} for board {definition.name}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not callable(decoder):
        raise DslError(f"housekeeping codec {spec.codec!r} returned {decoder!r}, not a decoder")
    return decoder


def rdp_options(board: BoardHandle | None, overrides: dict[str, Any]) -> RdpOptions:
    """libcsp's defaults, then the board's csp.rdp, then the node's own options."""
    board_options = dict(settings_of(board).rdp) if board is not None else {}
    return replace(RdpOptions(), **{**board_options, **overrides})


# ---------------------------------------------------------------- nodes


class ConnectionHandle:
    """A CSP connection a plan opened."""

    dsl_members = frozenset({"send", "read", "transaction", "close", "port"})

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    @property
    def port(self) -> int:
        return self._connection.sport

    async def send(self, data: Any) -> None:
        await self._connection.send(as_bytes(data, "data"))

    async def read(self, timeout: float = 2.0) -> bytes:
        return await self._connection.read(as_seconds(timeout, "timeout"))

    async def transaction(self, data: Any, timeout: float = 2.0) -> bytes:
        return await self._connection.transaction(
            as_bytes(data, "data"), as_seconds(timeout, "timeout")
        )

    async def close(self) -> None:
        await self._connection.close()


@dataclass
class CspDeclaration(NodeDeclaration):
    """``csp(...)`` in a system declaration."""

    address: int
    link: InterfaceRef | None = None
    bus: str | None = None
    rdp: dict[str, Any] = field(default_factory=dict)  # RdpOptions fields the plan set
    broadcast: bool = False

    def describe(self) -> str:
        where = f"link {self.link!r}" if self.link else f"bus {self.bus}"
        return f"CSP {self.address} on {where}"

    def create(self, name: str, system: SystemContext) -> NodeHandle:
        return NodeHandle(name, self, system)


class NodeHandle:
    """A CSP node the harness runs (a ground station, another subsystem)."""

    dsl_kind = "node"
    dsl_members = frozenset(
        {
            "name",
            "address",
            "stats",
            "ping",
            "request",
            "received",
            "connect",
            "link_up",
            "link_down",
            "linked",
            "hk",
            "hk_published",
            "send",
            "receive",
            "ident",
            "route_set",
            "clock",
        }
    )

    def __init__(self, name: str, spec: CspDeclaration, system: SystemContext) -> None:
        self._name = name
        self._spec = spec
        self._system = system
        self._node: CspNode | None = None
        self._decoders: dict[int, HkDecoder] = {}

    def __repr__(self) -> str:
        return f"<node {self._name} ({self._spec.address})>"

    def start(self, system: SystemContext) -> None:
        """Attach the node to its link or bus (the simulation has started)."""
        spec = self._spec
        kiss = system.uart(spec.link) if spec.link is not None else None
        can = system.can_bus(spec.bus) if spec.bus is not None else None
        node = CspNode(
            self._name,
            spec.address,
            system.scheduler,
            kiss=kiss,
            can=can,
            rdp_options=rdp_options(None, spec.rdp),
            seed=system.seed,
        )
        node.accept_broadcast = spec.broadcast
        self.attach(node)

    def attach(self, node: CspNode) -> None:
        """Run on ``node``; every RDP connection it opens to a board of the
        system (from a plan or a plugin) takes that board's options."""
        node.rdp_for = self.rdp_options_for
        self._node = node

    def rdp_options_for(self, address: int) -> RdpOptions:
        """libcsp's RDP defaults, then the ``csp.rdp`` of the board at
        ``address``, then the options this node's declaration sets."""
        return rdp_options(self.target(address)[1], self._spec.rdp)

    @property
    def node(self) -> CspNode:
        if self._node is None:
            raise DslError(f"node {self._name} is not running yet")
        return self._node

    @property
    def spec(self) -> CspDeclaration:
        return self._spec

    def start_background(self, name: str, body: BackgroundBody) -> BackgroundHandle:
        return self._system.start_background(name, body)

    @property
    def scheduler(self) -> Scheduler:
        return self._system.scheduler

    @property
    def name(self) -> str:
        return self._name

    @property
    def address(self) -> int:
        return self._spec.address

    @property
    def linked(self) -> bool:
        return self.node.link_up

    def link_up(self) -> None:
        """Restore the node's link (a ground station acquiring the spacecraft)."""
        self._set_link(True)

    def link_down(self) -> None:
        """Lose the node's link: traffic both ways is lost until ``link_up``."""
        self._set_link(False)

    def _set_link(self, up: bool) -> None:
        self.node.set_link(up)
        self._system.emit(self._name, "link", "up" if up else "down")

    @property
    def stats(self) -> Record:
        return record_of(self.node.stats)

    @property
    def received(self) -> list[Record]:
        """Packets addressed to this node outside any connection."""
        return [
            Record(
                time=entry.time,
                source=entry.packet.id.src,
                sport=entry.packet.id.sport,
                dport=entry.packet.id.dport,
                data=entry.packet.data,
            )
            for entry in self.node.received
        ]

    def target(self, target: Any) -> tuple[int, BoardHandle | None]:
        """A board or a CSP address, as (address, the board at that address or None)."""
        if isinstance(target, BoardHandle):
            return csp_address(target), target
        if isinstance(target, int) and not isinstance(target, bool):
            for board in self._system.boards.values():
                if settings_of(board).address == target:
                    return target, board
            return target, None
        raise DslError(f"a CSP target is a board or an address, not {type_name(target)}")

    async def ping(
        self, target: Any, payload: Any = None, size: int = 10, timeout: float = 2.0
    ) -> float:
        """CSP ping; returns the round trip in seconds. Without a ``payload``
        it sends ``size`` bytes counting 0, 1, 2 ... 255, 0, 1 ..."""
        address, _board = self.target(target)
        data = as_bytes(payload, "payload") if payload is not None else ping_payload(size)
        return await self.node.ping(address, data, as_seconds(timeout, "timeout"))

    async def request(
        self,
        target: Any,
        port: int,
        data: Any = b"",
        timeout: float = 2.0,
        crc: bool = False,
        priority: int = csp.PRIORITY_NORMAL,
    ) -> bytes:
        """One request and its reply on a plain connection."""
        address, _board = self.target(target)
        return await self.node.request(
            address,
            int(port),
            as_bytes(data, "data"),
            timeout=as_seconds(timeout, "timeout"),
            priority=int(priority),
            crc=bool(crc),
        )

    async def connect(
        self,
        target: Any,
        port: int,
        rdp: bool = True,
        crc: bool = True,
        timeout: float | None = None,
    ) -> ConnectionHandle:
        address, board = self.target(target)
        connection = await self.node.connect(
            address,
            int(port),
            rdp=bool(rdp),
            crc=bool(crc),
            timeout=timeout,
            options=rdp_options(board, self._spec.rdp),
        )
        return ConnectionHandle(connection)

    # ---------------------------------------------------- connectionless

    def send(
        self,
        target: Any,
        port: int,
        data: Any = b"",
        sport: Any = None,
        priority: int = csp.PRIORITY_NORMAL,
        crc: bool = False,
    ) -> None:
        """One packet without a connection, to a board, an address or the
        broadcast address 31, from ``sport`` (an ephemeral port by default)."""
        address, _board = self.target(target)
        self.node.send_to(
            address,
            int(port),
            as_bytes(data, "data"),
            sport=None if sport is None else int(sport),
            priority=int(priority),
            crc=bool(crc),
        )

    async def receive(
        self,
        port: Any = None,
        source: Any = None,
        sport: Any = None,
        timeout: float = 2.0,
        since: Any = None,
    ) -> Record:
        """The first packet outside any connection that matches (destination
        ``port``, ``source`` board or address, source port), arriving from
        ``since`` (simulated seconds; default now) on."""
        address = None if source is None else self.target(source)[0]
        entry = await self.node.receive(
            dport=None if port is None else int(port),
            source=address,
            sport=None if sport is None else int(sport),
            timeout=as_seconds(timeout, "timeout"),
            since_ns=None if since is None else seconds_to_ns(as_seconds(since, "since")),
        )
        return Record(
            time=entry.time,
            source=entry.packet.id.src,
            sport=entry.packet.id.sport,
            dport=entry.packet.id.dport,
            data=entry.packet.data,
            priority=entry.packet.id.priority,
        )

    # ------------------------------------------------------------- CMP

    async def ident(self, target: Any, timeout: float = 2.0) -> Record:
        """CMP IDENT: hostname, model, revision, date and time of the build."""
        address, _board = self.target(target)
        found = await self.node.cmp_ident(address, timeout=as_seconds(timeout, "timeout"))
        return record_of(found)

    async def route_set(
        self, target: Any, destination: int, interface: str, via: int = 255, timeout: float = 2.0
    ) -> None:
        """CMP ROUTE_SET: the target routes ``destination`` through ``interface``."""
        address, _board = self.target(target)
        try:
            await self.node.cmp_route_set(
                address,
                int(destination),
                str(interface),
                via=int(via),
                timeout=as_seconds(timeout, "timeout"),
            )
        except ValueError as exc:
            raise DslError(str(exc)) from None

    async def clock(
        self, target: Any, seconds: Any = None, nanoseconds: int = 0, timeout: float = 2.0
    ) -> Record:
        """CMP CLOCK: the target's clock; with ``seconds``, set it first."""
        address, _board = self.target(target)
        try:
            found = await self.node.cmp_clock(
                address,
                0 if seconds is None else int(seconds),
                int(nanoseconds),
                timeout=as_seconds(timeout, "timeout"),
            )
        except ValueError as exc:
            raise DslError(str(exc)) from None
        return record_of(found)

    # --------------------------------------------------------- housekeeping

    def _decoder(self, address: int, board: BoardHandle | None) -> tuple[Housekeeping, HkDecoder]:
        spec = housekeeping_of(board, address)
        assert board is not None
        decoder = self._decoders.get(address)
        if decoder is None:
            decoder = decoder_for(board, spec)
            self._decoders[address] = decoder
        return spec, decoder

    async def hk(self, target: Any, port: Any = None, timeout: float = 2.0) -> Record:
        """Request housekeeping and decode it with the board's codec."""
        address, board = self.target(target)
        spec, decoder = self._decoder(address, board)
        raw = await self.node.request(
            address,
            spec.port if port is None else int(port),
            spec.request,
            timeout=as_seconds(timeout, "timeout"),
        )
        return Record(decoder(raw))

    def hk_published(self, target: Any = None, port: Any = None) -> list[Record]:
        """Housekeeping packets boards sent this node on their own, decoded:
        from ``target`` (default: any board), to ``port`` (default: the
        sending board's housekeeping port)."""
        explicit = None if port is None else int(port)
        fixed = self.target(target) if target is not None else None
        if fixed is not None:
            housekeeping_of(fixed[1], fixed[0])
        records = []
        for entry in self.node.received:
            ident = entry.packet.id
            if explicit is not None and ident.dport != explicit:
                continue
            address, board = fixed if fixed is not None else self.target(ident.src)
            if ident.src != address:
                continue
            if explicit is None:
                spec = settings_of(board).housekeeping if board is not None else None
                if spec is None or ident.dport != spec.port:
                    continue
            _spec, decoder = self._decoder(address, board)
            records.append(Record({**decoder(entry.packet.data), "time": entry.time}))
        return records


def csp_node(
    address: int,
    link: Any = None,
    bus: Any = None,
    window: Any = None,
    conn_timeout: Any = None,
    packet_timeout: Any = None,
    ack_timeout: Any = None,
    delayed_acks: Any = None,
    ack_delay_count: Any = None,
    broadcast: bool = False,
) -> CspDeclaration:
    """``node gs: csp(address: 10, link: dut.kiss)``: a harness CSP node on a
    board's KISS UART or on a CAN bus."""
    if (link is None) == (bus is None):
        raise DslError("csp(): give exactly one of link (a board's KISS UART) or bus")
    if link is not None and (not isinstance(link, InterfaceRef) or link.kind != "uart"):
        raise DslError(f"csp(): link is a UART interface of a board, not {show(link)}")
    if bus is not None and not isinstance(bus, BusHandle):
        raise DslError(f"csp(): bus is a declared bus, not {show(bus)}")
    if not isinstance(address, int) or isinstance(address, bool) or not 0 <= address <= 31:
        raise DslError(f"csp(): address must be 0-31, not {show(address)}")
    rdp: dict[str, Any] = {}
    if window is not None:
        rdp["window_size"] = int(window)
    if conn_timeout is not None:
        rdp["conn_timeout_ms"] = round(as_seconds(conn_timeout, "conn_timeout") * 1000)
    if packet_timeout is not None:
        rdp["packet_timeout_ms"] = round(as_seconds(packet_timeout, "packet_timeout") * 1000)
    if ack_timeout is not None:
        rdp["ack_timeout_ms"] = round(as_seconds(ack_timeout, "ack_timeout") * 1000)
    if delayed_acks is not None:
        rdp["delayed_acks"] = bool(delayed_acks)
    if ack_delay_count is not None:
        rdp["ack_delay_count"] = int(ack_delay_count)
    return CspDeclaration(
        int(address),
        link,
        bus.name if bus is not None else None,
        rdp,
        bool(broadcast),
    )


# ------------------------------------------------------------------ buses


def observer_of(bus: BusHandle) -> BusObserver:
    found = bus.observer(OBSERVER)
    assert isinstance(found, BusObserver)
    return found


def packet_row(packet: Observed) -> list[Any]:
    ident = packet.id
    return [
        packet.time_ns / 1e9,
        ident.src,
        ident.dst,
        ident.sport,
        ident.dport,
        ident.priority,
        f"0x{ident.flags:02x}",
        len(packet.data),
        int(packet.crc_ok),
    ]


def observe(context: BusContext) -> BusObserver:
    """The bus observer factory: every CSP packet on the bus, streamed to
    ``<bus>.packets.csv``."""
    observer = BusObserver(context.port)
    table = f"{context.name}.packets.csv"
    observer.subscribe(lambda packet: context.record(table, PACKET_HEADER, packet_row(packet)))
    return observer


def bus_packets(
    bus: BusHandle, src: Any = None, dst: Any = None, port: Any = None, since: Any = None
) -> list[Record]:
    """CSP packets seen on the bus, whoever sent them, filtered by source and
    destination address, destination port and time (simulated seconds). The
    bus keeps the most recent 20000; the run directory has all of them."""
    selected = observer_of(bus).select(
        src=None if src is None else int(src),
        dst=None if dst is None else int(dst),
        port=None if port is None else int(port),
        since_ns=None if since is None else seconds_to_ns(as_seconds(since, "since")),
    )
    return [
        Record(
            time=packet.time_ns / 1e9,
            src=packet.id.src,
            dst=packet.id.dst,
            sport=packet.id.sport,
            dport=packet.id.dport,
            flags=packet.id.flags,
            length=len(packet.data),
            crc_ok=packet.crc_ok,
            data=packet.data,
        )
        for packet in selected
    ]


def bus_traffic(bus: BusHandle) -> Record:
    """Frames and packets seen so far, packets per route ("3->0:7"),
    fragments that completed no packet and packets with a bad CRC-32."""
    observer = observer_of(bus)
    routes = {
        f"{src}->{dst}:{port}": count for (src, dst, port), count in sorted(observer.routes.items())
    }
    return Record(
        frames=bus.frames,
        packets=observer.total,
        incomplete=observer.dropped,
        crc_errors=observer.crc_errors,
        routes=routes,
    )


def bus_hk(bus: BusHandle, board: Any, port: Any = None) -> list[Record]:
    """Housekeeping the board sent on this bus to any node, decoded with its
    codec: every packet from its housekeeping port, with the time it finished
    on the wire and the node it went to (``dst``). The harness only listens."""
    if not isinstance(board, BoardHandle):
        raise DslError("hk: the argument is a board")
    address = csp_address(board)
    spec = housekeeping_of(board, address)
    decoder = decoder_for(board, spec)
    wanted = spec.port if port is None else int(port)
    records = []
    for packet in observer_of(bus).select(src=address):
        if packet.id.sport != wanted or not packet.crc_ok:
            continue
        record = dict(decoder(packet.data))
        record["time"] = packet.time_ns / 1e9
        record["dst"] = packet.id.dst
        records.append(Record(record))
    return records
