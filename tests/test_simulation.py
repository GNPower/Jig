"""Simulations and machines without Renode: a recording session stands in
for it, so the monitor commands each step sends can be checked."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jig.errors import ConfigError, ToolchainError
from jig.ext.zynq7000.driver import ZynqHooks
from jig.lang import run_plan
from jig.renode import platforms
from jig.renode.session import ModelTiming
from jig.system import boards, system
from jig.system.renode_shim import ModelSet, RenodeConfig, RenodeInstall
from jig.system.socs.stm32 import Stm32Driver
from jig.system.system import Console, Machine, Simulation
from jig.transports.uart import UartPort

from .conftest import FIXTURES


class RecordingSession:
    """The RenodeSession methods a simulation calls, recorded."""

    def __init__(self, **settings: Any) -> None:
        self.settings = settings
        self.install: RenodeInstall | None = settings.get("install")
        self.commands: list[str] = []
        self.machines: list[tuple[str, bool]] = []
        self.started = self.stopped = False
        self.elapsed_ns = 0
        self.startup_seconds: float | None = None
        self.model_timings: list[ModelTiming] = []

    def start(self) -> None:
        self.started = True
        self.startup_seconds = 4.25
        self.model_timings = [ModelTiming("jig", 26, 1.5)] + [
            ModelTiming(model_set.name, len(model_set.files), 0.25)
            for model_set in self.settings.get("model_sets", ())
        ]
        # The record RenodeSession.start writes, which the reports read.
        workdir: Path | None = self.settings.get("workdir")
        if workdir is None or self.install is None:
            return
        workdir.mkdir(parents=True, exist_ok=True)
        record = {
            "renode": self.install.as_dict(),
            "startup_seconds": self.startup_seconds,
            "model_sets": [
                {"name": t.name, "files": t.files, "seconds": t.seconds} for t in self.model_timings
            ],
        }
        (workdir / "session.json").write_text(json.dumps(record), encoding="utf-8")

    def stop(self) -> None:
        self.stopped = True

    def execute(self, command: str) -> str:
        self.commands.append(command)
        if command == "bootrom State":
            return "Booted\n"
        if command.endswith((" Write", "WriteFrames")) or " Write " in command:
            return "ack"
        return ""

    def python(self, code: str) -> str:
        return ""

    def create_machine(self, name: str, *, own_time: bool = False) -> None:
        self.machines.append((name, own_time))

    def select_machine(self, name: str) -> None:
        self.commands.append(f"(select {name})")

    def step_and_collect(self, nanoseconds: int) -> str:
        self.elapsed_ns += nanoseconds
        return ""


class TalkingSession(RecordingSession):
    """A recording session whose UARTs send what ``script`` says, each entry
    (simulated ns, Renode external name, bytes) in the step that reaches it."""

    def __init__(self, script: list[tuple[int, str, bytes]], **settings: Any) -> None:
        super().__init__(**settings)
        self.script = sorted(script)

    def step_and_collect(self, nanoseconds: int) -> str:
        start = self.elapsed_ns
        self.elapsed_ns += nanoseconds
        lines = []
        for time_ns, port, data in self.script:
            if start < time_ns <= self.elapsed_ns:
                stamps = [str(time_ns // 1000)] + ["0"] * (len(data) - 1)
                lines.append(f"U {port} {','.join(stamps)} {data.hex()}")
        return "\n".join(lines)


class FakeRenode(list[RecordingSession]):
    """The sessions simulations opened, and what each asked discovery for."""

    def __init__(self) -> None:
        super().__init__()
        self.finds: list[tuple[Path | None, RenodeConfig | None]] = []
        self.untested: list[bool] = []  # allow_untested, per find


@pytest.fixture
def renode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRenode:
    """Every session a simulation opens, recording instead of running Renode."""
    fake = FakeRenode()

    def find(
        *, explicit: Path | None, config: RenodeConfig | None, allow_untested: bool = False
    ) -> RenodeInstall:
        fake.finds.append((explicit, config))
        fake.untested.append(allow_untested)
        return RenodeInstall(tmp_path / "renode", tmp_path, "1.17.0", "test", "net8")

    def open_session(**settings: Any) -> RecordingSession:
        session = RecordingSession(**settings)
        fake.append(session)
        return session

    monkeypatch.setattr(system, "find_renode", find)
    monkeypatch.setattr(system, "open_session", open_session)
    return fake


def built(tmp_path: Path, name: str = "run") -> Simulation:
    return Simulation(name=name, workdir=tmp_path / name)


def test_the_session_opens_at_start_with_the_boards_model_sets(
    project: Path, tmp_path: Path, renode: FakeRenode
) -> None:
    (project / "build" / "node.elf").write_bytes(b"\x7fELF")
    (project / "build" / "package.elf").write_bytes(b"\x7fELF")  # stm32.firmware.elf
    sim = built(tmp_path)
    with pytest.raises(ConfigError, match="the simulation has not started"):
        sim.session  # noqa: B018
    assert sim.now_ns == 0
    sim.add_board("node", "example-node")  # both start unpowered
    sim.add_board("pkg", "example-package", power="off")
    assert renode == []  # nothing runs before start
    sim.start()
    (session,) = renode
    assert session.started and sim.session is session
    (model_set,) = session.settings["model_sets"]
    assert model_set == ModelSet("project", project, boards.find("example-node").models)
    assert session.settings["workdir"] == tmp_path / "run" / "renode"
    assert renode.finds == [(None, RenodeConfig(None, None))]
    assert session.machines == [("node", True), ("pkg", True)]
    assert "emulation SetGlobalSerialExecution true" in session.commands
    sim.close()
    assert session.stopped


def test_the_overlay_loads_the_soc_platform_first(
    project: Path, tmp_path: Path, renode: FakeRenode
) -> None:
    (project / "build" / "node.elf").write_bytes(b"\x7fELF")
    sim = built(tmp_path)
    machine = sim.add_board("node", "example-node")
    assert machine.platform_files() == [
        platforms.path("xilinx/zynq7000"),
        project / "boards" / "example-node" / "example-node.repl",
    ]
    sim.start()
    overlay = (tmp_path / "run" / "node" / "machine.repl").read_text(encoding="utf-8")
    first, second = overlay.splitlines()[:2]
    assert first == f'using "{platforms.path("xilinx/zynq7000").as_posix()}"'
    assert second.endswith('example-node/example-node.repl"')
    assert "cpu0:\n    PerformanceInMips: 100" in overlay


def test_a_zynq_board_without_a_hardware_design_boots_over_jtag(
    project: Path, tmp_path: Path, renode: FakeRenode
) -> None:
    elf = project / "build" / "node.elf"
    elf.write_bytes(b"\x7fELF")
    sim = built(tmp_path)
    sim.add_board("node", "example-node", power="on")
    sim.start()
    (session,) = renode
    assert "slcr BootMode 0" in session.commands  # straps: jtag
    boot = session.commands[session.commands.index("bootrom HoldCoreForDebugger true") :]
    assert [c for c in boot if not c.startswith("(select")] == [
        "bootrom HoldCoreForDebugger true",
        "bootrom PowerOn",
        f"sysbus LoadELF @{elf.as_posix()}",
        "bootrom HoldCoreForDebugger false",
        "cpu0 IsHalted false",
    ]
    assert sim.machines["node"].powered and sim.machines["node"].booted
    assert [(e.source, e.kind, e.detail) for e in sim.events] == [("node", "boot", "jtag")]


def test_a_zynq_board_with_a_hardware_design_replays_ps7_init(
    project: Path, tmp_path: Path, renode: FakeRenode
) -> None:
    (project / "build" / "app.elf").write_bytes(b"\x7fELF")
    sim = built(tmp_path)
    sim.add_board("dut", "example-zynq")
    sim.start()
    commands = renode[0].commands
    program = next(c for c in commands if c.startswith("machine RunRegisterProgram @"))
    bitstream = next(c for c in commands if c.startswith("devc ConfigureFromJtag @"))
    assert commands.index("bootrom PowerOn") < commands.index(program)
    assert commands.index(program) < commands.index(bitstream)
    assert commands.index(bitstream) < commands.index("cpu0 IsHalted false")


def test_power_states_drive_the_soc(project: Path, tmp_path: Path, renode: FakeRenode) -> None:
    (project / "build" / "node.elf").write_bytes(b"\x7fELF")
    sim = built(tmp_path)
    machine = sim.add_board("node", "example-node")  # power.initial: off
    sim.start()
    (session,) = renode
    assert not machine.powered and not machine.booted
    assert "bootrom PowerOff" in session.commands and "bootrom PowerOn" not in session.commands
    machine.power_off()  # already off: nothing happens
    session.commands.clear()
    machine.power_on()  # the first power-on runs the boot flow
    assert "bootrom HoldCoreForDebugger true" in session.commands
    assert machine.powered and machine.booted
    session.commands.clear()
    machine.power_off()
    machine.power_on()  # later ones restart through the power-on reset
    plain = [c for c in session.commands if not c.startswith("(select")]
    assert plain == [
        "bootrom PowerOff",
        "slcr OnGPIO 0 true",
        "slcr OnGPIO 0 false",
        "bootrom PowerOn",
    ]
    session.commands.clear()
    sim.run(machine.power_cycle(off=0.5))
    assert sim.now == 0.5
    assert [c for c in session.commands if c.startswith("bootrom")] == [
        "bootrom PowerOff",
        "bootrom PowerOn",
    ]
    kinds = [e.detail for e in sim.events if e.kind == "power"]
    assert kinds == ["off", "on", "off", "on", "off for 0.5 s", "on"]


def test_an_stm32_board_boots_its_elf_at_physical_addresses(
    tmp_path: Path, renode: FakeRenode
) -> None:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "build").mkdir()
    elf = root / "build" / "app.elf"
    elf.write_bytes(b"\x7fELF")
    directory = root / "boards" / "u5"
    directory.mkdir(parents=True)
    (directory / "u5.repl").write_text("", encoding="utf-8")
    (directory / "board.yaml").write_text(
        "schema: 2\nname: u5\nsoc: stm32\nplatform: u5.repl\n"
        "stm32: {firmware: {elf: build/app.elf}, cpu: cpu0, vector_table: 0x08004000}\n"
        "power: {initial: off}\n",
        encoding="utf-8",
    )
    sim = built(tmp_path)
    machine = sim.add_board("u5", boards.load(directory / "board.yaml"), boot="flash")
    sim.start()
    session = renode[0]
    assert "cpu0 IsHalted true" in session.commands  # starts unpowered
    session.commands.clear()
    machine.power_on()
    assert [c for c in session.commands if not c.startswith("(select")] == [
        f"sysbus LoadELF @{elf.as_posix()} false",
        "cpu0 InitVectorTableOffset 0x8004000",
        "cpu0 IsHalted false",
    ]
    session.commands.clear()
    machine.power_off()
    machine.power_on()
    assert [c for c in session.commands if not c.startswith("(select")] == [
        "cpu0 IsHalted true",
        "machine Reset",
        "cpu0 IsHalted false",
    ]
    assert Stm32Driver().overlay(machine.config) == "cpu0:\n    PerformanceInMips: 100\n"


def test_an_stm32_firmware_command_runs_once_in_the_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "make_elf.py").write_text(
        "import pathlib\n"
        "pathlib.Path('build').mkdir(exist_ok=True)\n"
        "pathlib.Path('build/app.elf').write_bytes(b'elf')\n"
        "with open('runs.txt', 'a') as log:\n"
        "    log.write('x')\n",
        encoding="utf-8",
    )
    directory = root / "boards" / "u5"
    directory.mkdir(parents=True)
    (directory / "u5.repl").write_text("", encoding="utf-8")
    python = Path(sys.executable).as_posix()
    (directory / "board.yaml").write_text(
        "schema: 2\nname: u5\nsoc: stm32\nplatform: u5.repl\n"
        f"stm32: {{firmware: {{command: ['{python}', make_elf.py], output: build/app.elf}}}}\n",
        encoding="utf-8",
    )
    board = boards.load(directory / "board.yaml")
    driver = Stm32Driver()
    from jig.system.socs import MachineConfig

    config = MachineConfig("u5", board, "flash", 100, tmp_path / "work")
    assert driver.firmware(config) == (root / "build" / "app.elf").resolve()
    assert driver.firmware(config) == (root / "build" / "app.elf").resolve()
    assert (root / "runs.txt").read_text() == "x"  # once per process
    failing = [sys.executable, "-c", "import sys; print('compile error'); sys.exit(3)"]
    broken = Stm32Driver()
    with pytest.raises(ToolchainError, match="failed \\(exit 3\\):\ncompile error"):
        broken._command(board, failing, "build/none.elf")


@pytest.mark.parametrize(
    ("firmware", "message"),
    [
        ("{elf: a.elf, command: [make], output: a.elf}", "gives one of elf, command or build, not"),
        ("{command: make, output: a.elf}", "command is a list of arguments"),
        ("{command: [make]}", "stm32.firmware.output is missing"),
        ("{elf: a.elf, output: a.elf}", "firmware.output does not go with elf"),
        ("{}", "gives one of elf, command or build"),
    ],
)
def test_stm32_firmware_forms_are_checked(tmp_path: Path, firmware: str, message: str) -> None:
    (tmp_path / "x.repl").write_text("", encoding="utf-8")
    (tmp_path / "board.yaml").write_text(
        f"schema: 2\nname: x\nsoc: stm32\nplatform: x.repl\nstm32: {{firmware: {firmware}}}\n",
        encoding="utf-8",
    )
    (problem,) = Stm32Driver().check(boards.load(tmp_path / "board.yaml"))
    assert message in problem


def test_plugin_hooks_reach_the_driver(tmp_path: Path, renode: FakeRenode) -> None:
    sim = built(tmp_path)
    machine = sim.add_board("node", "example-node")
    hooks = machine.config.hooks["zynq7000"]
    assert isinstance(hooks, ZynqHooks)
    assert machine.driver.images(machine.config) == {
        "app": FIXTURES / "build" / "node.elf",
    }
    with pytest.raises(ConfigError, match="gives no qspi_image"):
        machine.driver.qspi_image(machine.config)  # type: ignore[attr-defined]


def test_consoles_end_lines_as_the_interface_says(
    tmp_path: Path, renode: FakeRenode, project: Path
) -> None:
    (project / "build" / "node.elf").write_bytes(b"\x7fELF")
    sim = built(tmp_path)
    machine = sim.add_board("node", "example-node", power="on")
    sim.start()
    console = machine.console
    assert console.eol == "\r\n"
    console.send_line("help")
    assert renode[0].commands[-1] == 'node_uart1 Write "68656c700d0a"'
    assert machine.console_on("kiss").eol == "\r"


def test_bus_observers_come_from_the_registry(
    project: Path, tmp_path: Path, renode: FakeRenode
) -> None:
    from jig.ext.csp.observer import BusObserver

    (project / "build" / "node.elf").write_bytes(b"\x7fELF")
    sim = built(tmp_path)
    sim.add_board("node", "example-node")
    sim.add_can_bus("can0", members=[("node", "can")])
    sim.start()
    assert set(sim.bus_observers["can0"]) == {"csp"}
    assert isinstance(sim.bus_observers["can0"]["csp"], BusObserver)
    assert 'emulation CreateCanBus "can0" 1e+06' in renode[0].commands


def test_jig_check_contracts_builds_the_machine_without_booting(
    tmp_path: Path,
    renode: FakeRenode,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import types

    from jig import cli

    calls: list[tuple[object, str, str]] = []

    def verify(session: object, machine: str, board: boards.Board) -> list[str]:
        calls.append((session, machine, board.name))
        return [f"rtc: {board.rtc}: no IRealTimeClock"]

    stand_in = types.ModuleType("jig.contracts")
    stand_in.verify = verify  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jig.contracts", stand_in)
    monkeypatch.setenv("JIG_OUTPUT", str(tmp_path / "out"))
    assert cli.main(["-q", "check", "--contracts", "example-zynq"]) == 1
    (session,) = renode
    assert calls == [(session, "dut", "example-zynq")]
    assert not any(c.startswith("bootrom") for c in session.commands)  # built, not booted
    assert session.stopped
    assert renode.untested == [False]
    assert "rtc: rtc0: no IRealTimeClock" in capsys.readouterr().out
    assert cli.main(["-q", "check", "--contracts", "--allow-untested-renode", "example-zynq"]) == 1
    assert renode.untested == [False, True]
    monkeypatch.setitem(sys.modules, "jig.contracts", None)  # not installed
    assert cli.main(["-q", "check", "--contracts", "example-zynq"]) == 3


def test_jig_run_and_boot_pass_the_renode_options_to_discovery(
    project: Path, tmp_path: Path, renode: FakeRenode, capsys: pytest.CaptureFixture[str]
) -> None:
    from jig import cli

    (project / "build" / "node.elf").write_bytes(b"\x7fELF")  # checked, never loaded
    plan = tmp_path / "plan.jig"
    plan.write_text(
        'system { board node: "example-node" }\ntest "t" { check not node.powered }\n',
        encoding="utf-8",
        newline="\n",
    )
    explicit = tmp_path / "renode-1.18"
    assert cli.main(["-q", "run", str(plan), "--renode", str(explicit)]) == 0
    assert cli.main(["-q", "run", str(plan), "--allow-untested-renode"]) == 0
    assert cli.main(["-q", "boot", "example-node", "--seconds", "0.01"]) == 0
    assert (
        cli.main(["-q", "boot", "example-node", "--seconds", "0.01", "--allow-untested-renode"])
        == 0
    )
    assert [explicit for explicit, _config in renode.finds] == [explicit, None, None, None]
    assert renode.untested == [False, True, False, True]
    capsys.readouterr()


def test_reports_carry_the_renode_and_model_compile_times(
    project: Path, tmp_path: Path, renode: FakeRenode, capsys: pytest.CaptureFixture[str]
) -> None:
    import xml.etree.ElementTree as ET

    from jig import cli

    (project / "build" / "node.elf").write_bytes(b"\x7fELF")  # checked, never loaded
    plan = tmp_path / "plan.jig"
    plan.write_text(
        'system { board node: "example-node" }\ntest "t" { check not node.powered }\n',
        encoding="utf-8",
        newline="\n",
    )
    out = tmp_path / "reports"
    arguments = ["--json", str(out / "summary.json"), "--junit", str(out / "junit.xml")]
    assert cli.main(["-q", "run", str(plan), *arguments, "--html", str(out / "r.html")]) == 0
    capsys.readouterr()
    (run,) = json.loads((out / "summary.json").read_text(encoding="utf-8"))["plans"]
    install = renode[0].install
    assert run["renode"] == {
        "version": "1.17.0",
        "build": "test",
        "runtime": "net8",
        "status": install.status,
        "executable": str(install.executable),
        "startup_seconds": 4.25,
        "model_sets": [
            {"name": "jig", "files": 26, "seconds": 1.5},
            {"name": "project", "files": 1, "seconds": 0.25},
        ],
    }
    properties = {
        element.get("name"): element.get("value")
        for element in ET.parse(out / "junit.xml")
        .getroot()
        .findall("testsuite/properties/property")
    }
    assert {
        key: value for key, value in properties.items() if "renode" in key or "model" in key
    } == {
        "renode_version": "1.17.0",
        "renode_build": "test",
        "renode_runtime": "net8",
        "renode_startup_seconds": "4.250",
        "model_set.jig.compile_seconds": "1.500",
        "model_set.project.compile_seconds": "0.250",
    }
    page = (out / "r.html").read_text(encoding="utf-8")
    assert "Renode 1.17.0 (test, net8); models: jig 26 files 1.50 s, project 1 file 0.25 s" in page


# ------------------------------------------------- consoles from the start


def test_a_console_reads_what_its_port_recorded(tmp_path: Path) -> None:
    port = UartPort(RecordingSession(), "x_uart1", "x.console")  # type: ignore[arg-type]
    port.feed(b"banner\r\n", [7000] * 8)
    clock = SimpleNamespace(now_ns=9_000_000)
    # Made after the board printed, the console still holds what it printed.
    console = Console(clock, port, tmp_path / "console.log")  # type: ignore[arg-type]
    assert console.text == console.buffer == "banner\r\n"
    console.clear()
    port.feed(b"\xc0> ", [9000] * 3)
    assert console.buffer == "\xc0> "
    assert console.text == "banner\r\n\xc0> "
    assert console.search(r"> $") is not None and console.tail(2) == "> "
    console.close()
    assert (tmp_path / "console.log").read_bytes().decode() == "banner\r\n\xc0> "


def test_consoles_record_from_the_start_of_the_run(
    project: Path, monkeypatch: pytest.MonkeyPatch, renode: FakeRenode
) -> None:
    """Two boards whose banners come at different times, waited on in
    sequence: b prints at 7 ms, while the setup still waits for a's banner at
    30 ms and before anything reads b's console."""
    (project / "build" / "node.elf").write_bytes(b"\x7fELF")
    script = [
        (7_000_000, "b_uart1", b"b: banner\r\nready> "),
        (8_000_000, "b_uart0", b"\xc0\x00\x01\xc0"),  # a link no plan reads as text
        (30_000_000, "a_uart1", b"a: banner\r\nready> "),
    ]
    monkeypatch.setattr(
        system, "open_session", lambda **settings: TalkingSession(script, **settings)
    )
    plan = project / "banners.jig"
    plan.write_text(
        "system {\n"
        '    board a: "example-node" (power: "on")\n'
        '    board b: "example-node" (power: "on")\n'
        "}\n"
        "setup {\n"
        '    a.console.expect("a: banner", within: 1 s)\n'
        '    b.console.expect("b: banner", within: 1 s)\n'
        "}\n"
        'test "b\'s console holds its early output" {\n'
        '    check b.console.text == "b: banner\\r\\nready> "\n'
        '    check b.console.buffer contains "ready> "\n'
        "}\n",
        encoding="utf-8",
        newline="\n",
    )
    report = run_plan(plan)
    assert report.ok, report.aborted or [case.detail for case in report.all_cases()]
    assert report.setup is not None and report.setup.finished == pytest.approx(0.03)
    run = Path(report.run_directory)
    transcripts = {label: Path(path) for label, path in report.transcripts.items()}
    assert transcripts == {
        "a.console": run / "a" / "console.log",
        "a.kiss": run / "a" / "kiss.log",
        "b.console": run / "b" / "console.log",
        "b.kiss": run / "b" / "kiss.log",
    }
    text = {label: path.read_bytes().decode() for label, path in transcripts.items()}
    assert text == {
        "a.console": "a: banner\r\nready> ",
        "a.kiss": "",
        "b.console": "b: banner\r\nready> ",
        "b.kiss": "\xc0\x00\x01\xc0",
    }


