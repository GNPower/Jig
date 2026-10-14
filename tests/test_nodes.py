"""The harness CSP node and RDP connections against a fake board."""

from __future__ import annotations

import pytest

from jig.ext.csp.node import CspError, CspNode
from jig.ext.csp.rdp import RdpOptions
from jig.runtime.scheduler import Scheduler
from jig.testing import FakeBoard, FakeClock, PayloadHandler


def setup(options: RdpOptions | None = None) -> tuple[Scheduler, FakeBoard, CspNode]:
    scheduler = Scheduler(FakeClock(), step_ns=10_000_000)
    board = FakeBoard(scheduler)
    node = CspNode("gs", 8, scheduler, kiss=board.uart, rdp_options=options)  # type: ignore[arg-type]
    return scheduler, board, node


def test_ping_echoes() -> None:
    scheduler, _board, node = setup()
    rtt = scheduler.run(node.ping(3, b"hello"))
    assert rtt >= 0


def test_the_default_ping_payload_keeps_nine_bytes() -> None:
    # Nine bytes: one BEGIN and one MORE CFP frame on CAN, a fixed KISS length.
    assert CspNode.ping.__defaults__ == (b"jig probe", 2.0)
    assert len(b"jig probe") == 9


def test_an_rdp_service_answers_on_its_connection() -> None:
    scheduler, board, node = setup()

    def upper() -> PayloadHandler:
        return lambda payload: payload.upper()

    board.serve_rdp(9, upper)

    async def run() -> bytes:
        connection = await node.connect(3, 9, rdp=True, crc=True)
        try:
            return await connection.transaction(b"abc", 2.0)
        finally:
            await connection.close()

    assert scheduler.run(run()) == b"ABC"


def test_lost_rdp_segments_are_retransmitted() -> None:
    scheduler, board, node = setup(RdpOptions(packet_timeout_ms=300))
    received = bytearray()

    def sink() -> PayloadHandler:
        def handle(payload: bytes) -> bytes | None:
            received.extend(payload)
            return None

        return handle

    board.serve_rdp(9, sink)
    seen = {"count": 0}

    def drop_every_fifth(_raw: bytes) -> bool:
        seen["count"] += 1
        return seen["count"] % 5 == 0

    board.drop_segment = drop_every_fifth
    data = b"0123456789" * 300

    async def run() -> None:
        connection = await node.connect(3, 9, rdp=True, crc=True)
        try:
            for start in range(0, len(data), 100):
                await connection.send(data[start : start + 100])
            await scheduler.wait_for(lambda: True if len(received) == len(data) else None, 30.0)
        finally:
            await connection.close()

    scheduler.run(run())
    assert bytes(received) == data
    assert node.stats.retransmissions > 0


def test_an_unanswered_rdp_connection_fails() -> None:
    scheduler, board, node = setup(RdpOptions(conn_timeout_ms=500))
    board.serve_rdp(9, lambda: lambda _payload: None)

    async def run() -> None:
        await node.connect(3, 10, rdp=True, crc=True)  # the fake board serves port 9 only

    with pytest.raises(CspError, match="no RDP answer"):
        scheduler.run(run())


def test_a_lost_link_loses_traffic_both_ways() -> None:
    scheduler, _board, node = setup()
    node.set_link(False)
    with pytest.raises(CspError, match="no reply"):
        scheduler.run(node.ping(3, b"in the dark", timeout=0.5))
    assert node.stats.lost_sent == 1
    node.set_link(True)
    assert scheduler.run(node.ping(3, b"acquired")) >= 0
    assert node.stats.lost_sent == 1
