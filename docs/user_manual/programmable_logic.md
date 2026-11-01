# Programmable logic from a Vivado block design

A Zynq-7000 device whose board names an XSA gets its programmable logic (PL)
from the block design inside that XSA. Jig reads the design's hardware
handoff file and writes the PL's part of the machine's platform description:
a model for each IP it simulates, the interrupts, the reset domains and the
wiring between the PS and the PL. `ip` lines in the device body replace or
add models. The module that does this is `jig.ext.zynq7000.blockdesign`.

## The file Jig reads

An XSA is a zip archive. Vivado puts a hardware handoff (`.hwh`) in it for
the block design, and one more for each SmartConnect's internal design. Jig
reads the handoff that holds the `processing_system7` instance; when two do,
the one `sysdef.xml` marks as the top block design (`BD_TYPE` `DEFAULT_BD`).
From it Jig takes:

- every instance, with its name, VLNV and parameters;
- the address map of every master: the PS's general-purpose ports
  (`M_AXI_GP0`, `M_AXI_GP1`) and masters in the PL such as an AXI CDMA, each
  range with its slave, the slave's interface, base and high address, and
  whether Vivado calls it memory or registers;
- the ports and bus interfaces the block design exports;
- the nets joining instance ports, from each port's net name and connection
  list.

An XSA without a `.hwh`, such as one that holds only `ps7_init` and a
bitstream, has no PL to generate. A handoff that `sysdef.xml` names but the
archive lacks, a handoff that isn't readable XML, and an XSA in which no
handoff holds the PS are errors that name the XSA and the file.

## Models by VLNV

Jig matches an instance's VLNV without its version:

```{list-table}
:header-rows: 1
:widths: 30 30 40

* - IP
  - Model
  - Taken from the design
* - `xilinx.com:ip:axi_gpio`
  - `Jig.Xilinx.XilinxAxiGpio`
  - `C_DOUT_DEFAULT`, `C_TRI_DEFAULT` and their channel 2 forms,
    `C_IS_DUAL`
* - `xilinx.com:ip:axi_quad_spi`
  - `Jig.Xilinx.XilinxAxiQuadSpi`
  - `spiClockFrequency` (below), `C_FIFO_DEPTH`, `C_NUM_SS_BITS`
* - `xilinx.com:ip:axi_cdma`
  - `Jig.Xilinx.XilinxAxiCdma`
  - `reachableRanges` (below)
* - `xilinx.com:ip:axi_bram_ctrl`
  - `Memory.MappedMemory`
  - the size of its address segment
```

Each model sits at the base address the PS's map gives the instance, under
the instance's block-design name (`axi_gpio_0`), the name Vivado's address
editor and the firmware's `xparameters.h` use. IP that only joins other IP
gets no text: `processing_system7`, `xlconcat`, `xlconstant`, `xlslice`,
`proc_sys_reset`, `axi_interconnect`, `smartconnect` and `blk_mem_gen`. Any
other instance on the PS's map becomes a silent tag over its range, named
after the instance: reads return 0, writes are dropped, and Renode logs
them at debug level only.

## Interrupts

The PS takes 16 interrupt lines from the PL. UG585 v1.7 table 7-3 gives
their shared peripheral interrupt IDs, and Jig's `xilinx/zynq7000` platform
connects ID N to `gic@(N-32)`:

```{list-table}
:header-rows: 1
:widths: 30 35 35

* - PS line
  - Interrupt IDs
  - Renode GIC lines
* - IRQF2P\[0\] to IRQF2P\[7\]
  - 61 to 68
  - `gic@29` to `gic@36`
* - IRQF2P\[8\] to IRQF2P\[15\]
  - 84 to 91
  - `gic@52` to `gic@59`
```

Jig follows each line of the `processing_system7` instance's `IRQ_F2P` port
back through `xlconcat` and `xlslice` blocks to the instance port or design
port that drives it, and connects the model's `IRQ` output to that line. An
`IRQ_F2P` port narrower than 16 lines joins the bottom lines when the IP's
`C_IRQ_F2P_MODE` is `DIRECT` and the top lines when it is `REVERSE`, so with
11 inputs in `REVERSE` mode `IRQ_F2P[0]` is ID 66. A line driven by an IP
without a model, by a constant or by a design port that no `ip` line takes
stays unconnected. The comment at the top of the generated text lists every
line, its ID and where it goes.

## Reset domains

Jig's Zynq-7000 platform models the PL as a reset domain, the `pl`
peripheral (`Zynq7000_PlDomain`). Block RAM it holds is cleared when the PL
is reset or configured again. An AXI slave it holds stalls the CPU when
software reaches it while its FCLK reset is asserted, as the AXI
interconnect does on the chip.

