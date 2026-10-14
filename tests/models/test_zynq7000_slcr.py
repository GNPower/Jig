"""Zynq-7000 SLCR model (Jig.Xilinx.Zynq7000_SLCR): write protection, the PLL
and clock power-on values, PLL status, reset causes in REBOOT_STATUS, the
boot-mode straps, the FCLK resets and the peripheral software resets. Register
values are UG585 v1.7 appendix B.28's."""

from __future__ import annotations

import pytest

from jig import project

from .bench import Bench

pytestmark = pytest.mark.renode

SLCR = 0xF8000000
LOCK = SLCR + 0x004
UNLOCK = SLCR + 0x008
LOCKSTA = SLCR + 0x00C
ARM_PLL_CTRL = SLCR + 0x100
DDR_PLL_CTRL = SLCR + 0x104
IO_PLL_CTRL = SLCR + 0x108
PLL_STATUS = SLCR + 0x10C
ARM_CLK_CTRL = SLCR + 0x120
PSS_RST_CTRL = SLCR + 0x200
SPI_RST_CTRL = SLCR + 0x21C
CAN_RST_CTRL = SLCR + 0x220
UART_RST_CTRL = SLCR + 0x228
GPIO_RST_CTRL = SLCR + 0x22C
FPGA_RST_CTRL = SLCR + 0x240
REBOOT_STATUS = SLCR + 0x258
BOOT_MODE = SLCR + 0x25C
DDRIOB_DCI_CTRL = SLCR + 0xB70
DDRIOB_DCI_STATUS = SLCR + 0xB74

UNLOCK_KEY = 0xDF0D
LOCK_KEY = 0x767B

# REBOOT_STATUS cause bits (UG585 B.28).
SWDT_RST = 1 << 16
AWDT0_RST = 1 << 17
SLC_RST = 1 << 19
SRST_B = 1 << 21
POR = 1 << 22

MACHINE = """\
slcr: Jig.Xilinx.Zynq7000_SLCR @ sysbus <0xF8000000, +0x1000>
    bootMode: 1
    [0-3] -> probe@[0-3]

probe: JigFixtures.GpioProbe @ sysbus
"""