# ---------------------------------------------- files checked before Renode


def test_missing_files_stop_the_run_before_renode_starts(
    project: Path, tmp_path: Path, renode: FakeRenode
) -> None:
    image = tmp_path / "flash.img"
    with built(tmp_path) as sim:
        sim.add_board("node", "example-node")  # starts unpowered: checked all the same
        sim.add_board("dut", "example-zynq", storage={"flash0": image})
        with pytest.raises(ConfigError) as caught:
            sim.start()
        assert renode == [] and renode.finds == []  # Renode was neither looked for nor started
        node_elf = (project / "build" / "node.elf").resolve()
        app_elf = (project / "build" / "app.elf").resolve()
        assert str(caught.value).splitlines() == [
            "3 files the boards need are missing:",
            f"    node: firmware app {node_elf} not found (build the firmware first)",
            f"    dut: image for flash0 {image} not found",
            f"    dut: firmware app {app_elf} not found (build the firmware first)",
        ]
        # Building without booting (jig check --contracts) needs no firmware.
        image.write_bytes(b"\xff" * 16)
        sim.start(boot=False)
        assert len(renode) == 1


def test_a_driver_can_name_more_files_a_boot_needs(
    project: Path, tmp_path: Path, renode: FakeRenode
) -> None:
    (project / "build" / "node.elf").write_bytes(b"\x7fELF")
    with built(tmp_path) as sim:
        machine = sim.add_board("node", "example-node")
        design = project / "design.bin"
        machine.driver.required_files = lambda config: {"design file": design}  # type: ignore[attr-defined]
        with pytest.raises(ConfigError, match=r"^node: design file .*design\.bin not found$"):
            sim.start()
        design.write_bytes(b"\0")
        sim.start()
        assert len(renode) == 1


