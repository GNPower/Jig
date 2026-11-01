"""Zynq-7000 block designs (``jig.ext.zynq7000.blockdesign``): reading the
hardware handoff from an XSA, what the generator derives from it, and the
platform text it writes, with and without ip lines.

The design is ``tests/fixtures/hdl/example_bd.hwh``, written by hand for these
tests; its header comment lists what it holds."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from jig.errors import ConfigError
from jig.ext.zynq7000 import blockdesign as bd
from jig.ext.zynq7000.blockdesign import (
    AddressRange,
    Cosim,
    IpOverride,
    Literal,
    Memory,
    Reference,
    ResetDomain,
    Signal,
)

from .conftest import FIXTURES, make_xsa

HWH = FIXTURES / "hdl" / "example_bd.hwh"
FCLK = {0: 100e6, 1: 50e6}

# Vivado writes a handoff of its own for each SmartConnect, beside the block
# design's; it holds no processing_system7 instance.
SCOPED = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<EDKSYSTEM EDWVERSION="1.2" TIMESTAMP="Mon Oct 19 12:00:00 2026" VIVADOVERSION="2024.1">
  <SYSTEMINFO ARCH="zynq" DEVICE="7z020" NAME="example_bd_smartconnect_0_0"/>
  <MODULES>
    <MODULE INSTANCE="s00_entry_pipeline" MODTYPE="sc_node" VLNV="xilinx.com:ip:sc_node:1.0"/>
  </MODULES>
</EDKSYSTEM>
"""

SYSDEF = """<?xml version="1.0"?>
<Project Version="1" Minor="7">
  <File Type="HW_HANDOFF" Name="example_bd.hwh" BD_TYPE="DEFAULT_BD"/>
  <File Type="HW_HANDOFF" Name="example_bd_smartconnect_0_0.hwh" BD_TYPE="SCOPED_BD"/>
</Project>
"""

# A PS whose one interrupt line comes from bit 2 of a design port through a
# slice, with no concat.
SLICED = """<?xml version="1.0" encoding="UTF-8"?>
<EDKSYSTEM>
  <SYSTEMINFO NAME="sliced"/>
  <EXTERNALPORTS>
    <PORT DIR="I" LEFT="3" NAME="irqs" RIGHT="0" SIGNAME="irqs_1">
      <CONNECTIONS><CONNECTION INSTANCE="xlslice_0" PORT="Din"/></CONNECTIONS>
    </PORT>
  </EXTERNALPORTS>
  <MODULES>
    <MODULE INSTANCE="processing_system7_0" VLNV="xilinx.com:ip:processing_system7:5.5">
      <PORTS>
        <PORT DIR="I" NAME="IRQ_F2P" SIGNAME="xlslice_0_Dout">
          <CONNECTIONS><CONNECTION INSTANCE="xlslice_0" PORT="Dout"/></CONNECTIONS>
        </PORT>
      </PORTS>
    </MODULE>
    <MODULE INSTANCE="xlslice_0" VLNV="xilinx.com:ip:xlslice:1.0">
      <PARAMETERS>
        <PARAMETER NAME="DIN_FROM" VALUE="2"/>
        <PARAMETER NAME="DIN_TO" VALUE="2"/>
        <PARAMETER NAME="DIN_WIDTH" VALUE="4"/>
      </PARAMETERS>
      <PORTS>
        <PORT DIR="I" LEFT="3" NAME="Din" RIGHT="0" SIGNAME="irqs_1">
          <CONNECTIONS><CONNECTION INSTANCE="External_Ports" PORT="irqs"/></CONNECTIONS>
        </PORT>
        <PORT DIR="O" LEFT="0" NAME="Dout" RIGHT="0" SIGNAME="xlslice_0_Dout">
          <CONNECTIONS><CONNECTION INSTANCE="processing_system7_0" PORT="IRQ_F2P"/></CONNECTIONS>
        </PORT>
      </PORTS>
    </MODULE>
  </MODULES>
</EDKSYSTEM>
"""

