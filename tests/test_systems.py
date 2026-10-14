"""Plans with a system on the fake simulation: the fixture boards' plugins,
housekeeping codecs, harness nodes, power states and project defaults."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from jig.ext.csp import protocol as csp
from jig.lang import PlanError, RunOptions
from jig.lang.interpreter import Interpreter
from jig.lang.parser import load
from jig.report.report import SuiteReport
from jig.testing import FakeBoard, FakeSimulation, run_source
from jig.transports.can import CanFrame

STATUS = struct.Struct(">4sBBBBIIIIIII")


def status(uptime_ms: int, node: int = 1) -> bytes:
    return STATUS.pack(b"ZEDS", 1, node, 0, 0, uptime_ms, 5, 6, 7, 8, 9, 0)


def statuses(report: SuiteReport) -> dict[str, str]:
    return {case.name: case.status for case in report.all_cases()}


def details(report: SuiteReport) -> list[str]:
    return [case.detail for case in report.all_cases() if case.status != "PASS"]


def fake_with_node(tmp_path: Path) -> tuple[FakeSimulation, FakeBoard]:
    """A fake simulation whose example-node board answers on its KISS link."""
    sim = FakeSimulation(workdir=tmp_path / "out")
    board = FakeBoard(sim.scheduler, address=1)
    uptime = {"ms": 1000}

    def answer(payload: bytes) -> bytes | None:
        if payload != b"\x01":
            return None
        uptime["ms"] += 500
        return status(uptime["ms"])

    board.serve(10, answer)
    sim.uarts[("dut", "kiss")] = board.uart
    return sim, board


NODE_SYSTEM = """
system "node" {
    board dut: "example-node" (power: "on")
    node gs: csp(address: 10, link: dut.kiss)
}
"""


def test_housekeeping_dispatches_through_the_board_codec(tmp_path: Path) -> None:
    sim, board = fake_with_node(tmp_path)
    board.send_to(10, 10, status(250), sport=10)  # published, unasked
    board.send_to(10, 11, b"other port", sport=11)
    report = run_source(
        tmp_path,
        NODE_SYSTEM
        + """
        test "hk" {
            let first = gs.hk(dut)
            check first.magic == "ZEDS"
            check first.uptime_ms == 1500
            check first.reply == 1
            check first.label == "status"
            check gs.hk(dut).reply == 2
            check gs.hk(1).uptime_ms == 2500
        }
        test "published" {
            let published = gs.hk_published(dut)
            check len(published) == 1
            check published[0].uptime_ms == 250
            check published[0].reply == 4
            check published[0].time >= 0
            check len(gs.hk_published()) == 1
            check len(gs.hk_published(dut, port: 12)) == 0
        }
        test "not housekeeping" { let x = gs.hk_published(dut, port: 11) }
        """,
        simulation=sim,
    )
    assert statuses(report) == {
        "hk": "PASS",
        "published": "PASS",
        "not housekeeping": "ERROR",
    }, details(report)
    assert "not a status reply" in report.cases[2].detail
    assert report.system == [
        "board dut: example-node, jtag boot, 100 MIPS",
        "node gs: CSP 10 on link dut.kiss",
    ]


def test_bus_housekeeping_decodes_with_a_new_stream_each_call(tmp_path: Path) -> None:
    sim = FakeSimulation(workdir=tmp_path / "out")
    source = NODE_SYSTEM.replace(
        "node gs: csp(address: 10, link: dut.kiss)",
        "bus can0: can(members: [dut.can])\n    node gs: csp(address: 10, bus: can0)",
    )
    program = load(_write(tmp_path / "bus.jig", source + _BUS_TESTS))
    interpreter = Interpreter(program, sim, RunOptions())  # type: ignore[arg-type]
    report = SuiteReport(name="bus", source="bus.jig")
    report.system = interpreter.build_system()
    interpreter.start_system()
    reply = csp.Packet(csp.CspId(src=1, dst=10, dport=40, sport=10), status(777))
    other = csp.Packet(csp.CspId(src=1, dst=10, dport=41, sport=11), b"not housekeeping")
    port = sim.can_buses["can0"]
    for number, packet in enumerate((reply, other, reply)):
        frames = csp.cfp_fragments(packet, number)
        port.feed([CanFrame(i, d, time_us=1000 * number) for i, d in frames])
    interpreter.run(report)
    assert statuses(report) == {"bus": "PASS"}, details(report)
    rows = sim.recorded["can0.packets.csv"]
    assert rows[0][:3] == ["t", "src", "dst"] and len(rows) == 4


_BUS_TESTS = """
test "bus" {
    let replies = can0.hk(dut)
    check len(replies) == 2
    check replies[0].uptime_ms == 777 and replies[0].reply == 1 and replies[1].reply == 2
    check replies[0].dst == 10
    check can0.hk(dut)[0].reply == 1
    check can0.traffic().packets == 3
    check len(can0.packets(src: 1, port: 41)) == 1
    check dut.address == 1
}
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_board_members_are_scoped_and_node_members_dispatch(tmp_path: Path) -> None:
    sim = FakeSimulation(workdir=tmp_path / "out")
    for name, address in (("node", 1), ("pkg", 2)):
        sim.uarts[(name, "kiss")] = FakeBoard(sim.scheduler, address=address).uart
    report = run_source(
        tmp_path,
        """
        system {
            board node: "example-node"
            board pkg: "example-package"
            node gs: csp(address: 10, link: node.kiss)
        }
        test "scoped" {
            check node.origin() == "example-node"
            check pkg.origin() == "example-package"
            check node.where.board == "example-node"
            check node.where.root == "fixtures"
            check status_size() == 36 and package_name() == "example-package"
            check gs.describe(node) == "example-node describes node"
            check gs.describe(pkg) == "example-package describes pkg"
            check gs.describe(2) == "example-package describes 2"
        }
        test "pkg has no where" { let x = pkg.where }
        test "describe needs a board" { let x = gs.describe(7) }
        """,
        simulation=sim,
    )
    assert statuses(report)["scoped"] == "PASS", details(report)
    assert "has no member 'where'" in report.cases[1].detail
    assert "origin" in report.cases[1].detail  # the listing names registered members
    assert "name the board first" in report.cases[2].detail


