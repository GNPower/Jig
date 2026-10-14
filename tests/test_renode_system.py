"""A simulation on Renode without running firmware: the board's model set
compiles into the session, the SoC platform loads before the board's
description, a CAN bus gets its observer, and a board that starts unpowered
stays so.

Needs Renode (``JIG_RENODE`` or a managed install) and the .NET 8 SDK; run it
with ``pytest -m renode``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from jig.errors import ToolchainError
from jig.ext.csp.observer import BusObserver
from jig.renode import models, toolchain
from jig.system.system import Simulation

pytestmark = pytest.mark.renode


@pytest.fixture
def renode() -> Path:
    try:
        executable = toolchain.renode_executable()
    except ToolchainError as exc:
        pytest.skip(f"no Renode: {exc}")
    if shutil.which("dotnet") is None or models.checkout_root() is None:
        pytest.skip("building the C# models needs the .NET SDK and a Jig checkout")
    return executable


def test_an_unpowered_board_with_its_models_on_renode(
    renode: Path, project: Path, tmp_path: Path
) -> None:
    # Its firmware has to exist before Renode starts, though the board never runs it.
    (project / "build" / "node.elf").write_bytes(b"\x7fELF")
    with Simulation(name="system", workdir=tmp_path / "run", step_wall_limit=120.0) as sim:
        node = sim.add_board("node", "example-node")  # power.initial: off
        sim.add_can_bus("can0", members=[("node", "can")])
        sim.start()
        sim.run_for(0.01)
        assert not node.powered and not node.booted
        assert node.execute("bootrom State").strip().splitlines()[0] == "Unpowered"
        # The board's model set compiled into the session: its type resolves.
        node.execute(
            "machine LoadPlatformDescriptionFromString "
            '"block: Example.ExampleBlock @ sysbus <0x43C00000, +0x100>"'
        )
        assert int(node.execute("sysbus ReadDoubleWord 0x43C00000").strip(), 0) == 0
        assert isinstance(sim.bus_observers["can0"]["csp"], BusObserver)
        assert sim.now == pytest.approx(0.01)