EXPECTED = """\
// Programmable logic of block design example_bd (example.xsa, example_bd.hwh).
// IRQ_F2P (DIRECT). Lines 0-7 of the PS are interrupt IDs 61-68 and lines 8-15
// IDs 84-91 (UG585 v1.7 table 7-3); ID N is gic@(N-32).
//   [0] ID 61: axi_quad_spi_0 IRQ
//   [1] ID 62: axi_timer_0/interrupt, no model
//   [2] ID 63: fabric_irq[0], a port of the design; no ip line
//   [3] ID 64: fabric_irq[1], a port of the design; no ip line
//   [4] ID 65: constant 0 (xlconstant_1)
//   [5] ID 66: not connected
//   [6] ID 67: axi_cdma_0 IRQ
//   [7] ID 68: not connected
//   [8] ID 84: user_irq, a port of the design; no ip line
//   [9] ID 85: axi_gpio_0 IRQ
//   [10] ID 86: not connected
// user_axi: exported AXI port at 0x43C00000-0x43C0FFFF; no ip line places a
// model there.

// axi_gpio_0 (xilinx.com:ip:axi_gpio:2.0) at 0x41200000-0x4120FFFF, reset by
// FCLK_RESET0_N through proc_sys_reset_0.
// Inputs:
//   [0] FCLK_RESET1_N
//   [1] FCLK_RESET1_N through proc_sys_reset_1
//   [2] constant 1
//   [3] FCLK_RESET0_N through proc_sys_reset_0
//   [7:4] switches[3:0], a port of the design
axi_gpio_0: Jig.Xilinx.XilinxAxiGpio @ sysbus 0x41200000
    dataDefault1: 0x00000000
    triDefault1: 0xFFFFFFFF
    dataDefault2: 0x000000A5
    triDefault2: 0x00000000
    dualChannel: true
    IRQ -> gic@53
    init:
        SetInput 1 2 true

// axi_quad_spi_0 (xilinx.com:ip:axi_quad_spi:3.2) at 0x41E00000-0x41E0FFFF,
// reset by FCLK_RESET0_N through proc_sys_reset_0.
// SCK: ext_spi_clk (processing_system7_0/FCLK_CLK0, 100 MHz) / C_SCK_RATIO 16
// = 6.25 MHz.
axi_quad_spi_0: Jig.Xilinx.XilinxAxiQuadSpi @ sysbus 0x41E00000
    spiClockFrequency: 6250000
    fifoDepth: 256
    slaveSelectBits: 2
    IRQ -> gic@29

// axi_cdma_0 (xilinx.com:ip:axi_cdma:4.1) at 0x7E200000-0x7E20FFFF, reset by
// FCLK_RESET0_N through proc_sys_reset_0.
axi_cdma_0: Jig.Xilinx.XilinxAxiCdma @ sysbus 0x7E200000
    reachableRanges: "0x00000000:0x20000000,0x80000000:0x2000,0xFFFC0000:0x40000"
    IRQ -> gic@35

// axi_bram_ctrl_0 (xilinx.com:ip:axi_bram_ctrl:4.1) at 0x80000000-0x80001FFF,
// reset by FCLK_RESET1_N through proc_sys_reset_1.
axi_bram_ctrl_0: Memory.MappedMemory @ sysbus 0x80000000
    size: 0x2000

// FCLK_RESETn_N (slcr GPIO outputs 0-3) into the AXI GPIO inputs they drive.
slcr:
    0 -> axi_gpio_0@3
    1 -> axi_gpio_0@0 | axi_gpio_0@1

// The PL reset domain: block RAM cleared with the PL; AXI slaves that
// stall the CPU while their FCLK reset is asserted.
pl:
    init add:
        AddPeripheral axi_gpio_0 0
        AddPeripheral axi_quad_spi_0 0
        AddPeripheral axi_cdma_0 0
        AddMemory axi_bram_ctrl_0

// Instances without a model: their ranges read 0 and ignore writes.
sysbus:
    init add:
        Tag <0x42800000, 0x4280FFFF> "axi_timer_0" 0 false true
"""


def handoff(*changes: tuple[str, str]) -> str:
    """The fixture handoff with each (old, new) replacement made once."""
    text = HWH.read_text(encoding="utf-8")
    for old, new in changes:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def write_xsa(path: Path, members: dict[str, str]) -> Path:
    """An XSA holding ps7_init.tcl, a bitstream and ``members``."""
    make_xsa(path)
    with zipfile.ZipFile(path, "a") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return path


@pytest.fixture
def xsa(tmp_path: Path) -> Path:
    return write_xsa(
        tmp_path / "example.xsa",
        {
            "sysdef.xml": SYSDEF,
            "example_bd.hwh": handoff(),
            "example_bd_smartconnect_0_0.hwh": SCOPED,
        },
    )


@pytest.fixture
def design(xsa: Path) -> bd.BlockDesign:
    return bd.read(xsa)


def render(*overrides: IpOverride, text: str | None = None) -> str:
    design = bd.parse(text if text is not None else handoff(), "example.xsa:example_bd.hwh")
    return bd.render(design, FCLK, overrides)


def prose(text: str) -> str:
    """The comments of ``text`` joined into one line, whatever their wrapping."""
    return " ".join(line[3:].strip() for line in text.splitlines() if line.startswith("// "))


def entry(text: str, name: str) -> list[str]:
    """The lines of one peripheral's entry, from its first line to the blank line after it."""
    lines = [*text.splitlines(), ""]
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}: "))
    return lines[start : lines.index("", start)]