def test_jig_run_names_the_missing_firmware_and_exits_2(
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jig import cli

    def no_renode(**settings: Any) -> RenodeInstall:
        raise AssertionError("Renode was looked for")

    monkeypatch.setattr(system, "find_renode", no_renode)
    plan = tmp_path / "plan.jig"
    plan.write_text(
        'system { board dut: "example-zynq" }\ntest "t" { check true }\n',
        encoding="utf-8",
        newline="\n",
    )
    assert cli.main(["-q", "run", str(plan)]) == 2
    elf = (project / "build" / "app.elf").resolve()
    out = capsys.readouterr().out
    assert f"ABORTED: dut: firmware app {elf} not found (build the firmware first)" in out


# ------------------------------------------------- the boot flow, again


def _jtag_board(project: Path, sim: Simulation) -> Machine:
    (project / "build" / "node.elf").write_bytes(b"\x7fELF")
    return sim.add_board("node", "example-node", power="on")


def _qspi_board(project: Path, sim: Simulation) -> Machine:
    (project / "build" / "app.elf").write_bytes(b"\x7fELF")
    image = project / "build" / "flash.img"
    image.write_bytes(b"\xff" * 64)
    return sim.add_board("dut", "example-zynq", boot="qspi", storage={"flash0": image})


def _flash_board(project: Path, sim: Simulation) -> Machine:
    (project / "build" / "u5.elf").write_bytes(b"\x7fELF")
    directory = project / "boards" / "u5"
    directory.mkdir()
    (directory / "u5.repl").write_text("", encoding="utf-8")
    (directory / "board.yaml").write_text(
        "schema: 2\nname: u5\nroot: ../..\nsoc: stm32\nplatform: u5.repl\n"
        "stm32: {firmware: {elf: build/u5.elf}, cpu: cpu0}\n",
        encoding="utf-8",
    )
    return sim.add_board("u5", boards.load(directory / "board.yaml"), boot="flash")


@pytest.mark.parametrize(
    "make", [_jtag_board, _qspi_board, _flash_board], ids=["jtag", "qspi", "flash"]
)
def test_boot_runs_the_boot_flow_again_in_every_boot_mode(
    make: Callable[[Path, Simulation], Machine],
    project: Path,
    tmp_path: Path,
    renode: FakeRenode,
) -> None:
    with built(tmp_path) as sim:
        machine = make(project, sim)
        sim.start()
        (session,) = renode
        sim.run(machine.power_cycle(off=0.5))
        flow = RecordingSession()
        machine.driver.boot(machine.config, flow)  # type: ignore[arg-type]
        session.commands.clear()
        seen = len(sim.events)
        machine.boot()
        assert session.commands == flow.commands  # the driver's boot flow, nothing else
        assert machine.booted
        assert [(e.time_ns, e.source, e.kind, e.detail) for e in sim.events[seen:]] == [
            (500_000_000, machine.name, "boot", machine.config.boot)
        ]
        machine.power_off()
        with pytest.raises(ConfigError, match="is powered off: power it on before booting it"):
            machine.boot()


def test_boot_replays_the_debugger_on_a_jtag_board(
    project: Path, tmp_path: Path, renode: FakeRenode
) -> None:
    with built(tmp_path) as sim:
        machine = _jtag_board(project, sim)
        sim.start()
        sim.run(machine.power_cycle(off=0.5))
        session = renode[0]
        session.commands.clear()
        machine.boot()
        elf = (project / "build" / "node.elf").resolve()
        assert [c for c in session.commands if not c.startswith("(select")] == [
            "bootrom HoldCoreForDebugger true",
            "bootrom PowerOn",
            f"sysbus LoadELF @{elf.as_posix()}",
            "bootrom HoldCoreForDebugger false",
            "cpu0 IsHalted false",
        ]


def test_the_soc_drivers_name_the_stm32_elf_and_the_zynq_xsa(
    project: Path, tmp_path: Path, renode: FakeRenode
) -> None:
    with built(tmp_path) as sim:
        sim.add_board("pkg", "example-package")
        sim.add_board("dut", "example-zynq")
        files = {
            what: path
            for machine in sim.machines.values()
            for what, path in machine.required_files(boot=True)
        }
        assert files["stm32.firmware.elf"] == (project / "build" / "package.elf").resolve()
        assert files["zynq7000.xsa"] == (project / "build" / "example.xsa").resolve()