@pytest.fixture
def slcr(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def unlock(bench: Bench) -> None:
    bench.write(UNLOCK, UNLOCK_KEY)


def test_power_on_values_are_ug585s(slcr: Bench) -> None:
    expected = {
        ARM_PLL_CTRL: 0x0001A008,
        DDR_PLL_CTRL: 0x0001A008,
        IO_PLL_CTRL: 0x0001A008,
        PLL_STATUS: 0x0000003F,
        ARM_CLK_CTRL: 0x1F000400,
        SLCR + 0x124: 0x18400003,  # DDR_CLK_CTRL
        SLCR + 0x12C: 0x01FFCCCD,  # APER_CLK_CTRL
        SLCR + 0x140: 0x00003C01,  # GEM0_CLK_CTRL
        SLCR + 0x150: 0x00001E03,  # SDIO_CLK_CTRL
        SLCR + 0x154: 0x00003F03,  # UART_CLK_CTRL
        SLCR + 0x1C4: 0x00000001,  # CLK_621_TRUE
        PSS_RST_CTRL: 0x00000000,
        FPGA_RST_CTRL: 0x01F33F0F,  # B.28's value; not yet checked on hardware
        REBOOT_STATUS: POR,
        LOCKSTA: 0x00000001,
        SLCR + 0x910: 0x00000000,  # OCM_CFG
    }
    assert {address: slcr.read(address) for address in expected} == expected


def test_writes_need_the_unlock_key(slcr: Bench) -> None:
    slcr.write(ARM_CLK_CTRL, 0x1F000200)
    assert slcr.read(ARM_CLK_CTRL) == 0x1F000400  # locked: ignored
    slcr.write(UNLOCK, 0x1234)
    assert slcr.read(LOCKSTA) == 1
    unlock(slcr)
    assert slcr.read(LOCKSTA) == 0
    slcr.write(ARM_CLK_CTRL, 0x1F000200)
    assert slcr.read(ARM_CLK_CTRL) == 0x1F000200
    slcr.write(LOCK, LOCK_KEY)
    assert slcr.read(LOCKSTA) == 1
    slcr.write(ARM_CLK_CTRL, 0x1F000400)
    assert slcr.read(ARM_CLK_CTRL) == 0x1F000200
    assert slcr.read(LOCK) == 0 and slcr.read(UNLOCK) == 0


def test_pll_status_follows_reset_and_power_down(slcr: Bench) -> None:
    unlock(slcr)
    slcr.write(ARM_PLL_CTRL, 0x0001A009)  # PLL_RESET
    assert slcr.read(PLL_STATUS) == 0x3F & ~(1 << 0 | 1 << 3)
    slcr.write(IO_PLL_CTRL, 0x0001A00A)  # PLL_PWRDWN
    assert slcr.read(PLL_STATUS) == 0x3F & ~(1 << 0 | 1 << 3 | 1 << 2 | 1 << 5)
    slcr.write(ARM_PLL_CTRL, 0x0001A008)
    slcr.write(IO_PLL_CTRL, 0x0001A008)
    assert slcr.read(PLL_STATUS) == 0x3F
    slcr.write(PLL_STATUS, 0)  # read-only
    assert slcr.read(PLL_STATUS) == 0x3F


def test_divider_writes_are_stored(slcr: Bench) -> None:
    # Clocks are not generated: a divider write is storage, read back as written.
    unlock(slcr)
    slcr.write(SLCR + 0x154, 0x00001402)  # UART_CLK_CTRL: divisor 20, UART1 clock on
    assert slcr.read(SLCR + 0x154) == 0x00001402


def test_dci_done_follows_enable(slcr: Bench) -> None:
    unlock(slcr)
    assert slcr.read(DDRIOB_DCI_STATUS) == 0
    slcr.write(DDRIOB_DCI_CTRL, 1 << 1)  # ENABLE
    assert slcr.read(DDRIOB_DCI_STATUS) == 1 << 13  # DONE
    slcr.write(DDRIOB_DCI_CTRL, 0)
    assert slcr.read(DDRIOB_DCI_STATUS) == 0


def test_boot_mode_reads_the_straps(slcr: Bench) -> None:
    assert slcr.read(BOOT_MODE) == 1  # Quad-SPI, from the platform description
    slcr.execute("slcr BootMode 0x5")  # NAND, set by the harness
    assert slcr.read(BOOT_MODE) == 5
    unlock(slcr)
    slcr.write(BOOT_MODE, 0)  # read-only
    assert slcr.read(BOOT_MODE) == 5


def test_a_software_reset_records_its_cause_and_resets_the_machine(slcr: Bench) -> None:
    unlock(slcr)
    slcr.write(ARM_CLK_CTRL, 0x1F000200)
    slcr.write(PSS_RST_CTRL, 1)  # SOFT_RST
    assert slcr.read(REBOOT_STATUS) == POR | SLC_RST
    slcr.run_us(1)  # the machine resets at its next synchronisation point
    assert slcr.read(LOCKSTA) == 1
    assert slcr.read(ARM_CLK_CTRL) == 0x1F000400
    assert slcr.read(PSS_RST_CTRL) == 0
    assert slcr.read(REBOOT_STATUS) == POR | SLC_RST  # survives every reset but power-on
    unlock(slcr)
    slcr.write(REBOOT_STATUS, 0)  # software clears it
    assert slcr.read(REBOOT_STATUS) == 0


def test_reset_pins_record_their_causes(slcr: Bench) -> None:
    unlock(slcr)
    slcr.write(REBOOT_STATUS, 0)
    slcr.execute("slcr OnGPIO 1 true")  # PS_SRST_B asserted
    slcr.run_us(1)
    assert slcr.read(REBOOT_STATUS) == SRST_B
    slcr.execute("slcr OnGPIO 1 false")
    slcr.execute("slcr OnGPIO 0 true")  # PS_POR_B asserted: only the power-on cause remains
    slcr.run_us(1)
    assert slcr.read(REBOOT_STATUS) == POR
    assert slcr.text("slcr LastResetCause") == "PowerOn"


def test_fclk_resets_follow_fpga_rst_ctrl(slcr: Bench) -> None:
    # FCLK_RESETn_N is low (asserted) while FPGAn_OUT_RST is set.
    assert [slcr.flag(f"probe Level {n}") for n in range(4)] == [False] * 4
    unlock(slcr)
    slcr.write(FPGA_RST_CTRL, 0)
    assert [slcr.flag(f"probe Level {n}") for n in range(4)] == [True] * 4
    slcr.write(FPGA_RST_CTRL, 1 << 2)
    assert [slcr.flag(f"probe Level {n}") for n in range(4)] == [True, True, False, True]
    assert slcr.number("probe Falls 2") == 1


PERIPHERALS = """\
slcr: Jig.Xilinx.Zynq7000_SLCR @ sysbus <0xF8000000, +0x1000>
    init:
        AddResetTarget uart0 0x228 0x5

uart0: Jig.Cadence.CadenceUART @ sysbus <0xE0000000, +0x1000>
"""


def test_a_reset_bit_resets_its_target(bench: Bench) -> None:
    bench.load(PERIPHERALS)
    uart_mode = 0xE0000004
    unlock(bench)
    bench.write(uart_mode, 0x20)
    bench.write(UART_RST_CTRL, 1 << 1)  # UART1_CPU1X_RST: not uart0's
    assert bench.read(uart_mode) == 0x20
    bench.write(UART_RST_CTRL, 1 << 2)  # UART0_REF_RST
    assert bench.read(uart_mode) == 0
    bench.write(UART_RST_CTRL, 0)
    bench.write(uart_mode, 0x20)
    bench.write(UART_RST_CTRL, 1 << 0)  # UART0_CPU1X_RST
    assert bench.read(uart_mode) == 0
    assert bench.read(UART_RST_CTRL) == 1  # the register keeps what was written


def test_a_reset_target_must_name_a_reset_register(bench: Bench) -> None:
    bench.load(PERIPHERALS)
    with pytest.raises(Exception, match="is not a reset-control register"):
        bench.execute("slcr AddResetTarget sysbus.uart0 0x300 0x1")
    with pytest.raises(Exception, match="at least one reset bit"):
        bench.execute("slcr AddResetTarget sysbus.uart0 0x228 0x0")


def test_the_platform_wires_each_controller_to_its_reset_bits(bench: Bench) -> None:
    platform = project.platform_file("xilinx/zynq7000")
    bench.load(
        f'using "{platform.as_posix()}"\n\n'
        "globalTimer:\n    frequency: 333333333\n\n"
        "privateTimer:\n    frequency: 333333333\n\n"
        "swdt:\n    frequency: 111111111\n"
    )
    bench.execute("cpu0 IsHalted true")
    unlock(bench)
    # One register each that holds a written value and resets to a known one.
    targets = [
        (SPI_RST_CTRL, 0x1, 0xE0006028, 0x1),  # spi0 TX_thres
        (SPI_RST_CTRL, 0x8, 0xE0007028, 0x1),  # spi1, by its reference-clock bit
        (CAN_RST_CTRL, 0x1, 0xE0008008, 0x0),  # can0 BRPR
        (CAN_RST_CTRL, 0x2, 0xE0009008, 0x0),  # can1 BRPR
        (UART_RST_CTRL, 0x1, 0xE0000018, 0x28B),  # uart0 BAUDGEN
        (UART_RST_CTRL, 0x8, 0xE0001018, 0x28B),  # uart1, by its reference-clock bit
        (GPIO_RST_CTRL, 0x1, 0xE000A204, 0x0),  # gpio DIRM_0
    ]
    for _, _, address, _ in targets:
        bench.write(address, 0x7)
    for control, bits, address, reset in targets:
        assert bench.read(address) == 0x7, hex(address)
        bench.write(control, bits)
        bench.write(control, 0)
        assert bench.read(address) == reset, hex(address)