# ------------------------------------------------------------------ reading


def test_the_handoff_holding_the_ps_is_read(design: bd.BlockDesign) -> None:
    assert design.source == "example.xsa:example_bd.hwh"
    assert design.name == "example_bd"
    assert sorted(design.instances) == [
        "axi_bram_ctrl_0", "axi_cdma_0", "axi_gpio_0", "axi_interconnect_0",
        "axi_quad_spi_0", "axi_timer_0", "blk_mem_gen_0", "proc_sys_reset_0",
        "proc_sys_reset_1", "processing_system7_0", "smartconnect_0",
        "xlconcat_0", "xlconcat_1", "xlconstant_0", "xlconstant_1",
    ]  # fmt: skip
    gpio = design.instances["axi_gpio_0"]
    assert (gpio.vlnv, gpio.ip, gpio.kind) == (
        "xilinx.com:ip:axi_gpio:2.0",
        "xilinx.com:ip:axi_gpio",
        "axi_gpio",
    )
    assert gpio.parameters["C_DOUT_DEFAULT_2"] == "0x000000A5"
    assert gpio.number("C_DOUT_DEFAULT_2") == 0xA5
    assert gpio.ports["gpio_io_i"].width == 8
    assert design.ps is design.instances["processing_system7_0"]
    assert sorted(design.ports) == [
        "fabric_irq", "leds", "switches", "user_aclk", "user_aresetn",
        "user_axi_araddr", "user_axi_arready", "user_axi_arvalid",
        "user_axi_rdata", "user_axi_rready", "user_axi_rvalid", "user_irq",
    ]  # fmt: skip
    assert design.interfaces["user_axi"] == bd.BusInterface(
        "user_axi", "MASTER", "axi_interconnect_0_M03_AXI"
    )


def test_the_handoff_is_found_without_sysdef(tmp_path: Path) -> None:
    xsa = write_xsa(
        tmp_path / "plain.xsa",
        {
            "sysdef.xml": "<Project",  # unreadable: ignored
            "example_bd_smartconnect_0_0.hwh": SCOPED,
            "example_bd.hwh": handoff(),
        },
    )
    assert bd.read(xsa).source == "plain.xsa:example_bd.hwh"


def test_sysdef_names_the_top_design_when_two_hold_a_ps(tmp_path: Path) -> None:
    second = handoff(('NAME="example_bd" PACKAGE', 'NAME="other_bd" PACKAGE'))
    xsa = write_xsa(
        tmp_path / "two.xsa",
        {
            "sysdef.xml": SYSDEF,
            "example_bd_smartconnect_0_0.hwh": second,
            "example_bd.hwh": handoff(),
        },
    )
    assert bd.read(xsa).name == "example_bd"


def test_an_xsa_without_a_handoff_has_no_pl(tmp_path: Path) -> None:
    xsa = make_xsa(tmp_path / "ps-only.xsa")
    design = bd.read(xsa)
    assert design.ps is None
    assert (design.instances, design.slaves(), design.interrupts()) == ({}, [], [])
    assert bd.pl_section(xsa, FCLK) == "// ps-only.xsa holds no block design: no PL instances.\n"


@pytest.mark.parametrize(
    ("members", "message"),
    [
        (
            {"sysdef.xml": SYSDEF, "example_bd_smartconnect_0_0.hwh": SCOPED},
            "sysdef.xml names the hardware handoff example_bd.hwh, which the archive does not hold",
        ),
        (
            {"example_bd.hwh": handoff()[:-40]},
            "example_bd.hwh: the hardware handoff is not readable XML",
        ),
        (
            {"example_bd_smartconnect_0_0.hwh": SCOPED},
            r"no hardware handoff in it holds a processing_system7 instance \(it has "
            r"example_bd_smartconnect_0_0.hwh\)",
        ),
        (
            {"a.hwh": handoff(), "b.hwh": handoff()},
            "a.hwh, b.hwh each hold a processing_system7 instance, and sysdef.xml names none",
        ),
        (
            {"example_bd.hwh": '<SYSTEM><MODULE MODTYPE="processing_system7"/></SYSTEM>'},
            "not a hardware handoff \\(its root element is SYSTEM, not EDKSYSTEM\\)",
        ),
        (
            {"example_bd.hwh": handoff(('BASEVALUE="0x41200000"', 'BASEVALUE="0x4120_000G"'))},
            "MEMRANGE axi_gpio_0 has BASEVALUE='0x4120_000G', not a number",
        ),
    ],
)
def test_an_unreadable_xsa_names_itself_and_what_is_missing(
    tmp_path: Path, members: dict[str, str], message: str
) -> None:
    xsa = write_xsa(tmp_path / "broken.xsa", members)
    with pytest.raises(ConfigError, match=message) as error:
        bd.read(xsa)
    assert "broken.xsa" in str(error.value)