def test_a_background_service_runs_until_its_block_ends(tmp_path: Path) -> None:
    sim, board = fake_with_node(tmp_path)
    report = run_source(
        tmp_path,
        NODE_SYSTEM
        + """
        test "heartbeat" {
            let beat = gs.heartbeat(dut)
            wait 3500 ms
            check beat.running and beat.updates == 4
            beat.stop()
            wait 2 s
            check not beat.running
        }
        test "bad arguments" { gs.heartbeat() }
        """,
        simulation=sim,
    )
    assert statuses(report)["heartbeat"] == "PASS", details(report)
    beats = [p for p in board.received if p.id.dport == 20]
    assert [p.data for p in beats] == [b"\x82"] * 4
    assert {(p.id.sport, p.id.priority) for p in beats} == {(20, 2)}
    assert report.cases[1].status == "ERROR"


def test_power_states_on_the_fake_simulation(tmp_path: Path) -> None:
    sim = FakeSimulation(workdir=tmp_path / "out")
    report = run_source(
        tmp_path,
        """
        system {
            board node: "example-node"
            board zynq: "example-zynq" (power: "off")
        }
        test "power" {
            check not node.powered and not zynq.powered
            node.power_on()
            check node.powered
            node.power_cycle(off: 2 s)
            check node.powered and now() == 2
            node.power_off()
            check not node.powered
        }
        """,
        simulation=sim,
    )
    assert statuses(report) == {"power": "PASS"}, details(report)
    assert report.system[0].endswith("100 MIPS, powered off")
    kinds = [(e.source, e.kind, e.detail) for e in sim.events]
    assert kinds == [
        ("node", "power", "off"),
        ("zynq", "power", "off"),
        ("node", "power", "on"),
        ("node", "power", "off for 2 s"),
        ("node", "power", "on"),
        ("node", "power", "off"),
    ]


