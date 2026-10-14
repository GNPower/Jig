"""Xilinx AXI GPIO v2.0 (Jig.Xilinx.XilinxAxiGpio, PG144): power-on values
from the IP parameters, outputs driving their lines, inputs seen in GPIO_DATA,
read-modify-writes that keep the outputs, the channel interrupts with the
toggle-on-write status register, and a single-channel core."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

GPIO = 0x40000000
DATA1, TRI1, DATA2, TRI2 = GPIO + 0x000, GPIO + 0x004, GPIO + 0x008, GPIO + 0x00C
GIER, IP_ISR, IP_IER = GPIO + 0x11C, GPIO + 0x120, GPIO + 0x128
GLOBAL_ENABLE = 1 << 31

# Channel 1 bits 0-3 are outputs to probe inputs 0-3; probe outputs 10 and 11
# drive channel 1 bits 4 and 5, probe output 12 channel 2 bit 0.
MACHINE = """\
axiGpio: Jig.Xilinx.XilinxAxiGpio @ sysbus <0x40000000, +0x200>
    dataDefault1: 0x5
    triDefault1: 0xFFFFFFF0
    [0-3] -> probe@[0-3]
    IRQ -> probe@8

probe: JigFixtures.GpioProbe @ sysbus
    [10-11] -> axiGpio@[4-5]
    12 -> axiGpio@32
"""


@pytest.fixture
def gpio(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def lines(bench: Bench) -> list[bool]:
    return [bench.flag(f"probe Level {n}") for n in range(4)]


def test_power_on_values_come_from_the_parameters(gpio: Bench) -> None:
    assert gpio.read(TRI1) == 0xFFFFFFF0
    assert gpio.read(DATA1) == 0x5  # outputs show their data, inputs read low
    assert gpio.read(TRI2) == 0xFFFFFFFF  # C_TRI_DEFAULT_2 left at its default
    assert gpio.read(DATA2) == 0
    assert (gpio.read(GIER), gpio.read(IP_ISR), gpio.read(IP_IER)) == (0, 0, 0)
    assert lines(gpio) == [True, False, True, False]


def test_outputs_drive_their_lines(gpio: Bench) -> None:
    gpio.write(DATA1, 0xA)
    assert lines(gpio) == [False, True, False, True]
    gpio.write(TRI1, 0xFFFFFFFF)  # all inputs: the lines are released
    assert lines(gpio) == [False] * 4


def test_inputs_show_in_the_data_register(gpio: Bench) -> None:
    gpio.execute("probe Drive 10 true")
    assert gpio.read(DATA1) == 0x5 | 1 << 4
    gpio.execute("axiGpio SetInput 1 5 true")  # board wiring held at a level
    assert gpio.read(DATA1) == 0x5 | 1 << 4 | 1 << 5
    # A read-modify-write of the data register keeps the outputs.
    gpio.write(DATA1, gpio.read(DATA1) | 0x2)
    assert lines(gpio) == [True, True, True, False]


def test_input_changes_raise_the_channel_interrupt(gpio: Bench) -> None:
    gpio.write(IP_IER, 0x3)
    gpio.execute("probe Drive 10 true")
    assert gpio.read(IP_ISR) == 0x1
    assert not gpio.flag("probe Level 8")  # GIER gates the output
    gpio.write(GIER, GLOBAL_ENABLE)
    assert gpio.flag("probe Level 8")
    gpio.write(IP_ISR, 0x1)  # toggle on write: clears the set bit
    assert gpio.read(IP_ISR) == 0 and not gpio.flag("probe Level 8")
    gpio.execute("probe Drive 12 true")  # channel 2
    assert gpio.read(IP_ISR) == 0x2


def test_an_output_bit_change_is_no_input_event(gpio: Bench) -> None:
    gpio.write(IP_IER, 0x1)
    gpio.write(DATA1, 0xF)
    assert gpio.read(IP_ISR) == 0


def test_a_single_channel_core(bench: Bench) -> None:
    bench.load(
        "gpio: Jig.Xilinx.XilinxAxiGpio @ sysbus <0x40000000, +0x200>\n    dualChannel: false\n"
    )
    bench.write(DATA2, 0xFF)
    bench.write(TRI2, 0)
    assert bench.read(DATA2) == 0 and bench.read(TRI2) == 0
