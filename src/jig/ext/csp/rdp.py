"""CSP's Reliable Datagram Protocol, as libcsp 1.6 implements it.

Source: libcsp 1.6, ``src/transport/csp_rdp.c``. The protocol is the
RFC 908 sequence/acknowledgment scheme with libcsp's extensions (delayed
acknowledgments, extended acknowledgments listing out-of-order segments).

Wire format: every RDP segment is the CSP payload followed by a 5-byte
trailer, flags (SYN 0x08, ACK 0x04, EAK 0x02, RST 0x01), then the sequence and
acknowledgment numbers as big-endian 16-bit values. A SYN carries the six
connection options as big-endian 32-bit words: window size, connection
timeout, packet timeout, delayed acknowledgments, acknowledgment timeout,
acknowledgment delay count. The passive side adopts the options of the SYN.

``RdpConnection`` holds one endpoint's state and does no I/O: feed it received
segments and the time, take the segments it wants transmitted from
``outgoing`` and the delivered payloads from ``received``. Timing follows
``csp_rdp_check_timeouts``: call ``poll`` regularly.
"""

from __future__ import annotations

import enum
import struct
from collections import deque
from dataclasses import dataclass, field

SYN = 0x08
ACK = 0x04
EAK = 0x02
RST = 0x01

HEADER_SIZE = 5
_HEADER = struct.Struct(">BHH")
_OPTIONS = struct.Struct(">6I")


def _u16(value: int) -> int:
    return value & 0xFFFF


def seq_between(seq: int, start: int, end: int) -> bool:
    """``seq`` in [start, end] on the 16-bit circle (csp_rdp_seq_between)."""
    return _u16(end - start) >= _u16(seq - start)


def seq_before(seq: int, cmp: int) -> bool:
    """``seq`` precedes ``cmp`` on the 16-bit circle (csp_rdp_seq_before)."""
    diff = _u16(seq - cmp)
    return diff >= 0x8000


def seq_after(seq: int, cmp: int) -> bool:
    return seq_before(cmp, seq)


@dataclass(frozen=True)
class RdpOptions:
    """Connection options a SYN carries (csp_rdp_set_opt).

    The defaults are libcsp 1.6's (``csp_rdp.c``, the static option values):
    a window of 4 segments, a 10 s connection timeout, a 1 s packet timeout,
    and delayed acknowledgments held for 250 ms or 2 segments."""

    window_size: int = 4
    conn_timeout_ms: int = 10_000
    packet_timeout_ms: int = 1_000
    delayed_acks: bool = True
    ack_timeout_ms: int = 250
    ack_delay_count: int = 2

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        return tuple(name for name in cls.__dataclass_fields__)

    def pack(self) -> bytes:
        return _OPTIONS.pack(
            self.window_size,
            self.conn_timeout_ms,
            self.packet_timeout_ms,
            int(self.delayed_acks),
            self.ack_timeout_ms,
            self.ack_delay_count,
        )

    @classmethod
    def unpack(cls, data: bytes) -> RdpOptions:
        window, conn, packet, delayed, ack_timeout, delay_count = _OPTIONS.unpack(data[:24])
        return cls(window, conn, packet, bool(delayed), ack_timeout, delay_count)


@dataclass(frozen=True)
class Segment:
    flags: int
    seq: int
    ack: int
    data: bytes = b""

    def encode(self) -> bytes:
        return self.data + _HEADER.pack(self.flags, _u16(self.seq), _u16(self.ack))

    @classmethod
    def decode(cls, raw: bytes) -> Segment:
        if len(raw) < HEADER_SIZE:
            raise ValueError(f"an RDP segment needs at least {HEADER_SIZE} bytes, got {len(raw)}")
        flags, seq, ack = _HEADER.unpack(raw[-HEADER_SIZE:])
        return cls(flags, seq, ack, raw[:-HEADER_SIZE])

    def describe(self) -> str:
        names = [
            name
            for bit, name in ((SYN, "SYN"), (ACK, "ACK"), (EAK, "EAK"), (RST, "RST"))
            if self.flags & bit
        ]
        return f"{'|'.join(names) or '-'} seq={self.seq} ack={self.ack} len={len(self.data)}"


class State(enum.Enum):
    CLOSED = 0
    SYN_SENT = 1
    SYN_RCVD = 2
    OPEN = 3
    CLOSE_WAIT = 4


@dataclass
class _Outstanding:
    seq: int
    flags: int
    data: bytes
    timestamp: int
    quarantine: int = 0


