"""Plans with a system on the fake simulation: the fixture boards' plugins,
housekeeping codecs, harness nodes, power states and project defaults."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from jig.ext.csp import protocol as csp
from jig.ext.csp.dsl import NodeHandle
from jig.lang import PlanError, RunOptions
from jig.lang.interpreter import Interpreter
from jig.lang.library import BoardLinkRef, DslError
from jig.lang.parser import load
from jig.report.report import SuiteReport
from jig.testing import FakeBoard, FakeCanPort, FakeSimulation, run_source
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
    node gs: csp(address: 10) over kiss
    link ground: uart(members: [dut.kiss, gs])
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
        "link ground: UART 115200 baud, dut.kiss, gs",
        "node gs: CSP 10 over kiss on ground",
    ]


def test_bus_housekeeping_decodes_with_a_new_stream_each_call(tmp_path: Path) -> None:
    sim = FakeSimulation(workdir=tmp_path / "out")
    source = NODE_SYSTEM.replace(
        "node gs: csp(address: 10) over kiss\n    link ground: uart(members: [dut.kiss, gs])",
        "link can0: can(members: [dut.can, gs])\n    node gs: csp(address: 10) over cfp",
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
            node gs: csp(address: 10) over kiss
            link ground: uart(members: [node.kiss, gs])
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
        (directory / "x.txt").write_text("", encoding="utf-8")
        (directory / "plugin.py").write_text(
            f"def shared():\n    return {name!r}\n\n"
            "def register(api):\n    api.function('shared', shared)\n",
            encoding="utf-8",
        )
        (directory / "board.jig").write_text(
            f'board "{name}" {{\n    plugin: "plugin.py"\n'
            '    device mcu: stm32(part: "STM32F407VG") { firmware: "x.elf" }\n}\n',
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


# ------------------------------------------------------------- system links


@pytest.mark.parametrize(
    ("declarations", "message"),
    [
        (
            "node gs: csp(address: 10) over kiss",
            r"node gs joins no link: add it to a system link's members",
        ),
        (
            "node gs: csp(address: 10)\n link g: uart(members: [d.kiss, gs])",
            "node gs needs packets and link g gives a byte stream: put kiss between them",
        ),
        (
            "node gs: csp(address: 10) over cfp\n link g: uart(members: [d.kiss, gs])",
            "cfp needs CAN frames and link g gives a byte stream",
        ),
        (
            "node gs: csp(address: 10) over kiss\n link g: can(members: [d.can, gs])",
            "kiss needs a byte stream and link g gives CAN frames$",
        ),
        ("link g: can(members: [d.kiss])", "link g is a can link and d.kiss is uart"),
        ("external link g: uart(members: [d.kiss])", "external marks a link that leaves a board"),
        (
            "link g: uart(baud: 9600, members: [d.kiss])",
            "link g states 9600 baud and the boards' links 115200 baud",
        ),
        (
            "link g: can(members: [d.can])\n link h: can(members: [d.can])",
            "g is a system link: a board's link joins one system link at most",
        ),
        (
            'link g: uart(members: [d.kiss, e.kiss])\n board e: "example-node"',
            r"joins the UARTs of two boards \(d.kiss, e.kiss\)",
        ),
        (
            "node gs: csp(address: 10) over kiss\n link g: uart(members: [d.kiss, gs])\n"
            " link h: uart(members: [d.console, gs])",
            "node gs joins g already",
        ),
        ("link g: uart(members: [3])", "members are boards' external links and nodes, not 3"),
        ("link g: uart(members: [])", "members is a list of boards' links and nodes"),
        ("link g: uart(speed: 3, members: [d.kiss])", "unknown argument speed"),
        ("link g: 7", r"a link is declared with uart\(\.\.\.\) or can\(\.\.\.\), not 7"),
        ("node gs: kiss over cfp", r"a node is declared with csp\(\.\.\.\), not kiss over cfp"),
    ],
)
def test_system_links_check_their_members(tmp_path: Path, declarations: str, message: str) -> None:
    source = f'system {{\n board d: "example-node"\n {declarations}\n}}\n'
    with pytest.raises(PlanError, match=message):
        run_source(tmp_path, source)


def test_a_can_link_carries_csp_from_a_node_over_cfp(tmp_path: Path) -> None:
    sim = FakeSimulation(workdir=tmp_path / "out")
    report = run_source(
        tmp_path,
        """
        system {
            board d: "example-node" (power: "on")
            link bus: can(bitrate: 1 Mbit/s, members: [d.can, gs])
            node gs: csp(address: 10) over cfp
        }
        test "names" {
            check bus.bitrate == 1000000
            check d.can.bitrate == 1000000
            check d.can.frames == 0
            check d.kiss.text == ""
        }
        """,
        simulation=sim,
    )
    assert statuses(report) == {"names": "PASS"}, details(report)
    assert report.system == [
        "board d: example-node, jtag boot, 100 MIPS",
        "link bus: CAN 1 Mbit/s, d.can, gs",
        "node gs: CSP 10 over cfp on bus",
    ]
    assert set(sim.can_buses) == {"bus"}


def test_a_node_keeps_the_link_it_joined(tmp_path: Path) -> None:
    """``node.link``: what Jig handed the node when the system started, with
    the boards' links its system link joins, for plugin code that serves
    one board's link."""
    sim = FakeSimulation(workdir=tmp_path / "out")
    sim.uarts[("a", "kiss")] = FakeBoard(sim.scheduler, address=1).uart
    plan = _write(
        tmp_path / "links.jig",
        "system {\n"
        ' board a: "example-node"\n'
        ' board b: "example-node"\n'
        " node gs: csp(address: 10) over kiss\n"
        " node probe: csp(address: 11) over cfp\n"
        " link ground: uart(members: [a.kiss, gs])\n"
        " link can0: can(members: [b.can, a.can, probe])\n"
        "}\n",
    )
    interpreter = Interpreter(load(plan), sim, RunOptions())  # type: ignore[arg-type]
    interpreter.build_system()
    gs, probe = interpreter.nodes["gs"], interpreter.nodes["probe"]
    assert isinstance(gs, NodeHandle) and isinstance(probe, NodeHandle)
    with pytest.raises(DslError, match="node gs is not running yet"):
        _ = gs.link
    interpreter.start_system()
    ground = gs.link
    assert (ground.name, ground.kind) == ("ground", "uart")
    assert ground.port is sim.uarts[("a", "kiss")]
    assert [layer.name for layer in ground.layers] == ["kiss"]
    assert ground.board_links == (BoardLinkRef("a", "kiss", "uart", True, ("zynq.uart0",)),)
    can0 = probe.link
    assert (can0.name, can0.kind, can0.port) == ("can0", "can", sim.can_buses["can0"])
    assert [(ref.machine, ref.name, ref.interfaces) for ref in can0.board_links] == [
        ("b", "can", ("zynq.can0",)),
        ("a", "can", ("zynq.can0",)),
    ]


