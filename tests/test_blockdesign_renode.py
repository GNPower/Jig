"""The block-design generator's platform text loaded in Renode on top of Jig's
``xilinx/zynq7000`` platform: the models sit where the design puts them, the
FCLK resets reach the AXI GPIO inputs, interrupts reach the GIC lines of their
``IRQ_F2P`` IDs, and the CDMA reaches only what its own map lists.

Needs Renode; run it with ``pytest -m renode``."""

from __future__ import annotations

from pathlib import Path

import pytest

from jig import project
from jig.ext.zynq7000 import blockdesign as bd
from jig.renode.session import RenodeSession

from .renode_support import SMALLEST_ZYNQ_OVERLAY, real_renode
from .test_blockdesign import FCLK, SCOPED, SYSDEF, handoff, write_xsa

pytestmark = pytest.mark.renode

SLCR_UNLOCK = 0xF8000008
FPGA_RST_CTRL = 0xF8000240
GICD_ISPENDR = 0xF8F01200
GPIO = 0x41200000  # axi_gpio_0, on IRQ_F2P[9] (ID 85)
QUAD_SPI = 0x41E00000
TIMER = 0x42800000  # axi_timer_0: no model, a silent tag
BLOCK = 0x43C10000  # an AXI GPIO model an ip line places by address, on IRQ_F2P[10] (ID 86)
CDMA = 0x7E200000
BRAM = 0x80000000  # 8 KiB in the design, 16 KiB by an ip line

# AXI GPIO (PG144) and AXI CDMA (PG034) registers.
GPIO_TRI, GPIO2_DATA, GPIO2_TRI = 0x4, 0x8, 0xC
GPIO_GIER, GPIO_ISR, GPIO_IER = 0x11C, 0x120, 0x128
CDMA_SR, CDMA_SA, CDMA_DA, CDMA_BTT = 0x04, 0x18, 0x20, 0x28
CDMA_IOC, CDMA_DECODE_ERROR = 1 << 12, 1 << 6


def test_the_generated_platform_runs_in_renode(tmp_path: Path) -> None:
    renode = real_renode()
    xsa = write_xsa(
        tmp_path / "example.xsa",
        {
            "sysdef.xml": SYSDEF,
            "example_bd.hwh": handoff(),
            "example_bd_smartconnect_0_0.hwh": SCOPED,
        },
    )
    overrides = [
        bd.IpOverride("axi_bram_ctrl_0", bd.Memory(0x4000)),
        bd.IpOverride(
            "block", "Jig.Xilinx.XilinxAxiGpio", on=bd.AddressRange(BLOCK, BLOCK + 0xFFF), irq=10
        ),
        bd.IpOverride(
            "filter",
            bd.Cosim(),
            {"frequency": 100_000_000},
            on={"user_axi": 0x1000},
            irq="fabric_irq",
        ),
    ]
    platform = project.platform_file("xilinx/zynq7000")
    machine = tmp_path / "machine.repl"
    machine.write_text(
        f'using "{platform.as_posix()}"\n\n{SMALLEST_ZYNQ_OVERLAY}\n'
        + bd.pl_section(xsa, FCLK, overrides),
        encoding="utf-8",
        newline="\n",
    )
    with RenodeSession(
        workdir=tmp_path / "renode", install=renode, step_wall_limit=120.0
    ) as session:
        session.create_machine("zynq")
        session.execute(f"machine LoadPlatformDescription @{machine.as_posix()}")
        session.execute("cpu0 IsHalted true")

        def read(address: int) -> int:
            return int(session.execute(f"sysbus ReadDoubleWord {address:#x}").strip(), 0)

        def write(address: int, value: int) -> None:
            session.execute(f"sysbus WriteDoubleWord {address:#x} {value:#x}")

        def pending(interrupt_id: int) -> bool:
            # The distributor answers a CPU only.
            register = GICD_ISPENDR + 4 * (interrupt_id // 32)
            value = int(session.execute(f"sysbus ReadDoubleWord {register:#x} cpu0").strip(), 0)
            return bool(value >> (interrupt_id % 32) & 1)

        # Release FCLK_RESET0_N-3_N: a GP0 slave stalls the CPU while its reset holds it.
        write(SLCR_UNLOCK, 0xDF0D)
        write(FPGA_RST_CTRL, 0)

        # The GPIO's power-on state comes from the design's parameters, and its
        # inputs from the FCLK resets (directly and through the reset blocks)
        # and the constant: [0] FCLK_RESET1_N, [1] FCLK_RESET1_N, [2] 1, [3] FCLK_RESET0_N.
        assert (read(GPIO + GPIO_TRI), read(GPIO + GPIO2_DATA), read(GPIO + GPIO2_TRI)) == (
            0xFFFFFFFF,
            0xA5,
            0,
        )
        assert read(GPIO) == 0b1111
        write(FPGA_RST_CTRL, 0b10)  # FCLK_RESET1_N asserted
        assert read(GPIO) == 0b1100
        write(FPGA_RST_CTRL, 0)

        # IRQ_F2P[9] is ID 85: the GPIO interrupts when an input changes.
        write(GPIO + GPIO_ISR, read(GPIO + GPIO_ISR))  # toggle-on-write: clears it
        write(GPIO + GPIO_GIER, 0x80000000)
        write(GPIO + GPIO_IER, 0b1)
        assert not pending(85)
        write(FPGA_RST_CTRL, 0b1)  # input 3 falls with FCLK_RESET0_N
        assert pending(85)
        write(FPGA_RST_CTRL, 0)

        # IRQ_F2P[10] is ID 86: the block an ip line placed by address and line.
        write(BLOCK + GPIO_GIER, 0x80000000)
        write(BLOCK + GPIO_IER, 0b1)
        assert not pending(86)
        session.execute("block OnGPIO 0 true")
        assert pending(86)

        assert read(QUAD_SPI + 0x60) == 0x180  # SPICR's reset value (PG153)
        assert read(TIMER) == 0
        write(BRAM + 0x3FFC, 0x600DF00D)  # the ip line's 16 KiB
        assert read(BRAM + 0x3FFC) == 0x600DF00D

        # The CDMA copies within its map (the BRAM) and fails with a decode
        # error outside it (the block).
        write(BRAM, 0x11223344)
        write(CDMA + CDMA_SA, BRAM)
        write(CDMA + CDMA_DA, BRAM + 0x3000)
        write(CDMA + CDMA_BTT, 4)
        session.run_for(0.0001)
        assert read(CDMA + CDMA_SR) & CDMA_IOC
        assert read(BRAM + 0x3000) == 0x11223344
        write(CDMA + CDMA_SR, CDMA_IOC)
        write(CDMA + CDMA_DA, BLOCK)
        write(CDMA + CDMA_BTT, 4)
        session.run_for(0.0001)
        assert read(CDMA + CDMA_SR) & CDMA_DECODE_ERROR
