"""The CSP extension's additions: connectionless send and receive with
explicit ports and priority, broadcast reception, the CMP client, and where a
connection's RDP options come from."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from jig.ext.csp import cmp
from jig.ext.csp import protocol as csp
from jig.ext.csp.dsl import CspDeclaration, csp_node, ping_payload, rdp_options
from jig.ext.csp.node import CspError, CspNode
from jig.ext.csp.rdp import RdpOptions
from jig.lang.library import DslError, InterfaceRef
from jig.runtime.scheduler import Scheduler
from jig.system import boards
from jig.testing import FakeBoard, FakeClock, FakeSimulation, run_source


def setup() -> tuple[Scheduler, FakeBoard, CspNode]:
    scheduler = Scheduler(FakeClock(), step_ns=10_000_000)
    board = FakeBoard(scheduler)
    node = CspNode("gs", 8, scheduler, kiss=board.uart)  # type: ignore[arg-type]
    return scheduler, board, node


# ---------------------------------------------------------------------- ping


def test_ping_payloads_of_any_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sim = FakeSimulation(workdir=tmp_path / "out")
    board = FakeBoard(sim.scheduler, address=1)
    decoder = csp.KissDecoder()
    pings: list[bytes] = []
    write = board.uart.write

    def tap(data: bytes) -> None:
        pings.extend(p.data for p in decoder.feed(data) if p.id.dport == csp.PORT_PING)
        write(data)

    monkeypatch.setattr(board.uart, "write", tap)
    sim.uarts[("dut", "kiss")] = board.uart
    report = run_source(
        tmp_path,
        """
        system {
            board dut: "example-node" (power: "on")
            node gs: csp(address: 10, link: dut.kiss)
        }
        test "sizes" {
            for size in [0, 255, 256, 300] {
                gs.ping(dut, size: size)
            }
        }
        test "a negative size" { gs.ping(dut, size: -1) }
        """,
        simulation=sim,
    )
    statuses = {case.name: case.status for case in report.cases}
    assert statuses == {"sizes": "PASS", "a negative size": "ERROR"}, report.cases[0].detail
    assert "size is a whole number of bytes, not -1" in report.cases[1].detail
    count = bytes(range(256))
    assert pings == [b"", count[:255], count, count + count[:44]]
    assert ping_payload(600) == count * 2 + count[:88]


# ------------------------------------------------------------ connectionless


def test_send_to_uses_the_ports_and_priority_given() -> None:
    scheduler, board, node = setup()
    assert node.send_to(3, 20, b"\xc2\x10", sport=20, priority=csp.PRIORITY_HIGH) == 20
    ephemeral = node.send_to(3, 21, b"x", crc=True)
    assert 25 <= ephemeral <= 63
    scheduler.run(scheduler.sleep(0.05))
    first, second = board.received
    assert (first.id.src, first.id.dport, first.id.sport, first.id.priority) == (8, 20, 20, 1)
    assert first.data == b"\xc2\x10" and first.id.flags == 0
    assert second.id.flags == csp.FLAG_CRC32 and second.data == b"x"  # CRC checked and stripped


def test_receive_waits_for_a_matching_packet() -> None:
    scheduler, board, node = setup()
    board.send_to(8, 10, b"early", sport=10)
    scheduler.run(scheduler.sleep(0.1))

    async def later() -> None:
        await scheduler.sleep(0.5)
        board.send_to(8, 11, b"wrong port", sport=11)
        board.send_to(8, 10, b"wanted", sport=10)

    async def wait() -> tuple[bytes, float]:
        task = scheduler.spawn(later())
        found = await node.receive(dport=10, source=3, timeout=2.0)
        await task
        return found.packet.data, found.time

    data, time = scheduler.run(wait())
    assert data == b"wanted" and time == pytest.approx(0.6, abs=0.02)
    since = scheduler.run(node.receive(dport=10, timeout=0.0, since_ns=0))
    assert since.packet.data == b"early"
    with pytest.raises(CspError, match=r"a packet \(port 12, source 3\) not received within 0.2 s"):
        scheduler.run(node.receive(dport=12, source=3, timeout=0.2))


def test_broadcast_packets_reach_a_node_that_takes_them() -> None:
    scheduler, board, node = setup()
    board.send_to(csp.BROADCAST, 20, b"to all", sport=20)
    scheduler.run(scheduler.sleep(0.05))
    assert node.received == []
    node.accept_broadcast = True
    board.send_to(csp.BROADCAST, 20, b"to all", sport=20)
    board.send_to(csp.BROADCAST, csp.PORT_PING, b"no echo", sport=40)
    scheduler.run(scheduler.sleep(0.05))
    assert [entry.packet.data for entry in node.received] == [b"to all", b"no echo"]
    assert node.stats.pings_answered == 0  # a ping to the broadcast address is not answered


# ----------------------------------------------------------------------- CMP


def cmp_server(clock: list[int]) -> object:
    """A CMP handler as libcsp 1.6's service handler answers."""

    def handle(request: bytes) -> bytes | None:
        kind, code = request[0], request[1]
        if kind != cmp.REQUEST:
            return None
        if code == cmp.IDENT:
            body = b"".join(
                text.ljust(length, b"\0")
                for text, length in (
                    (b"node-3", cmp.HOSTNAME_LEN),
                    (b"SN:007", cmp.MODEL_LEN),
                    (b"v1.2-3-gabc", cmp.REVISION_LEN),
                    (b"Oct  9 2026", cmp.DATE_LEN),
                    (b"12:00:00", cmp.TIME_LEN),
                )
            )
            return bytes((cmp.REPLY, code)) + body
        if code == cmp.ROUTE_SET:
            interface = request[4:].rstrip(b"\0")
            if interface not in (b"KISS", b"CAN"):
                return None  # libcsp sends no reply when the route cannot be set
            return bytes((cmp.REPLY, code)) + request[2:]
        if code == cmp.CLOCK:
            seconds, nanoseconds = struct.unpack(">II", request[2:10])
            if seconds:
                clock[:] = [seconds, nanoseconds]
            return bytes((cmp.REPLY, code)) + struct.pack(">II", *clock)
        return None

    return handle