def test_a_missing_or_corrupt_xsa_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"XSA .*absent\.xsa not found"):
        bd.read(tmp_path / "absent.xsa")
    corrupt = tmp_path / "corrupt.xsa"
    corrupt.write_bytes(b"PK\x03\x04 not a zip")
    with pytest.raises(ConfigError, match=r"corrupt\.xsa is not a readable zip archive"):
        bd.read(corrupt)


# --------------------------------------------------------------------- maps


def test_the_ps_map_reaches_the_slaves_and_the_exported_port(design: bd.BlockDesign) -> None:
    assert [(s.slave, s.interface, hex(s.base), s.size, s.memory) for s in design.slaves()] == [
        ("axi_gpio_0", "M_AXI_GP0", "0x41200000", 0x10000, False),
        ("axi_quad_spi_0", "M_AXI_GP0", "0x41e00000", 0x10000, False),
        ("axi_timer_0", "M_AXI_GP0", "0x42800000", 0x10000, False),
        ("user_axi", "M_AXI_GP0", "0x43c00000", 0x10000, False),
        ("axi_cdma_0", "M_AXI_GP0", "0x7e200000", 0x10000, False),
        ("axi_bram_ctrl_0", "M_AXI_GP1", "0x80000000", 0x2000, True),
    ]
    assert {
        name: [str(s) for s in segments] for name, segments in design.exported_axi().items()
    } == {"user_axi": ["0x43C00000-0x43C0FFFF"]}


def test_the_cdma_reaches_what_its_own_map_lists(design: bd.BlockDesign) -> None:
    assert [(s.slave, s.slave_interface, str(s)) for s in design.reach("axi_cdma_0")] == [
        ("processing_system7_0", "S_AXI_HP0", "0x00000000-0x1FFFFFFF"),
        ("axi_bram_ctrl_0", "S_AXI", "0x80000000-0x80001FFF"),
        ("processing_system7_0", "S_AXI_HP0", "0xFFFC0000-0xFFFFFFFF"),
    ]


# --------------------------------------------------------------------- nets


def test_gpio_inputs_trace_through_the_concat(design: bd.BlockDesign) -> None:
    inputs = design.gpio_inputs("axi_gpio_0")
    assert inputs == {
        0: Signal("processing_system7_0", "FCLK_RESET1_N"),
        1: Signal("proc_sys_reset_1", "peripheral_aresetn"),
        2: Signal("xlconstant_0", "dout", constant=1),
        3: Signal("proc_sys_reset_0", "peripheral_aresetn"),
        4: Signal(None, "switches", 0, 4),
        5: Signal(None, "switches", 1, 4),
        6: Signal(None, "switches", 2, 4),
        7: Signal(None, "switches", 3, 4),
    }
    assert [design.fclk_reset(inputs[n]) for n in range(8)] == [1, 1, None, 0] + [None] * 4
    assert str(inputs[2]) == "constant 1 (xlconstant_0)"
    assert str(inputs[6]) == "switches[2]"
    # Without NUM_PORTS the concat's inputs are counted; constants may be binary.
    text = handoff(
        ('<PARAMETER NAME="NUM_PORTS" VALUE="5"/>', ""),
        ('<PARAMETER NAME="CONST_VAL" VALUE="0x1"/>', '<PARAMETER NAME="CONST_VAL" VALUE="0b1"/>'),
    )
    assert bd.parse(text, "variant.hwh").gpio_inputs("axi_gpio_0") == inputs


def test_a_slice_and_a_direct_connection_are_followed() -> None:
    design = bd.parse(SLICED, "sliced.hwh")
    (interrupt,) = design.interrupts()
    assert interrupt.source == Signal(None, "irqs", 2, 4)
    assert (interrupt.id, interrupt.gic) == (61, 29)
    assert design.exported_interrupts() == {"irqs": [interrupt]}


def test_nothing_drives_an_open_input(design: bd.BlockDesign) -> None:
    assert design.driver("xlconcat_0", "In4") is None
    assert design.driver("processing_system7_0", "IRQ_F2P", 5) is None


# --------------------------------------------------------------- interrupts


def test_irq_f2p_lines_map_to_ug585_ids() -> None:
    # UG585 v1.7 table 7-3: IRQF2P[7:0] are IDs 61-68, IRQF2P[15:8] IDs 84-91.
    assert [bd.spi_id(line) for line in range(16)] == [*range(61, 69), *range(84, 92)]
    # REVERSE joins a narrower port to the top lines.
    assert [bd.spi_id(line, 11, "REVERSE") for line in range(11)] == [66, 67, 68, *range(84, 92)]
    assert [bd.spi_id(line, 16, "REVERSE") for line in (0, 15)] == [61, 91]
    for line, inputs in ((16, 16), (-1, 4), (4, 4)):
        with pytest.raises(ConfigError, match=rf"IRQ_F2P\[{line}\] does not exist"):
            bd.spi_id(line, inputs)