def test_a_node_pings_a_board_across_a_fake_can_link(tmp_path: Path) -> None:
    """The fake CAN link keeps and acknowledges what a harness node writes
    and hands it to the link's observers; the test answers the ping it finds
    there, as board 1 would."""
    sim = FakeSimulation(workdir=tmp_path / "out")
    reassembler = csp.CfpReassembler()
    read = 0

    def board_answers_pings() -> None:
        nonlocal read
        port = sim.can_buses.get("can0")
        if port is None:
            return
        for frame in port.written[read:]:
            packet = reassembler.feed(frame.id, frame.data)
            if packet is not None and packet.id.dst == 1 and packet.id.dport == csp.PORT_PING:
                reply = csp.Packet(packet.id.reply(), packet.data)
                stamp = sim.now_ns // 1000
                port.feed([CanFrame(i, d, time_us=stamp) for i, d in csp.cfp_fragments(reply, 0)])
        read = len(port.written)

    sim.scheduler.add_service(board_answers_pings)
    report = run_source(
        tmp_path,
        """
        system {
            board dut: "example-node" (power: "on")
            node gs: csp(address: 10) over cfp
            link can0: can(members: [dut.can, gs])
        }
        test "ping" {
            check gs.ping(dut, size: 20, timeout: 100 ms) < 50 ms
            wait 10 ms
            check len(can0.packets(src: 10, dst: 1)) == 1
            check len(can0.packets(src: 1, dst: 10)) == 1
        }
        test "listen only" { can0.acknowledge(false) }
        """,
        simulation=sim,
    )
    assert statuses(report) == {"ping": "PASS", "listen only": "PASS"}, details(report)
    port = sim.can_buses["can0"]
    assert isinstance(port, FakeCanPort) and not port.acknowledging
    assert {frame.sender for frame in port.written} == {"harness"}
    again = csp.CfpReassembler()
    sent = [again.feed(frame.id, frame.data) for frame in port.written]
    assert [(p.id.src, p.id.dst, p.id.dport, p.data) for p in sent if p is not None] == [
        (10, 1, csp.PORT_PING, bytes(range(20)))
    ]