@dataclass
class RdpConnection:
    """One endpoint of an RDP connection."""

    options: RdpOptions = field(default_factory=RdpOptions)
    iss: int = 0
    state: State = State.CLOSED
    outgoing: list[bytes] = field(default_factory=list)
    received: deque[bytes] = field(default_factory=deque)
    closed_by_peer: bool = False
    timed_out: bool = False
    reset_reason: str = ""

    def __post_init__(self) -> None:
        self.snd_iss = _u16(self.iss)
        self.snd_nxt = _u16(self.iss + 1)
        self.snd_una = _u16(self.iss)
        self.rcv_cur = 0
        self.rcv_irs = 0
        self.rcv_lsa = 0
        self.ack_timestamp = 0
        self.close_timestamp = 0
        self._tx: list[_Outstanding] = []
        self._rx: dict[int, bytes] = {}  # out-of-order segments by sequence number
        self.retransmissions = 0

    # ------------------------------------------------------------ queries

    @property
    def is_open(self) -> bool:
        return self.state is State.OPEN

    @property
    def can_write(self) -> bool:
        """Room in the send window (csp_rdp_is_conn_ready_for_tx)."""
        return self.state is State.OPEN and not seq_after(
            self.snd_nxt, _u16(self.snd_una + self.options.window_size - 1)
        )

    @property
    def unacknowledged(self) -> int:
        return sum(1 for entry in self._tx if not seq_before(entry.seq, self.snd_una))

    # ------------------------------------------------------------ actions

    def open_active(self, now_ms: int) -> None:
        """Send a SYN with our options (csp_rdp_connect)."""
        if self.state is not State.CLOSED:
            raise RuntimeError(f"cannot open an RDP connection in state {self.state.name}")
        self.ack_timestamp = now_ms
        self.state = State.SYN_SENT
        self._send_control(SYN, self.snd_iss, 0, self.options.pack(), now_ms, keep=True)

    def write(self, data: bytes, now_ms: int) -> None:
        """Send a data segment (csp_rdp_send); the caller checks ``can_write`` first."""
        if self.state is not State.OPEN:
            raise RuntimeError(f"cannot send on an RDP connection in state {self.state.name}")
        if not self.can_write:
            raise RuntimeError("the RDP send window is full")
        seq = self.snd_nxt
        self._tx.append(_Outstanding(seq, ACK, data, now_ms))
        self.outgoing.append(Segment(ACK, seq, self.rcv_cur, data).encode())
        self.snd_nxt = _u16(self.snd_nxt + 1)

    def close(self, now_ms: int) -> None:
        """Send RST and wait in CLOSE-WAIT (csp_rdp_close)."""
        if self.state in (State.CLOSED, State.CLOSE_WAIT):
            return
        if self.state is State.SYN_SENT:
            self.state = State.CLOSED
            return
        self.state = State.CLOSE_WAIT
        self.close_timestamp = now_ms
        self._send_control(ACK | RST, self.snd_nxt, self.rcv_cur, b"", now_ms)

    def poll(self, now_ms: int) -> None:
        """Retransmissions, delayed acknowledgments, CLOSE-WAIT expiry."""
        if self.state is State.CLOSE_WAIT:
            if now_ms > self.close_timestamp + self.options.conn_timeout_ms:
                self.state = State.CLOSED
                self.timed_out = True
            return
        if (
            self.state is State.SYN_SENT
            and now_ms > self.ack_timestamp + self.options.conn_timeout_ms
        ):
            self.state = State.CLOSED
            self.timed_out = True
            self.reset_reason = "no SYN/ACK within the connection timeout"
            return
        kept: list[_Outstanding] = []
        for entry in self._tx:
            if seq_before(entry.seq, self.snd_una):
                continue  # acknowledged
            if now_ms > entry.timestamp + self.options.packet_timeout_ms:
                entry.timestamp = now_ms
                self.retransmissions += 1
                self.outgoing.append(
                    Segment(entry.flags, entry.seq, self.rcv_cur, entry.data).encode()
                )
            kept.append(entry)
        self._tx = kept
        if self.state is State.OPEN and self.options.delayed_acks:
            self._check_ack(now_ms)

    # ----------------------------------------------------------- reception

    def on_segment(self, raw: bytes, now_ms: int) -> None:
        """Process one received segment (csp_rdp_new_packet)."""
        segment = Segment.decode(raw)
        if segment.flags & RST:
            self._on_reset(segment, now_ms)
            return
        if self.state is State.CLOSED:
            if not segment.flags & SYN:
                self._send_control(RST, self.snd_nxt, self.rcv_cur, b"", now_ms)
                return
            self._on_syn(segment, now_ms)
            return
        if self.state is State.SYN_SENT:
            if segment.flags & SYN and segment.flags & ACK:
                self.rcv_cur = segment.seq
                self.rcv_irs = segment.seq
                self.rcv_lsa = _u16(segment.seq - 1)
                self.snd_una = _u16(segment.ack + 1)
                self.ack_timestamp = now_ms
                self.state = State.OPEN
                self._send_control(ACK, self.snd_nxt, self.rcv_cur, b"", now_ms)
            elif segment.flags & ACK:
                # Our SYN hit a half-open connection.
                self._send_control(RST, self.snd_nxt, self.rcv_cur, b"", now_ms)
            else:
                self._reset("invalid reply to SYN")
            return
        if self.state is State.CLOSE_WAIT:
            if segment.flags & SYN or not segment.flags & ACK:
                return
            if not self._ack_valid(segment.ack):
                return
            self.snd_una = _u16(segment.ack + 1)
            self._send_control(ACK | RST, self.snd_nxt, self.rcv_cur, b"", now_ms)
            return
        # SYN_RCVD or OPEN
        if segment.flags & SYN or not segment.flags & ACK:
            if segment.seq != self.rcv_irs:
                self._reset("SYN or missing ACK in an open connection")
            return
        if not seq_between(
            segment.seq, _u16(self.rcv_cur + 1), _u16(self.rcv_cur + self.options.window_size * 2)
        ):
            if self.state is State.SYN_RCVD:
                self._send_control(ACK | SYN, self.snd_iss, self.rcv_irs, b"", now_ms)
            else:
                self._send_eack(now_ms)
            return
        if not self._ack_valid(segment.ack):
            return
        if self.state is State.SYN_RCVD:
            if segment.ack != self.snd_iss:
                self._reset("wrong acknowledgment of our SYN/ACK")
                return
            self.state = State.OPEN
        self.snd_una = _u16(segment.ack + 1)
        if segment.flags & EAK:
            self._on_eack(segment, now_ms)
            return
        if not segment.data:
            return
        if segment.seq != _u16(self.rcv_cur + 1):
            if segment.seq not in self._rx:
                self._rx[segment.seq] = segment.data
                self._send_eack(now_ms)
            else:
                self._check_ack(now_ms)
            return
        self.received.append(segment.data)
        self.rcv_cur = segment.seq
        self._check_ack(now_ms)
        while _u16(self.rcv_cur + 1) in self._rx:
            self.rcv_cur = _u16(self.rcv_cur + 1)
            self.received.append(self._rx.pop(self.rcv_cur))

    # ------------------------------------------------------------- helpers

    def _ack_valid(self, ack: int) -> bool:
        low = _u16(self.snd_una - 1 - self.options.window_size * 2)
        return seq_between(ack, low, _u16(self.snd_nxt - 1))

    def _on_syn(self, segment: Segment, now_ms: int) -> None:
        self.rcv_cur = segment.seq
        self.rcv_irs = segment.seq
        self.rcv_lsa = segment.seq
        if len(segment.data) >= _OPTIONS.size:
            self.options = RdpOptions.unpack(segment.data)
        self.state = State.SYN_RCVD
        self._send_control(ACK | SYN, self.snd_iss, self.rcv_irs, b"", now_ms)

    def _on_reset(self, segment: Segment, now_ms: int) -> None:
        if segment.flags & ACK:
            self.snd_una = _u16(segment.ack + 1)
        if self.state is State.CLOSED:
            return
        if self.state is State.CLOSE_WAIT:
            self.state = State.CLOSED
            self.closed_by_peer = True
            return
        if segment.seq == _u16(self.rcv_cur + 1):
            # The peer closed after its last data: answer and wait out CLOSE-WAIT.
            self.state = State.CLOSE_WAIT
            self.close_timestamp = now_ms
            self.closed_by_peer = True
            self._send_control(ACK | RST, self.snd_nxt, self.rcv_cur, b"", now_ms)

    def _on_eack(self, segment: Segment, now_ms: int) -> None:
        listed = {
            struct.unpack_from(">H", segment.data, offset)[0]
            for offset in range(0, len(segment.data) - 1, 2)
        }
        kept = []
        for entry in self._tx:
            if entry.seq in listed:
                continue  # received out of order by the peer
            if any(seq_after(other, entry.seq) for other in listed) and now_ms > entry.quarantine:
                # A later segment arrived, so this one was probably lost: resend soon.
                entry.timestamp = now_ms - self.options.packet_timeout_ms - 1
                entry.quarantine = now_ms + self.options.packet_timeout_ms // 2
            kept.append(entry)
        self._tx = kept

    def _send_eack(self, now_ms: int) -> None:
        listed = b"".join(struct.pack(">H", seq) for seq in self._rx)
        self._send_control(ACK | EAK, self.snd_nxt, self.rcv_cur, listed, now_ms)

    def _should_ack(self, now_ms: int) -> bool:
        if not self.options.delayed_acks:
            return True
        if now_ms > self.ack_timestamp + self.options.ack_timeout_ms:
            return True
        return seq_after(self.rcv_cur, _u16(self.rcv_lsa + self.options.ack_delay_count))

    def _check_ack(self, now_ms: int) -> None:
        if self._should_ack(now_ms):
            self._send_control(ACK, self.snd_nxt, self.rcv_cur, b"", now_ms)

    def _send_control(
        self, flags: int, seq: int, ack: int, data: bytes, now_ms: int, *, keep: bool = False
    ) -> None:
        if keep:
            self._tx.append(_Outstanding(seq, flags, data, now_ms))
        self.outgoing.append(Segment(flags, seq, ack, data).encode())
        if flags & ACK:
            self.rcv_lsa = ack
            self.ack_timestamp = now_ms

    def _reset(self, reason: str) -> None:
        self.state = State.CLOSED
        self.reset_reason = reason