def test_the_cmp_client() -> None:
    scheduler, board, node = setup()
    clock = [1000, 5]
    board.serve(csp.PORT_CMP, cmp_server(clock))  # type: ignore[arg-type]
    ident = scheduler.run(node.cmp_ident(3))
    assert ident == cmp.Ident("node-3", "SN:007", "v1.2-3-gabc", "Oct  9 2026", "12:00:00")
    scheduler.run(node.cmp_route_set(3, 16, "KISS"))
    with pytest.raises(CspError, match="no reply"):
        scheduler.run(node.cmp_route_set(3, 16, "I2C", timeout=0.3))
    assert scheduler.run(node.cmp_clock(3)) == cmp.Clock(1000, 5)
    assert scheduler.run(node.cmp_clock(3, 1_700_000_000, 250)) == cmp.Clock(1_700_000_000, 250)
    assert clock == [1_700_000_000, 250]


def test_cmp_requests_have_libcsp_sizes() -> None:
    assert len(cmp.ident_request()) == cmp.IDENT_SIZE == 93
    request = cmp.route_set_request(16, "KISS")
    assert len(request) == cmp.ROUTE_SET_SIZE == 15
    assert request[:4] == bytes((0x00, 0x02, 16, 0xFF)) and request[4:8] == b"KISS"
    assert cmp.clock_request(1, 2) == bytes((0, 6)) + struct.pack(">II", 1, 2)
    with pytest.raises(ValueError, match="1 to 10 characters"):
        cmp.route_set_request(16, "INTERFACE11")
    with pytest.raises(cmp.CmpError, match="expected ff06"):
        cmp.parse_clock(bytes((0xFF, 0x01)) + bytes(8))
    with pytest.raises(cmp.CmpError, match="has 4 bytes, expected 10"):
        cmp.parse_clock(bytes(4))