def test_interrupts_trace_irq_f2p_back_to_their_sources(design: bd.BlockDesign) -> None:
    assert [
        (i.line, i.id, i.gic, str(i.source) if i.source else None) for i in design.interrupts()
    ] == [
        (0, 61, 29, "axi_quad_spi_0/ip2intc_irpt"),
        (1, 62, 30, "axi_timer_0/interrupt"),
        (2, 63, 31, "fabric_irq[0]"),
        (3, 64, 32, "fabric_irq[1]"),
        (4, 65, 33, "constant 0 (xlconstant_1)"),
        (5, 66, 34, None),
        (6, 67, 35, "axi_cdma_0/cdma_introut"),
        (7, 68, 36, None),
        (8, 84, 52, "user_irq"),
        (9, 85, 53, "axi_gpio_0/ip2intc_irpt"),
        (10, 86, 54, None),
    ]
    exported = design.exported_interrupts()
    assert {port: [i.line for i in lines] for port, lines in exported.items()} == {
        "fabric_irq": [2, 3],
        "user_irq": [8],
    }


def test_reverse_mode_moves_the_lines(tmp_path: Path) -> None:
    text = handoff(('C_IRQ_F2P_MODE" VALUE="DIRECT"', 'C_IRQ_F2P_MODE" VALUE="REVERSE"'))
    design = bd.parse(text, "reverse.hwh")
    assert [i.id for i in design.interrupts()] == [66, 67, 68, *range(84, 92)]
    assert "    IRQ -> gic@58" in entry(bd.render(design, FCLK), "axi_gpio_0")  # line 9, ID 90


# ------------------------------------------------------------------- resets


def test_reset_domains_come_from_the_reset_blocks(design: bd.BlockDesign) -> None:
    assert design.reset_domain("axi_gpio_0") == ResetDomain("proc_sys_reset_0", 0)
    assert design.reset_domain("axi_cdma_0") == ResetDomain("proc_sys_reset_0", 0)
    assert design.reset_domain("axi_bram_ctrl_0") == ResetDomain("proc_sys_reset_1", 1)
    assert design.reset_domain("smartconnect_0") == ResetDomain("proc_sys_reset_1", 1)
    assert design.reset_domain("axi_timer_0") == ResetDomain(None, 0)  # FCLK_RESET0_N itself
    assert design.reset_domain("xlconcat_0") is None
    assert design.interface_reset("user_axi") == ResetDomain("proc_sys_reset_0", 0)
    assert design.interface_reset("leds") is None


def test_a_reset_from_outside_the_ps_has_no_fclk() -> None:
    # proc_sys_reset_0 takes its reset from the design's switches instead of FCLK_RESET0_N.
    text = handoff(
        (
            '<PORT DIR="I" NAME="ext_reset_in" POLARITY="ACTIVE_LOW" SIGIS="rst" '
            'SIGNAME="processing_system7_0_FCLK_RESET0_N">\n'
            "          <CONNECTIONS>\n"
            '            <CONNECTION INSTANCE="processing_system7_0" PORT="FCLK_RESET0_N"/>',
            '<PORT DIR="I" NAME="ext_reset_in" POLARITY="ACTIVE_LOW" SIGIS="rst" '
            'SIGNAME="switches_1">\n'
            "          <CONNECTIONS>\n"
            '            <CONNECTION INSTANCE="External_Ports" PORT="switches"/>',
        ),
        ('            <CONNECTION INSTANCE="proc_sys_reset_0" PORT="ext_reset_in"/>\n', ""),
    )
    design = bd.parse(text, "external-reset.hwh")
    assert design.reset_domain("axi_gpio_0") == ResetDomain("proc_sys_reset_0", None)
    text = bd.render(design, FCLK)
    assert "reset by proc_sys_reset_0 from outside the PS" in prose(text)
    assert "AddPeripheral axi_gpio_0" not in text


# ------------------------------------------------------------------- clocks


def test_the_quad_spi_sck_divides_ext_spi_clk(design: bd.BlockDesign) -> None:
    assert design.spi_clock_hz("axi_quad_spi_0", FCLK) == 6_250_000
    # ps7_init's FCLK wins over the frequency Vivado recorded on the port.
    assert design.spi_clock_hz("axi_quad_spi_0", {0: 111_111_111.0}) == pytest.approx(6944444.4375)
    assert design.spi_clock_hz("axi_quad_spi_0", {}) == 6_250_000
    assert design.clock_hz("axi_cdma_0", "m_axi_aclk", FCLK) == 50e6


