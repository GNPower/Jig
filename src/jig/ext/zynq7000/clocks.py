"""Zynq-7000 PS clock tree from SLCR register values.

The clock generator (UG585 chapter 25) derives every PS clock from PS_CLK
through the ARM, DDR & IO PLLs and per-clock dividers. Replaying the register
program that ``ps7_init`` applies gives the frequencies a board runs at, so a
board definition can be checked against its XSA instead of trusting
hand-copied numbers.

Fields used (SLCR offsets):

* ``*_PLL_CTRL`` 0x100/0x104/0x108: PLL_FDIV [18:12], PLL_BYPASS_FORCE [4],
  PLL_PWRDWN [1], PLL_RESET [0].
* ``ARM_CLK_CTRL`` 0x120: SRCSEL [5:4] (0x ARM PLL, 10 DDR PLL, 11 IO PLL),
  DIVISOR [13:8].
* ``CLK_621_TRUE`` 0x1C4 [0]: 1 selects the 6:2:1 CPU clock ratio, 0 4:2:1.
* Peripheral clocks (``LQSPI`` 0x14C, ``UART`` 0x154, ``SPI`` 0x158, ``CAN``
  0x15C, ``PCAP`` 0x168, ``FPGAn`` 0x170 + 0x10*n): SRCSEL [5:4] (0x IO PLL,
  10 ARM PLL, 11 DDR PLL), DIVISOR0 [13:8], DIVISOR1 [25:20] where present.
* ``WDT_CLK_SEL`` 0x304 [0]: 0 = CPU_1x clocks the SWDT.

A field the program never writes keeps its power-on value where one is known
(the PLL, ARM_CLK_CTRL, UART_CLK_CTRL & CLK_621_TRUE values the C# SLCR model
also uses); any other unwritten field a frequency depends on is an error.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from jig.errors import ConfigError
from jig.ext.zynq7000.ps7init import Operation

SLCR_BASE = 0xF8000000

ARM_PLL_CTRL = 0x100
DDR_PLL_CTRL = 0x104
IO_PLL_CTRL = 0x108
ARM_CLK_CTRL = 0x120
LQSPI_CLK_CTRL = 0x14C
UART_CLK_CTRL = 0x154
SPI_CLK_CTRL = 0x158
CAN_CLK_CTRL = 0x15C
PCAP_CLK_CTRL = 0x168
FPGA_CLK_CTRL = (0x170, 0x180, 0x190, 0x1A0)
CLK_621_TRUE = 0x1C4


# Power-on values, as the C# Zynq7000_SLCR model's PowerOnValues.
POWER_ON_VALUES = {
    ARM_PLL_CTRL: 0x0001A008,
    DDR_PLL_CTRL: 0x0001A008,
    IO_PLL_CTRL: 0x0001A008,
    ARM_CLK_CTRL: 0x1F000400,
    UART_CLK_CTRL: 0x00003F03,
    CLK_621_TRUE: 0x00000001,
}


class SlcrState:
    """SLCR register values with a record of which bits are known: written by a
    program, or holding a known power-on value."""

    def __init__(self) -> None:
        self.values: dict[int, int] = dict(POWER_ON_VALUES)
        self.written: dict[int, int] = {offset: 0xFFFFFFFF for offset in POWER_ON_VALUES}

    def apply(self, operations: Iterable[Operation]) -> SlcrState:
        for op in operations:
            if not SLCR_BASE <= op.address < SLCR_BASE + 0x1000:
                continue
            offset = op.address - SLCR_BASE
            if op.kind == "W":
                self.values[offset] = op.value
                self.written[offset] = 0xFFFFFFFF
            elif op.kind == "M":
                old = self.values.get(offset, 0)
                self.values[offset] = (old & ~op.mask) | (op.value & op.mask)
                self.written[offset] = self.written.get(offset, 0) | op.mask
        return self

    def known(self, offset: int, lsb: int, width: int) -> bool:
        mask = ((1 << width) - 1) << lsb
        return self.written.get(offset, 0) & mask == mask

    def field(self, offset: int, lsb: int, width: int) -> int:
        mask = ((1 << width) - 1) << lsb
        if not self.known(offset, lsb, width):
            raise ConfigError(
                f"SLCR 0x{offset:03X}[{lsb + width - 1}:{lsb}] is never written "
                "by the init program "
                "and has no known power-on value"
            )
        return (self.values[offset] & mask) >> lsb


@dataclass(frozen=True)
class ZynqClocks:
    """PS clock frequencies in Hz."""

    ps_clk: float
    arm_pll: float
    ddr_pll: float
    io_pll: float
    cpu_6x4x: float
    cpu_3x2x: float
    cpu_2x: float
    cpu_1x: float
    lqspi_ref: float | None
    uart_ref: float | None
    spi_ref: float | None
    can_ref: float | None
    pcap: float | None
    fclk: tuple[float | None, float | None, float | None, float | None]

    @staticmethod
    def from_state(state: SlcrState, ps_clk: float) -> ZynqClocks:
        def pll(offset: int) -> float:
            if state.field(offset, 4, 1):  # PLL_BYPASS_FORCE
                return ps_clk
            if state.field(offset, 0, 1) or state.field(offset, 1, 1):
                raise ConfigError(f"PLL at SLCR 0x{offset:03X} is left in reset or powered down")
            return ps_clk * state.field(offset, 12, 7)

        arm, ddr, io = pll(ARM_PLL_CTRL), pll(DDR_PLL_CTRL), pll(IO_PLL_CTRL)

        def divided(source: float, divisor: int, name: str) -> float:
            if divisor == 0:
                raise ConfigError(f"{name}: divisor is 0")
            return source / divisor

        cpu_source = {0: arm, 1: arm, 2: ddr, 3: io}[state.field(ARM_CLK_CTRL, 4, 2)]
        cpu_6x4x = divided(cpu_source, state.field(ARM_CLK_CTRL, 8, 6), "ARM_CLK_CTRL")
        six_two_one = state.field(CLK_621_TRUE, 0, 1) == 1
        cpu_2x = cpu_6x4x / (3 if six_two_one else 2)
        cpu_1x = cpu_6x4x / (6 if six_two_one else 4)

        def peripheral(offset: int, name: str, two_stage: bool = False) -> float | None:
            """None when the program leaves this clock unconfigured."""
            if not state.known(offset, 4, 2):
                return None
            source = {0: io, 1: io, 2: arm, 3: ddr}[state.field(offset, 4, 2)]
            frequency = divided(source, state.field(offset, 8, 6), name)
            if two_stage:
                frequency = divided(frequency, state.field(offset, 20, 6), name)
            return frequency

        fclk = tuple(
            peripheral(offset, f"FPGA{index}_CLK_CTRL", two_stage=True)
            for index, offset in enumerate(FPGA_CLK_CTRL)
        )
        return ZynqClocks(
            ps_clk=ps_clk,
            arm_pll=arm,
            ddr_pll=ddr,
            io_pll=io,
            cpu_6x4x=cpu_6x4x,
            cpu_3x2x=cpu_6x4x / 2,
            cpu_2x=cpu_2x,
            cpu_1x=cpu_1x,
            lqspi_ref=peripheral(LQSPI_CLK_CTRL, "LQSPI_CLK_CTRL"),
            uart_ref=peripheral(UART_CLK_CTRL, "UART_CLK_CTRL"),
            spi_ref=peripheral(SPI_CLK_CTRL, "SPI_CLK_CTRL"),
            can_ref=peripheral(CAN_CLK_CTRL, "CAN_CLK_CTRL", two_stage=True),
            pcap=peripheral(PCAP_CLK_CTRL, "PCAP_CLK_CTRL"),
            fclk=(fclk[0], fclk[1], fclk[2], fclk[3]),
        )

    @staticmethod
    def from_operations(operations: Iterable[Operation], ps_clk: float) -> ZynqClocks:
        return ZynqClocks.from_state(SlcrState().apply(operations), ps_clk)