def test_cmp_from_a_plan(tmp_path: Path) -> None:
    sim = FakeSimulation(workdir=tmp_path / "out")
    board = FakeBoard(sim.scheduler, address=1)
    clock = [42, 0]
    board.serve(csp.PORT_CMP, cmp_server(clock))  # type: ignore[arg-type]
    sim.uarts[("dut", "kiss")] = board.uart
    report = run_source(
        tmp_path,
        """
        system {
            board dut: "example-node" (power: "on")
            node gs: csp(address: 10, link: dut.kiss)
        }
        test "cmp" {
            check gs.ident(dut).hostname == "node-3"
            gs.route_set(dut, 16, "CAN")
            check gs.clock(dut).seconds == 42
            check gs.clock(dut, seconds: 99).seconds == 99
            gs.send(31, 20, x"c2", sport: 20, priority: 2)
            gs.send(dut, 20, x"82", sport: 20)
            let echo = gs.receive(port: 7, timeout: 100 ms)
        }
        test "bad route" { gs.route_set(dut, 16, "A-NAME-TOO-LONG") }
        """,
        simulation=sim,
    )
    statuses = {case.name: case.status for case in report.cases}
    assert statuses["bad route"] == "ERROR"
    (cmp_case, _bad) = report.cases
    assert cmp_case.status == "FAIL"  # nothing answers on port 7
    assert "a packet (port 7) not received within 0.1 s" in cmp_case.detail
    assert [p.data for p in board.received if p.id.dport == 20] == [b"\xc2", b"\x82"]


# --------------------------------------------------------------- RDP options


def test_rdp_options_come_from_libcsp_then_the_board_then_the_plan() -> None:
    assert RdpOptions() == RdpOptions(4, 10_000, 1_000, True, 250, 2)
    assert rdp_options(None, {}) == RdpOptions()
    node = boards.find("example-node")

    class Board:
        definition = node

    assert rdp_options(Board(), {}).ack_timeout_ms == 100  # type: ignore[arg-type]
    plan = rdp_options(Board(), {"ack_timeout_ms": 50, "window_size": 8})  # type: ignore[arg-type]
    assert (plan.ack_timeout_ms, plan.window_size, plan.conn_timeout_ms) == (50, 8, 10_000)


def test_the_csp_constructor() -> None:
    link = InterfaceRef("dut", "kiss", "uart")
    plain = csp_node(10, link=link)
    assert plain == CspDeclaration(10, link, None, {}, False)
    assert plain.describe() == "CSP 10 on link dut.kiss"
    tuned = csp_node(
        10,
        link=link,
        window=8,
        conn_timeout=5.0,
        packet_timeout=0.5,
        ack_timeout=0.1,
        delayed_acks=False,
        ack_delay_count=4,
        broadcast=True,
    )
    assert tuned.rdp == {
        "window_size": 8,
        "conn_timeout_ms": 5000,
        "packet_timeout_ms": 500,
        "ack_timeout_ms": 100,
        "delayed_acks": False,
        "ack_delay_count": 4,
    }
    assert tuned.broadcast
    for kwargs, message in (
        ({}, "exactly one of link"),
        ({"link": InterfaceRef("dut", "can", "can")}, "link is a UART interface"),
        ({"link": link, "bus": object()}, "exactly one of link"),
    ):
        with pytest.raises(DslError, match=message):
            csp_node(10, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(DslError, match="address must be 0-31"):
        csp_node(32, link=link)


def test_every_rdp_connection_to_a_board_takes_its_options(tmp_path: Path) -> None:
    """A plugin that opens connections on the CspNode itself (an upload
    client, say) gets the target board's csp.rdp too."""
    from jig.ext.csp.dsl import NodeHandle
    from jig.lang import RunOptions
    from jig.lang.interpreter import Interpreter
    from jig.lang.parser import load

    sim = FakeSimulation(workdir=tmp_path / "out")
    sim.uarts[("dut", "kiss")] = FakeBoard(sim.scheduler, address=1).uart
    plan = tmp_path / "plan.jig"
    plan.write_text(
        'system {\n board dut: "example-node"\n'
        " node gs: csp(address: 10, link: dut.kiss)\n"
        " node tuned: csp(address: 11, link: dut.kiss, ack_timeout: 50 ms)\n}\n",
        encoding="utf-8",
    )
    interpreter = Interpreter(load(plan), sim, RunOptions())  # type: ignore[arg-type]
    interpreter.build_system()
    interpreter.start_system()
    gs, tuned = interpreter.nodes["gs"], interpreter.nodes["tuned"]
    assert isinstance(gs, NodeHandle) and isinstance(tuned, NodeHandle)
    assert gs.node.rdp_for is not None
    assert gs.node.rdp_for(1).ack_timeout_ms == 100  # example-node's csp.rdp
    assert gs.node.rdp_for(7).ack_timeout_ms == 250  # no board: libcsp's default
    assert tuned.node.rdp_for(1).ack_timeout_ms == 50  # the declaration wins
    assert tuned.node.rdp_options.ack_timeout_ms == 50