def test_a_quad_spi_without_a_clock_is_an_error() -> None:
    text = handoff(
        (
            '<PORT CLKFREQUENCY="100000000" DIR="I" NAME="ext_spi_clk"',
            '<PORT DIR="I" NAME="ext_spi_clk"',
        )
    )
    with pytest.raises(ConfigError, match="no frequency for axi_quad_spi_0/ext_spi_clk"):
        bd.render(bd.parse(text, "x.hwh"), {1: 50e6})
    # An ip line replacing the instance needs no clock from the design.
    spi = IpOverride("axi_quad_spi_0", "Jig.Xilinx.XilinxAxiQuadSpi", {"spiClockFrequency": 1000})
    assert "spiClockFrequency: 1000" in bd.render(bd.parse(text, "x.hwh"), {1: 50e6}, [spi])


# --------------------------------------------------------------------- text


def test_the_platform_text_of_the_design(xsa: Path) -> None:
    assert bd.pl_section(xsa, FCLK) == EXPECTED


def test_a_memory_size_override_also_sets_the_cdma_reach() -> None:
    text = render(IpOverride("axi_bram_ctrl_0", Memory(0x4000)))
    assert entry(text, "axi_bram_ctrl_0") == [
        "axi_bram_ctrl_0: Memory.MappedMemory @ sysbus 0x80000000",
        "    size: 0x4000",
    ]
    assert 'reachableRanges: "0x00000000:0x20000000,0x80000000:0x4000,0xFFFC0000:0x40000"' in text
    assert "        AddMemory axi_bram_ctrl_0" in text.splitlines()


def test_a_model_on_an_exported_port_takes_its_interrupt_and_reset() -> None:
    text = render(
        IpOverride(
            "filter",
            "Example.Dsp.Filter",
            {
                "gain": 3,
                "rate": 2.5,
                "label": "left",
                "enabled": True,
                "bram": Reference("axi_bram_ctrl_0"),
                "mode": Literal(".Fast"),
            },
            on="user_axi",
            irq="user_irq",
        )
    )
    assert entry(text, "filter") == [
        "filter: Example.Dsp.Filter @ sysbus 0x43C00000",
        "    gain: 3",
        "    rate: 2.5",
        '    label: "left"',
        "    enabled: true",
        "    bram: axi_bram_ctrl_0",
        "    mode: .Fast",
        "    IRQ -> gic@52",
    ]
    lines = text.splitlines()
    assert (
        "filter: an ip line's model on user_axi, reset by FCLK_RESET0_N through "
        "proc_sys_reset_0." in prose(text)
    )
    assert "//   [8] ID 84: user_irq -> filter IRQ" in lines
    assert "        AddPeripheral filter 0" in lines
    assert not any(line.startswith("// user_axi: exported AXI port") for line in lines)


def test_a_cosimulated_block_claims_windows_and_interrupt_lines() -> None:
    text = render(
        IpOverride(
            "filter",
            Cosim(),
            {"frequency": 100_000_000, "useAbsoluteAddress": True},
            on={"user_axi": 0x1000, "axi_bram_ctrl_0": 0x4000},
            irq="fabric_irq",
        )
    )
    assert entry(text, "filter") == [
        "filter: CoSimulated.CoSimulatedPeripheral @ {",
        "        sysbus <0x43C00000, +0x1000>;",
        "        sysbus <0x80000000, +0x4000>",
        "    }",
        "    frequency: 100000000",
        "    useAbsoluteAddress: true",
        "    cosimToRenodeSignalRange: <0, +2>",
        "    0 -> gic@31",
        "    1 -> gic@32",
    ]
    lines = text.splitlines()
    # The BRAM's window is the block's: no memory of its own, wider reach for the CDMA.
    assert not any(line.startswith("axi_bram_ctrl_0:") for line in lines)
    assert "        AddMemory axi_bram_ctrl_0" not in lines
    assert "// axi_bram_ctrl_0 (0x80000000-0x80001FFF) is under ip filter." in lines
    assert 'reachableRanges: "0x00000000:0x20000000,0x80000000:0x4000,0xFFFC0000:0x40000"' in text
    assert "//   [3] ID 64: fabric_irq[1] -> filter signal 1" in lines
    assert "        AddPeripheral filter 0" in lines


def test_a_block_placed_by_address_and_line() -> None:
    class PlanRange:  # a plan's ``0x43C10000 .. 0x43C10FFF``
        low, high = 0x43C10000, 0x43C10FFF

    block = IpOverride("block", Cosim(), {"frequency": 10_000_000}, on=PlanRange(), irq=10)
    text = render(block)
    assert entry(text, "block") == [
        "block: CoSimulated.CoSimulatedPeripheral @ sysbus <0x43C10000, +0x1000>",
        "    frequency: 10000000",
        "    cosimToRenodeSignalRange: <0, +1>",
        "    0 -> gic@54",
    ]
    lines = text.splitlines()
    assert "block: an ip line's model on an address range." in prose(text)
    assert not any("AddPeripheral block" in line for line in lines)  # no reset domain known