For each modelled slave, Jig traces the instance's `*aresetn` input to the
`proc_sys_reset` that drives it, and that block's `ext_reset_in` to the PS's
`FCLK_RESETn_N`, then writes `AddPeripheral <instance> <n>`. Memories get
`AddMemory <instance>`. A slave whose reset comes from outside the PS gets
neither.

## AXI GPIO inputs

The inputs of an AXI GPIO are traced through `xlconcat` blocks too. Bit n
of channel 1 (`gpio_io_i`) is the model's input n, and bit n of channel 2
(`gpio2_io_i`) its input 32 + n:

- an input driven by `FCLK_RESETn_N`, or by the active-low output of a
  `proc_sys_reset` that `FCLK_RESETn_N` feeds, follows the SLCR's GPIO output
  n (`slcr: 1 -> axi_gpio_0@0`);
- an input tied to a constant 1 is set when the machine starts
  (`SetInput 1 2 true`);
- an input from a port of the design is listed in the entry's comment, for
  the board's links to drive.

## The AXI Quad SPI clock and the AXI CDMA's reach

The AXI Quad SPI shifts at the clock on its `ext_spi_clk` divided by
`C_SCK_RATIO` (PG153). When the PS's `FCLK_CLKn` drives `ext_spi_clk`, the
clock is the one the XSA's `ps7_init` gives FCLKn; otherwise it is the
frequency Vivado recorded on the port. With FCLK0 at 100 MHz and a ratio of
16, SCK is 6.25 MHz.

An AXI CDMA reaches the ranges its own address map lists, its
`reachableRanges`. A transfer that leaves them ends with a decode error in
`CDMASR`, as the AXI interconnect returns one on the chip.

## What an ip line overrides

An `ip` line whose name is an instance of the block design replaces that
instance's model. The model keeps the instance's address, interrupt lines
and reset domain:

```jig
ip axi_bram_ctrl_0: memory(size: 64 KiB)    # a size other than the XSA's
ip axi_gpio_0: "Example.Gpio" (pins: 8)
```

A memory size set this way is also the size every other master reaches,
so the CDMA's `reachableRanges` grow with it. Only Jig's own AXI GPIO model
gets the SLCR wiring and the constant inputs; for a replacement, the
generated comment lists the inputs and their sources.

Any other name is a new block, which `on` places:

```jig
ip filter: "Example.Dsp.Filter" (on: user_axi, irq: user_irq, gain: 3)
ip filter: cosim(on: {user_axi: 4 KiB, axi_bram_ctrl_0: 16 KiB},
                 irq: fabric_irq, frequency: 100 MHz)
ip block: cosim(on: 0x43C10000 .. 0x43C10FFF, irq: 10, frequency: 10 MHz)
```

`on`
: An exported AXI port by name puts the model at the port's address
  segment. A map from ports to sizes gives the block one window from the
  base of each port's segment, and a port there may also be an instance's
  segment: that instance then gets no model of its own, so a block RAM that
  a co-simulated block serves has no memory beside it. An address range
  places the block anywhere, and it is the only form for a device without
  a block design: one without an XSA, or with an XSA that holds no `.hwh`.

`irq`
: An exported interrupt port connects the model's `IRQ` output to the
  `IRQ_F2P` line the port feeds. A number names an `IRQ_F2P` line directly.
  A co-simulated block takes a port of several bits and drives its lines
  with signals 0, 1 and so on, and Jig sets its `cosimToRenodeSignalRange`.

A block on an exported port takes the reset domain of the interconnect port
that exports it (`M03_AXI` resets with `M03_ARESETN`); a block on several
windows takes that of the first. A block placed by address has none.
Placing two models on one window, connecting two to one `IRQ_F2P` line, or
naming a port the design doesn't export is an error.

A property value can name an instance of the design that has a model, such
as a block RAM a model reads (`bram: axi_bram_ctrl_0`), as well as a
peripheral of the board or of the platform, or another `ip` line; the model
gets that peripheral. {doc}`board_files` lists the other places a
block-design name can go, such as the controller of an SPI link.

## Generated text

The test design `tests/fixtures/hdl/example_bd.hwh` drives the AXI GPIO's
inputs from both FCLK resets, two reset blocks, a constant and a 4-bit port
of the design. Part of what Jig writes for it, with FCLK0 at 100 MHz:

```text
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

// FCLK_RESETn_N (slcr GPIO outputs 0-3) into the AXI GPIO inputs they drive.
slcr:
    0 -> axi_gpio_0@3
    1 -> axi_gpio_0@0 | axi_gpio_0@1

pl:
    init add:
        AddPeripheral axi_gpio_0 0
        AddPeripheral axi_quad_spi_0 0
        AddPeripheral axi_cdma_0 0
        AddMemory axi_bram_ctrl_0
```

`IRQ -> gic@53` is `IRQ_F2P[9]`, interrupt ID 85, the second ID range.