def test_a_fake_can_port_hands_back_what_it_takes_at_the_next_service() -> None:
    sim = FakeSimulation()
    port = FakeCanPort(sim.scheduler, "can0")
    heard: list[CanFrame] = []
    port.subscribe(heard.append)
    assert port.send(0x123, b"\x01\x02", extended=False)
    assert port.send(0x7FF, b"", extended=False, remote=True)
    assert port.send_frames([(0x1C00A00, b""), (0x1C00A01, b"\xff")])
    assert heard == [] and port.frame_count == 0  # never inside the write
    sim.run(sim.scheduler.sleep(0.01))
    assert heard == port.written and port.frame_count == 4
    assert [(f.id, f.data, f.extended, f.remote, f.sender) for f in port.written] == [
        (0x123, b"\x01\x02", False, False, "harness"),
        (0x7FF, b"", False, True, "harness"),
        (0x1C00A00, b"", True, False, "harness"),
        (0x1C00A01, b"\xff", True, False, "harness"),
    ]


# ------------------------------------------------------- names plans use


def _board(tmp_path: Path, name: str, body: str) -> None:
    directory = tmp_path / "boards" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "board.jig").write_text(f'board "{name}" {{\n{body}\n}}\n', encoding="utf-8")
    (tmp_path / "jig.toml").write_text('[paths]\nboards = ["boards"]\n', encoding="utf-8")


MCU = """
    device mcu: stm32(part: "STM32F407VG") {
        firmware: "build/app.elf"
        protocol csp(address: 4)
        interface usart2: cli(eol: "\\n")
        interface can1: csp over cfp
    }
    peripheral temp: "Sensors.TI_TMP108"
    link housekeeping: i2c(controller: mcu.i2c1, targets: {0x48: temp})
    external link debug: uart(baud: 115200, members: [mcu.usart2])
    external link can: can(bitrate: 1 Mbit/s, members: [mcu.can1])
"""


def test_plans_reach_devices_peripherals_and_links_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _board(tmp_path, "pcb", MCU)
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    sim = FakeSimulation(workdir=tmp_path / "out")
    sim.on_board["b"] = lambda machine: machine.console_on("debug").replies.update(
        {"version": "v1.2\n> "}
    )
    report = run_source(
        tmp_path,
        """
        system { board b: "pcb" }
        test "names" {
            check b.mcu.name == "mcu" and b.mcu.family == "stm32"
            check b.temp.model == "Sensors.TI_TMP108"
            check b.cli("version") contains "v1.2"
            check b.debug.text contains "v1.2"
            check b.address == 4
        }
        test "a CAN link on no system link" { let n = b.can.frames }
        test "a link inside the board" { let n = b.housekeeping.frames }
        test "no console link" { let t = b.console.text }
        """,
        simulation=sim,
    )
    assert statuses(report) == {
        "names": "PASS",
        "a CAN link on no system link": "ERROR",
        "a link inside the board": "ERROR",
        "no console link": "ERROR",
    }, details(report)
    assert "b.can joins no system link, so the harness is not on it" in report.cases[1].detail
    assert "b.housekeeping is a link inside board b" in report.cases[2].detail
    assert "has no member 'console'" in report.cases[3].detail
    assert sim.machines["b"].consoles["debug"].sent == ["version"]
    assert sim.machines["b"].consoles["debug"].eol == "\n"