def test_replacing_an_instance_keeps_its_address_interrupt_and_reset() -> None:
    text = render(IpOverride("axi_gpio_0", "Example.Gpio", {"pins": 8}))
    assert entry(text, "axi_gpio_0") == [
        "axi_gpio_0: Example.Gpio @ sysbus 0x41200000",
        "    pins: 8",
        "    IRQ -> gic@53",
    ]
    lines = text.splitlines()
    assert "        AddPeripheral axi_gpio_0 0" in lines
    # The inputs are listed for the author; only the table's model gets them wired.
    assert "slcr:" not in lines
    assert "Inputs: [0] FCLK_RESET1_N [1] FCLK_RESET1_N through proc_sys_reset_1" in prose(text)


def test_a_cosimulated_block_can_replace_an_instance() -> None:
    text = render(IpOverride("axi_quad_spi_0", Cosim(), {"frequency": 50_000_000}))
    assert entry(text, "axi_quad_spi_0") == [
        "axi_quad_spi_0: CoSimulated.CoSimulatedPeripheral @ sysbus <0x41E00000, +0x10000>",
        "    frequency: 50000000",
        "    cosimToRenodeSignalRange: <0, +1>",
        "    0 -> gic@29",
    ]
    assert "//   [0] ID 61: axi_quad_spi_0 signal 0" in text.splitlines()


def test_what_the_models_lack_is_noted() -> None:
    text = render(
        text=handoff(
            (
                '<PARAMETER NAME="C_INCLUDE_SG" VALUE="0"/>',
                '<PARAMETER NAME="C_INCLUDE_SG" VALUE="1"/>',
            ),
            (
                '<PARAMETER NAME="C_SPI_MODE" VALUE="0"/>',
                '<PARAMETER NAME="C_SPI_MODE" VALUE="2"/>',
            ),
        )
    )
    lines = text.splitlines()
    assert "// Built with scatter-gather, which the model lacks." in lines
    assert "// Dual, quad and XIP modes are beyond the model, which runs standard SPI." in lines


def test_a_modelled_ip_the_ps_cannot_reach_gets_no_model() -> None:
    text = render(
        text=handoff(
            (
                '<MEMRANGE ADDRESSBLOCK="Reg" BASENAME="C_BASEADDR" BASEVALUE="0x41200000" '
                'HIGHNAME="C_HIGHADDR" HIGHVALUE="0x4120FFFF" INSTANCE="axi_gpio_0" '
                'IS_DATA="TRUE" IS_INSTRUCTION="TRUE" MASTERBUSINTERFACE="M_AXI_GP0" '
                'MEMTYPE="REGISTER" SLAVEBUSINTERFACE="S_AXI"/>',
                "",
            )
        )
    )
    lines = text.splitlines()
    assert "axi_gpio_0 (xilinx.com:ip:axi_gpio:2.0) is not on the PS's address map" in prose(text)
    assert "//   [9] ID 85: axi_gpio_0/ip2intc_irpt, no model" in lines
    assert not any(line.startswith(("axi_gpio_0:", "slcr:")) for line in lines)


def test_an_xsa_without_a_handoff_takes_address_ranges(tmp_path: Path) -> None:
    xsa = make_xsa(tmp_path / "ps-only.xsa")
    block = IpOverride("block", "Example.Block", on=AddressRange(0x43C00000, 0x43C00FFF), irq=3)
    text = bd.pl_section(xsa, FCLK, [block])
    assert text.splitlines()[:2] == [
        "// ps-only.xsa holds no block design: no PL instances.",
        "// IRQ_F2P[3] ID 64: block IRQ",
    ]
    assert entry(text, "block") == [
        "block: Example.Block @ sysbus <0x43C00000, +0x1000>",
        "    IRQ -> gic@32",
    ]
    with pytest.raises(
        ConfigError,
        match=r"user_axi names no port or instance: ps-only\.xsa has no block design, so on "
        r"takes an address range",
    ):
        bd.pl_section(xsa, FCLK, [IpOverride("block", "Example.Block", on="user_axi")])