def test_a_plan_runs_a_boards_boot_flow_again(tmp_path: Path) -> None:
    sim = FakeSimulation(workdir=tmp_path / "out")
    report = run_source(
        tmp_path,
        """
        system { board node: "example-node" (power: "on") }
        test "after a power cycle" {
            check node.boot_mode == "jtag"
            node.power_cycle(off: 500 ms)
            node.boot()
            check node.powered
        }
        test "not without power" {
            node.power_off()
            node.boot()
        }
        """,
        simulation=sim,
    )
    assert statuses(report) == {
        "after a power cycle": "PASS",
        "not without power": "ERROR",
    }, details(report)
    assert "node is powered off: power it on before booting it" in report.cases[1].detail
    assert [(e.time_ns, e.kind, e.detail) for e in sim.events] == [
        (0, "boot", "jtag"),
        (0, "power", "off for 0.5 s"),
        (500_000_000, "power", "on"),
        (500_000_000, "boot", "jtag"),
        (500_000_000, "power", "off"),
    ]


def test_a_plan_power_setting_must_be_on_or_off(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match='power is "on" or "off"'):
        run_source(tmp_path, 'system { board b: "example-zynq" (power: "dim") }\n')


def test_two_plugins_defining_one_function_in_a_system_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("first", "second"):
        directory = tmp_path / "boards" / name
        directory.mkdir(parents=True)
        (directory / "x.repl").write_text("", encoding="utf-8")
        (directory / "plugin.py").write_text(
            f"def shared():\n    return {name!r}\n\n"
            "def register(api):\n    api.function('shared', shared)\n",
            encoding="utf-8",
        )
        (directory / "board.yaml").write_text(
            f"schema: 2\nname: {name}\nsoc: stm32\nplatform: x.repl\nplugin: plugin.py\n",
            encoding="utf-8",
        )
    (tmp_path / "jig.toml").write_text('[paths]\nboards = ["boards"]\n', encoding="utf-8")
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    alone = run_source(
        tmp_path,
        'system { board a: "first" }\ntest "t" { check shared() == "first" }\n',
        name="alone",
    )
    assert statuses(alone) == {"t": "PASS"}, details(alone)
    with pytest.raises(
        PlanError, match=r"function 'shared': board second .* and so does board first"
    ):
        run_source(tmp_path, 'system { board a: "first"\n board b: "second" }\n', name="both")


def test_the_project_default_board_serves_plans_without_a_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .conftest import FIXTURES

    (tmp_path / "jig.toml").write_text(
        f'[paths]\nboards = ["{(FIXTURES / "boards").as_posix()}"]\n'
        '[defaults]\nboard = "example-zynq"\noptions = {pl = "rtl"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    sim = FakeSimulation(workdir=tmp_path / "out")
    report = run_source(
        tmp_path, 'test "t" { check dut.board == "example-zynq" }\n', simulation=sim
    )
    assert statuses(report) == {"t": "PASS"}, details(report)
    assert sim.machines["dut"].config.options == {"pl": "rtl"}
    # --option wins over the project's default; the plan's choice sits between.
    sim = FakeSimulation(workdir=tmp_path / "out2")
    run_source(
        tmp_path,
        'system { board b: "example-zynq" (pl: "model") }\n',
        simulation=sim,
        name="chosen",
    )
    assert sim.machines["b"].config.options == {"pl": "model"}
    sim = FakeSimulation(workdir=tmp_path / "out3")
    run_source(
        tmp_path,
        'system { board b: "example-zynq" (pl: "model") }\n',
        simulation=sim,
        name="forced",
        options=RunOptions(board_options={"pl": "rtl"}),
    )
    assert sim.machines["b"].config.options == {"pl": "rtl"}


def test_a_system_declares_nodes_only_with_a_node_kind(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match=r"a node is declared with csp\(\.\.\.\), not 3"):
        run_source(tmp_path, 'system { board b: "example-zynq"\n node n: 3 }\n')


def test_unknown_board_settings_list_power(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match="has: boot, mips, storage, power, pl"):
        run_source(tmp_path, 'system { board b: "example-zynq" (speed: 2) }\n')
