"""RDP endpoints talking to each other through a lossy channel."""

from __future__ import annotations

from collections.abc import Callable

from jig.ext.csp import rdp
from jig.ext.csp.rdp import RdpConnection, RdpOptions, Segment, State


def shuttle(
    a: RdpConnection,
    b: RdpConnection,
    now: int,
    drop: Callable[[str, Segment], bool] = lambda _direction, _segment: False,
) -> list[tuple[str, Segment]]:
    """Move segments both ways until neither side has anything to send."""
    log: list[tuple[str, Segment]] = []
    while a.outgoing or b.outgoing:
        for source, target, direction in ((a, b, "a>b"), (b, a, "b>a")):
            pending, source.outgoing = source.outgoing, []
            for raw in pending:
                segment = Segment.decode(raw)
                log.append((direction, segment))
                if not drop(direction, segment):
                    target.on_segment(raw, now)
    return log


def connect(
    options: RdpOptions | None = None, client_iss: int = 100
) -> tuple[RdpConnection, RdpConnection]:
    client = RdpConnection(options or RdpOptions(), iss=client_iss)
    server = RdpConnection(RdpOptions(window_size=1, delayed_acks=False), iss=5000)
    client.open_active(0)
    shuttle(client, server, 0)
    return client, server


def test_segment_round_trip_and_flags() -> None:
    segment = Segment(rdp.ACK | rdp.SYN, 0xFFFE, 7, b"abc")
    raw = segment.encode()
    assert raw == b"abc" + bytes([0x0C, 0xFF, 0xFE, 0x00, 0x07])
    assert Segment.decode(raw) == segment


def test_sequence_comparisons_wrap() -> None:
    assert rdp.seq_before(0xFFFF, 0)
    assert rdp.seq_after(1, 0xFFFF)
    assert rdp.seq_between(0, 0xFFFE, 2)
    assert not rdp.seq_between(3, 0xFFFE, 2)


def test_handshake_and_options() -> None:
    options = RdpOptions(window_size=6, ack_timeout_ms=77)
    client, server = connect(options)
    assert client.state is State.OPEN
    assert server.state is State.OPEN
    assert server.options == options  # the passive side adopts the SYN's options


def test_in_order_transfer_respects_the_window() -> None:
    client, server = connect(RdpOptions(window_size=4, delayed_acks=False))
    payloads = [bytes([i]) * 10 for i in range(12)]
    sent = 0
    now = 1
    while sent < len(payloads):
        while sent < len(payloads) and client.can_write:
            client.write(payloads[sent], now)
            sent += 1
        assert client.unacknowledged <= 4
        shuttle(client, server, now)
        now += 1
    assert list(server.received) == payloads
    client.poll(now)
    assert client.unacknowledged == 0


def test_a_lost_segment_is_retransmitted_and_delivered_in_order() -> None:
    client, server = connect(RdpOptions(window_size=4, delayed_acks=False, packet_timeout_ms=1000))
    for index in range(3):
        client.write(bytes([index]), 10)
    first_data = client.snd_una  # sequence number of the first data segment
    dropped: list[int] = []

    def drop_first(direction: str, segment: Segment) -> bool:
        if direction == "a>b" and segment.data and segment.seq == first_data and not dropped:
            dropped.append(segment.seq)
            return True
        return False

    log = shuttle(client, server, 10, drop_first)
    assert dropped
    assert any(seg.flags & rdp.EAK for direction, seg in log if direction == "b>a")
    assert list(server.received) == []  # held until the gap is filled
    client.poll(2000)  # after the packet timeout
    shuttle(client, server, 2000)
    assert list(server.received) == [b"\x00", b"\x01", b"\x02"]
    assert client.retransmissions >= 1


def test_delayed_acknowledgments_wait_for_the_count_or_timeout() -> None:
    client = RdpConnection(
        RdpOptions(window_size=8, delayed_acks=True, ack_timeout_ms=100, ack_delay_count=2), iss=1
    )
    server = RdpConnection(iss=9000)
    client.open_active(0)
    shuttle(client, server, 0)
    client.write(b"one", 1)
    log = shuttle(client, server, 1)
    acks_after_one = [s for d, s in log if d == "b>a" and not s.data]
    assert acks_after_one == []  # one segment: the ACK is delayed
    server.poll(150)  # past the acknowledgment timeout
    log = shuttle(client, server, 150)
    assert any(d == "b>a" and s.flags & rdp.ACK for d, s in log)


def test_close_handshake() -> None:
    client, server = connect()
    client.write(b"last", 5)
    shuttle(client, server, 5)
    client.close(10)
    shuttle(client, server, 10)
    assert server.closed_by_peer
    assert server.state is State.CLOSE_WAIT
    assert client.state is State.CLOSED
    server.poll(10 + server.options.conn_timeout_ms + 1)
    assert server.state is State.CLOSED


def test_wrapping_sequence_numbers() -> None:
    client, server = connect(RdpOptions(window_size=4, delayed_acks=False), client_iss=0xFFFD)
    for index in range(6):
        while not client.can_write:
            shuttle(client, server, index)
        client.write(bytes([index]), index)
        shuttle(client, server, index)
    assert list(server.received) == [bytes([i]) for i in range(6)]


def test_a_syn_that_is_never_answered_times_out() -> None:
    client = RdpConnection(RdpOptions(conn_timeout_ms=500, packet_timeout_ms=200), iss=3)
    client.open_active(0)
    client.outgoing.clear()
    client.poll(300)
    assert client.outgoing  # the SYN went out again
    client.poll(501)
    assert client.state is State.CLOSED
    assert client.timed_out