def test_a_device_without_an_xsa_places_ip_lines_by_address() -> None:
    block = IpOverride(
        "block", Cosim(), {"frequency": 10_000_000}, AddressRange(0x43C00000, 0x43C00FFF), 2
    )
    text = bd.render(bd.BlockDesign(), {}, [block])
    assert text.splitlines() == [
        "// No XSA: the ip lines below are the programmable logic.",
        "// IRQ_F2P[2] ID 63: block signal 0",
        "",
        "// block: an ip line's model on an address range.",
        "block: CoSimulated.CoSimulatedPeripheral @ sysbus <0x43C00000, +0x1000>",
        "    frequency: 10000000",
        "    cosimToRenodeSignalRange: <0, +1>",
        "    0 -> gic@31",
    ]
    with pytest.raises(ConfigError, match="ip ram: a device without an xsa has no block design"):
        bd.render(bd.BlockDesign(), {}, [IpOverride("ram", Memory(0x1000))])


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            [IpOverride("filter", "Example.Filter", on="nowhere")],
            r"nowhere is neither an exported AXI port nor an instance on the PS's address map "
            r"\(exported AXI ports: user_axi\)",
        ),
        (
            [IpOverride("filter", "Example.Filter")],
            "filter is not an instance of .*; place it with on",
        ),
        (
            [IpOverride("axi_gpio_0", "Example.Gpio", on="user_axi")],
            "axi_gpio_0 is an instance of .*drop on and irq",
        ),
        (
            [IpOverride("xlconcat_0", "Example.Concat")],
            "xlconcat_0 has no address on the PS's map",
        ),
        (
            [IpOverride("filter", "Example.Filter", on="user_axi", irq=9)],
            r"IRQ_F2P\[9\] would be driven by both axi_gpio_0 and filter",
        ),
        (
            [IpOverride("filter", "Example.Filter", on="user_axi", irq=11)],
            r"IRQ_F2P\[11\] does not exist \(the PS's IRQ_F2P has 11 lines\)",
        ),
        (
            [IpOverride("filter", "Example.Filter", on="user_axi", irq="fabric_irq")],
            "fabric_irq reaches 2 IRQ_F2P lines and the model has one IRQ output",
        ),
        (
            [IpOverride("filter", "Example.Filter", on="user_axi", irq="leds")],
            "leds is not a port of the design that reaches IRQ_F2P "
            r"\(such ports: fabric_irq, user_irq\)",
        ),
        (
            [IpOverride("filter", Cosim(), on={"user_axi": 0x3A600001})],
            r"filter \(0x43C00000-0x7E200000\) overlaps axi_cdma_0 \(from 0x7E200000\)",
        ),
        (
            [IpOverride("block", "Example.Block", on=AddressRange(0x41200100, 0x412001FF))],
            "overlaps block",
        ),
        (
            [
                IpOverride("a", "Example.Block", on="user_axi"),
                IpOverride("a", "Example.Block", on=AddressRange(0x43D00000, 0x43D00FFF)),
            ],
            "ip a: two ip lines share this name",
        ),
        (
            [
                IpOverride("a", Cosim(), on={"axi_bram_ctrl_0": 0x1000}),
                IpOverride("b", Cosim(), on="axi_bram_ctrl_0"),
            ],
            "ip b and ip a both cover axi_bram_ctrl_0",
        ),
        (
            [
                IpOverride("axi_bram_ctrl_0", Memory(0x1000)),
                IpOverride("b", Cosim(), on="axi_bram_ctrl_0"),
            ],
            "ip b: its window covers axi_bram_ctrl_0, which ip axi_bram_ctrl_0 also models",
        ),
        (
            [IpOverride("gic", "Example.Block", on="user_axi")],
            "gic is a peripheral of the Zynq-7000",
        ),
        ([IpOverride("bad name", "Example.Block", on="user_axi")], "is not a name Renode accepts"),
        ([IpOverride("block", "Example Block", on="user_axi")], "is not a Renode class name"),
        (
            [IpOverride("block", "Example.Block", {"label": 'a "b"'}, on="user_axi")],
            "a platform description cannot hold",
        ),
        (
            [IpOverride("block", "Example.Block", {"items": [1, 2]}, on="user_axi")],  # type: ignore[dict-item]
            "has no platform-description form",
        ),
        (
            [IpOverride("ram", Memory(0x1000), on="user_axi", irq=5)],
            "a memory has no interrupt output",
        ),
        ([IpOverride("ram", Memory(0), on="user_axi")], "a memory needs a size in bytes"),
        (
            [IpOverride("block", Cosim(), on={"user_axi": 0})],
            "the window on user_axi needs a size in bytes",
        ),
        (
            [IpOverride("block", "Example.Block", on=AddressRange(0x10, 0x0))],
            r"the range 0x10 \.\. 0x0 is empty",
        ),
        ([IpOverride("block", "Example.Block", on=[1, 2])], "on takes an exported AXI port"),  # type: ignore[arg-type]
    ],
)
def test_ip_lines_that_cannot_be_placed_are_errors(
    overrides: list[IpOverride], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        render(*overrides)
