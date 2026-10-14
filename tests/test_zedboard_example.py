"""The ZedBoard example on Renode, through Jig's Python API: a console the
code reads late still holds the boot banner, and ``boot()`` brings a board
booted over JTAG back after a power cycle.

Needs Renode and the example's ELFs (``python examples/zedboard/firmware/build.py``);
run it with ``pytest -m "renode and firmware"``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jig.system.system import Simulation

from .renode_support import real_renode

pytestmark = [pytest.mark.renode, pytest.mark.firmware]

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "zedboard"


@pytest.fixture
def example(monkeypatch: pytest.MonkeyPatch) -> Path:
    """The example as the project, its firmware built and a Renode found."""
    built = [EXAMPLE / "firmware" / "build" / f"zed-node-{node}.elf" for node in (1, 2)]
    if not all(elf.is_file() for elf in built):
        pytest.skip("the example's ELFs are not built (examples/zedboard/firmware/build.py)")
    monkeypatch.setenv("JIG_PROJECT", str(EXAMPLE))
    real_renode()
    return EXAMPLE


def test_a_console_read_late_holds_the_boot_banner(example: Path, tmp_path: Path) -> None:
    """Both boards print their banner 7 ms after they start; the code reads
    zed-b's console only once zed-a's prompt has come, 30 ms in."""
    with Simulation(name="banners", workdir=tmp_path / "run", step_wall_limit=120.0) as sim:
        a = sim.add_board("a", "zed-a")
        b = sim.add_board("b", "zed-b")
        sim.add_can_bus("can0", members=[("a", "can"), ("b", "can")])
        sim.start()
        sim.run(a.console.expect(r"zed-a: CSP address 1 ", timeout=2.0))
        sim.run(a.console.expect("zed> ", timeout=2.0))
        waited = sim.now_ns
        sim.run(b.console.expect(r"zed-b: CSP address 2 ", timeout=2.0))
        sim.run(b.console.expect("zed> ", timeout=2.0))
        assert sim.now_ns == waited  # both had come while the code waited for zed-a
    transcript = (tmp_path / "run" / "b" / "console.log").read_bytes().decode()
    assert transcript.startswith("\r\nzed-b: CSP address 2 on a ZedBoard")


def test_boot_brings_a_jtag_board_back_after_a_power_cycle(example: Path, tmp_path: Path) -> None:
    with Simulation(name="reboot", workdir=tmp_path / "run", step_wall_limit=120.0) as sim:
        b = sim.add_board("b", "zed-b")
        sim.add_can_bus("can0", members=[("b", "can")])
        sim.start()
        sim.run(b.console.expect("zed> ", timeout=2.0))
        b.console.clear()
        sim.run(b.power_cycle(off=0.1))
        sim.run_for(0.2)
        # As on the hardware, the board waits in its BootROM for a debugger.
        assert b.console.buffer == ""
        assert b.execute("bootrom State").split()[0] == "WaitingForDebugger"
        b.boot()
        sim.run(b.console.expect(r"zed-b: CSP address 2 ", timeout=0.1))
        sim.run(b.console.expect("zed> ", timeout=0.1))
        found = re.search(r"uptime (\d+) ms", sim.run(b.console.run("uptime", settle=0.3)))
        assert found is not None and int(found.group(1)) < 500
    assert [(e.source, e.kind, e.detail) for e in sim.events] == [
        ("b", "boot", "jtag"),
        ("b", "power", "off for 0.1 s"),
        ("b", "power", "on"),
        ("b", "boot", "jtag"),
    ]
