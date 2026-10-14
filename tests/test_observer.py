"""The CAN bus observer and the run recorder, without Renode."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from jig.ext.csp import dsl
from jig.ext.csp import protocol as csp
from jig.ext.csp.observer import BusObserver
from jig.report.report import Step
from jig.runtime.recorder import Recorder
from jig.system.system import BusContext
from jig.transports.can import CanBusPort, CanFrame


def frames_of(packet: csp.Packet, ident: int, start_us: int) -> list[CanFrame]:
    return [
        CanFrame(frame_id, data, time_us=start_us + 130 * index)
        for index, (frame_id, data) in enumerate(csp.cfp_fragments(packet, ident))
    ]


def test_observer_sees_packets_between_other_nodes() -> None:
    port = CanBusPort(None, "can0")  # type: ignore[arg-type]
    observer = BusObserver(port, limit=2)
    request = csp.Packet(csp.CspId(src=0, dst=3, dport=7, sport=40), b"\x01")
    reply = csp.Packet(csp.CspId(src=3, dst=0, dport=40, sport=7), bytes(range(200)))
    signed = csp.Packet(
        csp.CspId(src=8, dst=1, dport=1, sport=33, flags=csp.FLAG_CRC32),
        csp.crc32_append(b"ping"),
    )
    seen = []
    observer.subscribe(seen.append)
    port.feed(frames_of(request, 5, 1000))
    port.feed(frames_of(reply, 9, 2000))
    port.feed(frames_of(signed, 1, 9000))
    assert observer.total == 3 and len(seen) == 3
    assert [p.id.dst for p in observer.packets] == [0, 1]  # limit 2 keeps the latest
    assert observer.routes[(3, 0, 40)] == 1 and observer.routes[(0, 3, 7)] == 1
    (from_node3,) = [p for p in seen if p.id.src == 3]
    assert from_node3.data == bytes(range(200))
    assert from_node3.time_ns == (2000 + 130 * (len(frames_of(reply, 9, 0)) - 1)) * 1000
    (with_crc,) = observer.select(src=8)
    assert with_crc.data == b"ping" and with_crc.crc_ok
    expected = sum(len(frames_of(packet, 0, 0)) for packet in (request, reply, signed))
    assert port.frame_count == expected
    assert observer.dropped == 0


def test_recorder_streams_each_kind_to_its_file(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "run")
    recorder.event(1_500_000_000, "node1", "power", "off for 1 s")
    recorder.step("soak", Step(2.0, "main", "m.jig:3:5", "check", "check x", "ok"))
    recorder.record("soak", "rail_v", 2.5, 1552)
    recorder.record("soak", "label", 3.0, b"\x01\x02")
    port = CanBusPort(None, "can0")  # type: ignore[arg-type]
    # The CSP extension's bus observer writes <bus>.packets.csv through the recorder.
    dsl.observe(BusContext("can0", port, recorder))
    port.feed(frames_of(csp.Packet(csp.CspId(src=3, dst=0, dport=7, sport=40), b"hk"), 2, 10))
    recorder.close()
    run = tmp_path / "run"
    assert sorted(path.name for path in run.iterdir()) == [
        "can0.packets.csv",
        "events.jsonl",
        "steps.jsonl",
        "telemetry.csv",
    ]
    (event,) = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    assert event == {"t": 1.5, "source": "node1", "kind": "power", "detail": "off for 1 s"}
    rows = list(csv.reader((run / "telemetry.csv").open()))
    assert rows == [
        ["t", "block", "name", "value"],
        ["2.5", "soak", "rail_v", "1552"],
        ["3.0", "soak", "label", "0102"],
    ]
    packets = list(csv.DictReader((run / "can0.packets.csv").open()))
    assert list(packets[0]) == dsl.PACKET_HEADER
    assert packets[0]["src"] == "3" and packets[0]["dport"] == "7" and packets[0]["length"] == "2"
    assert recorder.counts["steps.jsonl"] == 1


def test_recorder_tables_keep_inside_the_run_directory(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "run")
    for name in ("telemetry.csv", "../escape.csv", "sub/table.csv", "table.txt"):
        with pytest.raises(ValueError, match="not a table name"):
            recorder.table(name, ["a"], [1])
    recorder.table("bus.extra.csv", ["a", "b"], [1, 2])
    recorder.table("bus.extra.csv", ["a", "b"], [3, 4])
    recorder.close()
    lines = (tmp_path / "run" / "bus.extra.csv").read_text().splitlines()
    assert lines == ["a,b", "1,2", "3,4"]
