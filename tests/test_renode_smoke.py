"""Renode smoke test: a session compiles Jig's models in Renode, builds a
Zynq-7000 machine from the packaged platform and runs it for 1 ms.

Needs Renode (``JIG_RENODE``, a managed install or one discovery finds); no
.NET SDK. Run it with ``pytest -m renode``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jig import project
from jig.renode import models
from jig.renode.session import RenodeSession

from .renode_support import SMALLEST_ZYNQ_OVERLAY, real_renode

pytestmark = pytest.mark.renode


def test_a_zynq7000_machine_runs_for_one_millisecond(tmp_path: Path) -> None:
    renode = real_renode()
    overlay = tmp_path / "machine.repl"
    platform = project.platform_file("xilinx/zynq7000")
    overlay.write_text(
        f'using "{platform.as_posix()}"\n\n{SMALLEST_ZYNQ_OVERLAY}', encoding="utf-8", newline="\n"
    )
    with RenodeSession(
        workdir=tmp_path / "renode", install=renode, step_wall_limit=120.0
    ) as session:
        record = json.loads((session.workdir / "session.json").read_text(encoding="utf-8"))
        assert record["model_sets"][0]["name"] == models.JIG_SET
        assert record["model_sets"][0]["files"] == len(models.jig_model_set().files)
        session.create_machine("zynq")
        session.execute(f"machine LoadPlatformDescription @{overlay.as_posix()}")
        # The BOOT_MODE straps default to JTAG: the BootROM model leaves the
        # core halted for a debugger, so the machine runs without firmware.
        session.execute("bootrom PowerOn")
        assert session.execute("bootrom State").strip().splitlines()[0] == "WaitingForDebugger"
        session.run_for(0.001)
        assert session.elapsed_ns == 1_000_000
        reported = session.execute("emulation ElapsedNanoseconds").strip().splitlines()[0]
        assert int(reported, 0) == 1_000_000
