"""``jig cosim build``: the generated Renode entry point, the IntegrationLibrary fix, and the
public example (``tests/fixtures/cosim/axil_regs``) built with Verilator and driven from a
Zynq-7000 machine."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path

import pytest

from jig import project
from jig.errors import ConfigError, ToolchainError
from jig.ext import cosim
from jig.ext.cosim import AxiLitePort, CosimBuild
from jig.renode import commands
from jig.renode.session import RenodeSession

from .conftest import FIXTURES
from .renode_support import SMALLEST_ZYNQ_OVERLAY, real_renode

EXAMPLE = FIXTURES / "cosim" / "axil_regs"


def test_the_entry_point_binds_every_signal_and_interrupt(tmp_path: Path) -> None:
    spec = CosimBuild(
        top="my_top",
        sources=tmp_path / "s.f",
        output=tmp_path,
        interrupts=("irq_a", "irq_b"),
        port=AxiLitePort(prefix="s_axi_", clock="aclk", reset="aresetn"),
    )
    text = cosim.render_sim_main(spec)
    assert '#include "Vmy_top.h"' in text and "new Vmy_top{context}" in text
    assert "    bus->clk = &top->aclk;" in text
    assert "    bus->rst = &top->aresetn;" in text
    assert "    bus->rready = &top->s_axi_rready;" in text
    assert text.count("    bus->") == 22  # 21 signals and the evaluate hook
    assert "agent->registerInterrupt(&top->irq_a, 0);" in text
    assert "agent->registerInterrupt(&top->irq_b, 1);" in text
    assert "@" not in text.replace("@TOP@", "")
    with pytest.raises(ConfigError, match="not a C"):
        cosim.render_sim_main(CosimBuild(top="bad top", sources=tmp_path, output=tmp_path))


def test_the_integration_library_copy_is_fixed_once(tmp_path: Path) -> None:
    source = tmp_path / "src" / "renode_bus.cpp"
    source.parent.mkdir(parents=True)
    source.write_text(
        "void RenodeAgent::connect(int receiverPort, int senderPort, const char* address)\n"
        "{\n    channel->connect(receiverPort, senderPort, address);\n}\n",
        encoding="utf-8",
    )
    assert cosim.patch_integration_library(tmp_path)
    fixed = source.read_text(encoding="utf-8")
    assert "SocketConnectionArgs args{receiverPort, senderPort, address, 0};" in fixed
    assert "channel->connect(&args);" in fixed
    assert not cosim.patch_integration_library(tmp_path)


def test_every_template_is_package_data() -> None:
    names = sorted(p.name for p in cosim.TEMPLATES.iterdir())
    assert names == [
        "jig_axil_bram.sv",
        "jig_axil_byte_lanes.sv",
        "jig_axil_decoder.sv",
        "jig_axil_regs.sv",
        "jig_dp_bram.sv",
        "sim_main.cpp.in",
    ]
    assert project.package_file("ext", "cosim", "templates") == cosim.TEMPLATES


@pytest.mark.skipif(platform.system() == "Linux", reason="the refusal on other hosts")
def test_building_needs_linux(tmp_path: Path) -> None:
    spec = CosimBuild(top="axil_regs_top", sources=EXAMPLE / "sources.f", output=tmp_path)
    with pytest.raises(ToolchainError, match="builds on Linux"):
        cosim.build_library(spec)


MACHINE = """\
regs: CoSimulated.CoSimulatedPeripheral @ sysbus <0x43C00000, +0x3000>
    useAbsoluteAddress: true
    frequency: 10000000
    limitBuffer: 10000
    timeout: 10000
"""


@pytest.mark.renode
@pytest.mark.skipif(
    platform.system() != "Linux" or shutil.which("verilator") is None,
    reason="co-simulation builds on Linux with Verilator",
)
def test_the_example_block_runs_on_a_zynq7000_machine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    renode = real_renode()
    out = tmp_path / "build"
    code = commands.main(
        ["cosim", "build", str(out), "--top", "axil_regs_top",
         "--sources", str(EXAMPLE / "sources.f"), "--clock-mhz", "10", "--irq", "irq",
         "--renode", str(renode.executable)]
    )  # fmt: skip
    assert code == 0, (out / "build.log").read_text(encoding="utf-8")[-3000:]
    library = Path(capsys.readouterr().out.strip())
    assert library == out / "libaxil_regs_top.so" and library.is_file()

    overlay = tmp_path / "machine.repl"
    zynq = project.platform_file("xilinx/zynq7000").as_posix()
    overlay.write_text(f'using "{zynq}"\n\n{SMALLEST_ZYNQ_OVERLAY}\n{MACHINE}', encoding="utf-8")
    with RenodeSession(workdir=tmp_path / "renode", install=renode, step_wall_limit=120.0) as s:
        s.create_machine("zynq")
        s.execute(f"machine LoadPlatformDescription @{overlay.as_posix()}")
        s.execute(f"regs SimulationFilePathLinux @{library.as_posix()}")
        s.execute("bootrom PowerOn")  # JTAG straps: the CPU stays halted

        def read(address: int, width: str = "DoubleWord") -> int:
            return int(s.execute(f"sysbus Read{width} {address:#x}").strip(), 0)

        def write(address: int, value: int, width: str = "DoubleWord") -> None:
            s.execute(f"sysbus Write{width} {address:#x} {value:#x}")

        assert read(0x43C00000) == 0x4A494701  # ID
        assert read(0x43C00018) == 10  # CLOCK_MHZ, from --clock-mhz
        write(0x43C00004, 0xCAFEF00D)
        assert read(0x43C00004) == 0xCAFEF00D
        write(0x43C00005, 0x42, "Byte")  # the byte-lane adapter moves it to bits 15:8
        assert read(0x43C00004) == 0xCAFE420D
        assert read(0x43C00006, "Word") == 0xCAFE
        assert read(0x43C00007, "Byte") == 0xCA
        write(0x43C0000C, 1)  # CONTROL: interrupt enable
        write(0x43C00014, 1)  # TRIGGER
        assert read(0x43C00010) == 1  # STATUS: pending
        write(0x43C00010, 1)
        assert read(0x43C00010) == 0
        write(0x43C01008, 0x12345678)  # jig_axil_regs
        assert read(0x43C01008) == 0x12345678 and read(0x43C0100C) == 0
        write(0x43C02010, 0xA5A55A5A)  # jig_axil_bram over jig_dp_bram
        write(0x43C02012, 0xBEEF, "Word")
        assert read(0x43C02010) == 0xBEEF5A5A
        before = read(0x43C00008)  # COUNTER: model clocks
        s.run_for(0.005)
        ticks = read(0x43C00008) - before
        assert 40_000 <= ticks <= 60_000, ticks  # 10 MHz for 5 ms, in 1 ms batches