def test_a_fake_console_over_vt100_renders_what_a_test_feeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _board(tmp_path, "pcb", MCU.replace('cli(eol: "\\n")', 'cli(eol: "\\n") over vt100'))
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    sim = FakeSimulation(workdir=tmp_path / "out")
    sim.on_board["b"] = lambda machine: machine.console_on("debug").feed(
        "\x1b[2J\x1b[Hstats\x1b[2D\x1b[@a\r\n\x1b[33mok\x1b[0m\r\n> "
    )
    report = run_source(
        tmp_path,
        """
        system { board b: "pcb" }
        test "rendered" {
            check b.debug.text == "staats\\nok\\n> "
            check text(b.debug.raw) contains "\\x1b[33mok"
            b.debug.press("ctrl-c")
            b.debug.press("enter")
        }
        test "an unknown key" { b.debug.press("f13") }
        """,
        simulation=sim,
    )
    assert statuses(report) == {"rendered": "PASS", "an unknown key": "ERROR"}, details(report)
    assert "no key named 'f13'" in report.cases[1].detail
    assert sim.machines["b"].consoles["debug"].pressed == ["ctrl-c", "enter"]


def test_a_vt100_console_keeps_the_bytes_since_the_last_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log line between the CR and the LF that end a dump's line overwrites
    the line on the screen; raw_buffer keeps the bytes the cli call got."""
    _board(tmp_path, "pcb", MCU.replace('cli(eol: "\\n")', 'cli(eol: "\\n") over vt100'))
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    sim = FakeSimulation(workdir=tmp_path / "out")
    dump = "dump\r\n00 01 02 03\r[adc] sample ready\r\n\n04 05 06 07\r\n> "
    sim.on_board["b"] = lambda machine: machine.console_on("debug").replies.update({"dump": dump})
    report = run_source(
        tmp_path,
        """
        system { board b: "pcb" }
        test "dump" {
            check not b.cli("dump") contains "00 01 02 03"
            check text(b.debug.raw_buffer) contains "00 01 02 03\\r[adc] sample ready"
            check b.debug.raw_buffer == b.debug.raw
            b.debug.clear()
            check len(b.debug.raw_buffer) == 0 and len(b.debug.raw) == 54
        }
        """,
        simulation=sim,
    )
    assert statuses(report) == {"dump": "PASS"}, details(report)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            MCU.replace("external link debug:", "external link reset:"),
            "board pcb: link reset has the name of the board member reset; rename the link",
        ),
        (
            MCU.replace("peripheral temp:", "peripheral power_on:").replace(
                "{0x48: temp}", "{0x48: power_on}"
            ),
            "peripheral power_on has the name of the board member power_on",
        ),
        (
            MCU.replace("device mcu:", "device address:").replace("mcu.", "address."),
            "device address has the name of the board member address",
        ),
    ],
)
def test_a_name_that_hides_a_board_member_fails_to_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, message: str
) -> None:
    _board(tmp_path, "pcb", body)
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    with pytest.raises(PlanError, match=message):
        run_source(tmp_path, 'system { board b: "pcb" }\n')


def test_cli_needs_one_interface_that_runs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = MCU.replace(
        "        interface can1: csp over cfp\n",
        "        interface can1: csp over cfp\n        interface usart1: cli\n",
    ).replace(
        "    external link can:",
        "    external link console: uart(baud: 115200, members: [mcu.usart1])\n"
        "    external link can:",
    )
    _board(tmp_path, "two", second)
    _board(tmp_path, "none", MCU.replace('interface usart2: cli(eol: "\\n")', ""))
    monkeypatch.setenv("JIG_PROJECT", str(tmp_path))
    report = run_source(
        tmp_path,
        """
        system { board t: "two"
                 board n: "none" }
        test "two" { t.send("x") }
        test "none" { n.send("x") }
        test "named" { t.console.send("x") }
        """,
        simulation=FakeSimulation(workdir=tmp_path / "out"),
    )
    assert statuses(report) == {"two": "ERROR", "none": "ERROR", "named": "PASS"}
    assert "board t runs cli on debug, console: name the link" in report.cases[0].detail
    assert "board n (none) runs cli on none of its external UART links" in report.cases[1].detail


def test_a_csp_target_is_a_board_or_its_device(tmp_path: Path) -> None:
    sim, _board = fake_with_node(tmp_path)
    report = run_source(
        tmp_path,
        NODE_SYSTEM
        + """
        test "device" {
            check gs.ping(dut.zynq) >= 0
            check gs.hk(dut.zynq).uptime_ms == 1500
        }
        """,
        simulation=sim,
    )
    assert statuses(report) == {"device": "PASS"}, details(report)
