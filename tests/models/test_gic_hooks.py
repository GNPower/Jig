"""GICv1 distributor hooks (Jig.Arm GicDistributorExtensions) on Renode's
ARM_GenericInterruptController: LimitGicPriorityBits keeps only the
implemented priority bits (ARM IHI 0048, "Interrupt prioritization"; 5 on
Zynq-7000), and IgnoreGicHandlingModelBits lets an interrupt configured the
way the XScuGic driver writes ICFGR (0b01 level, 0b11 rising edge) become
pending. A machine without the hooks shows the Renode behaviour they patch.
Distributor accesses carry cpu0 as their initiator, as a core's would."""

from __future__ import annotations

import pytest

from jig.errors import RenodeError

from .bench import Bench

pytestmark = pytest.mark.renode

DISTRIBUTOR = 0xF8F01000
ISPENDR1 = DISTRIBUTOR + 0x204  # pending bits of interrupts 32-63
ICPENDR1 = DISTRIBUTOR + 0x284
IPRIORITYR8 = DISTRIBUTOR + 0x420  # priorities of interrupts 32-35
ICFGR2 = DISTRIBUTOR + 0xC08  # configuration of interrupts 32-47

MACHINE = """\
cpu0: CPU.ARMv7A @ sysbus
    cpuType: "cortex-a9"
    genericInterruptController: gic

gic: IRQControllers.ARM_GenericInterruptController @ {
  sysbus new Bus.BusMultiRegistration { address: 0xF8F01000; size: 0x1000; region: "distributor" };
  sysbus new Bus.BusMultiRegistration { address: 0xF8F00100; size: 0x100; region: "cpuInterface" }
    }
    supportsTwoSecurityStates: false
    architectureVersion: .GICv1

probe: JigFixtures.GpioProbe @ sysbus
    [0-3] -> gic@[0-3]
"""

HOOKS = """\
sysbus:
    init:
        LimitGicPriorityBits gic 5
        IgnoreGicHandlingModelBits gic
"""


def build(bench: Bench, *, hooks: bool = True) -> Bench:
    bench.load(MACHINE + ("\n" + HOOKS if hooks else ""))
    bench.execute("cpu0 IsHalted true")
    return bench


def read(bench: Bench, address: int) -> int:
    return bench.read(address, cpu="cpu0")


def write(bench: Bench, address: int, value: int) -> None:
    bench.write(address, value, cpu="cpu0")


def test_only_the_implemented_priority_bits_are_kept(bench: Bench) -> None:
    build(bench)
    write(bench, IPRIORITYR8, 0xFFFFFFFF)
    assert read(bench, IPRIORITYR8) == 0xF8F8F8F8
    bench.write_byte(IPRIORITYR8 + 1, 0x0F, cpu="cpu0")
    assert read(bench, IPRIORITYR8) == 0xF8F808F8
    # The FreeRTOS Cortex-A9 port's check: write 0xFF, count the bits that stick.
    bench.write_byte(IPRIORITYR8, 0xFF, cpu="cpu0")
    assert bin(bench.read_byte(IPRIORITYR8, cpu="cpu0")).count("1") == 5


def test_without_the_hook_all_eight_bits_stick(bench: Bench) -> None:
    build(bench, hooks=False)
    write(bench, IPRIORITYR8, 0xFFFFFFFF)
    assert read(bench, IPRIORITYR8) == 0xFFFFFFFF


def test_a_gic_implements_four_to_eight_bits(bench: Bench) -> None:
    build(bench, hooks=False)
    with pytest.raises(RenodeError, match="4 to 8 priority bits"):
        bench.execute("sysbus LimitGicPriorityBits gic 3")


def test_the_handling_model_bit_reads_zero(bench: Bench) -> None:
    build(bench)
    write(bench, ICFGR2, 0xFFFFFFFF)
    assert read(bench, ICFGR2) == 0xAAAAAAAA


def test_a_rising_edge_interrupt_written_as_0b11_becomes_pending(bench: Bench) -> None:
    build(bench)
    write(bench, ICFGR2, 0b11)  # interrupt 32: edge, 1-N, as XScuGic writes it
    bench.execute("probe Drive 0 true")
    assert read(bench, ISPENDR1) & 1
    write(bench, ICPENDR1, 1)
    bench.execute("probe Drive 0 false")
    assert read(bench, ISPENDR1) & 1 == 0  # an edge stays pending until cleared


def test_a_level_interrupt_written_as_0b01_follows_its_line(bench: Bench) -> None:
    build(bench)
    write(bench, ICFGR2, 0b01 << 2)  # interrupt 33: level, 1-N
    bench.execute("probe Drive 1 true")
    assert read(bench, ISPENDR1) & 0b10
    bench.execute("probe Drive 1 false")
    assert read(bench, ISPENDR1) & 0b10 == 0


def test_without_the_hook_such_an_interrupt_never_pends(bench: Bench) -> None:
    build(bench, hooks=False)
    write(bench, ICFGR2, 0b11)
    assert read(bench, ICFGR2) & 0b11 == 0b11
    bench.execute("probe Drive 0 true")
    assert read(bench, ISPENDR1) & 1 == 0
