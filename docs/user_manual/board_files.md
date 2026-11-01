# Board files

A board is a directory under a board root, named after the board, holding
`board.jig` and the files it refers to. The file is written in the Jig
language: optional `const` lines, then one `board "<name>" { ... }`, whose
name matches the directory. It describes one PCB: the devices that run code
on it, the peripherals wired to them, the links between them and the links
that leave the board, the analog inputs a plan drives, and the options a
system picks from. {doc}`../how_to/add_a_board` walks through writing one,
and `jig platform` prints the Renode platform description Jig writes from it.

## A complete example

The ZedBoard example's board, `examples/zedboard/boards/zedboard/board.jig`,
describes one ZedBoard running either of two builds of the example firmware:

```{literalinclude} ../../examples/zedboard/boards/zedboard/board.jig
:language: jig
```

A system picks the build with the board's option: `board a: "zedboard"
(node: 1)`.

## Terms

```{list-table}
:header-rows: 1
:widths: 18 82

* - Term
  - Meaning
* - device
  - A part that runs code and that Renode simulates: a Zynq-7000 (both
    cores and the programmable logic are one device) or an STM32. Each
    device is one Renode machine. In this release a board has one device;
    a board file that declares two is an error.
* - peripheral
  - A chip wired to a device that runs no code in the simulation: a flash,
    an ADC, a temperature sensor.
* - interface
  - A device's communication controller (`uart0`, `can1`, `spi2`).
* - protocol
  - A layer above an interface: `kiss`, `cfp`, `csp`, `vt100`, `cli`.
    Layers stack with `over`.
* - link
  - A connection: a UART line, a CAN bus, an SPI, QSPI or I2C bus, or a
    GPIO net. `external` in front marks a link that leaves the board
    through a connector; systems join external links.
* - option
  - A choice a board offers the systems that use it: a firmware build, the
    model that stands in for a block.
```

## Paths

`root` names the board's root, relative to the board file. Without it, the
root is the nearest directory at or above the board file that holds a
`.git` file or directory, so a board inside a git submodule resolves
against the submodule's checkout; failing that, it is the board file's own
directory.

- Relative to the root: firmware, the XSA, `airhdl(...)` sources and
  `cosim(...)` inputs.
- Relative to the board file: `models`, `plugin`, `repl` fragments, and a
  `build(...)` firmware's script and sources.

## How Jig reads a board file

Jig evaluates a board file when a run builds the board's machine, with
each option's value: its default, then jig.toml's `[defaults] options`,
then the system's board line, then `jig run --option`. `if`, `elif` and
`else` in the board and in device bodies pick declarations by those values,
and expressions and string fields use them: `"zed-node-{node}.elf"`.
Strings also see `workdir`, the machine's directory in the run, so a
monitor command in `commands` can name `@{workdir}/trace.vcd`; `jig check`
and `jig platform` evaluate with the placeholder `<workdir>`.

Declarations come in any order. Jig collects the names of the devices,
peripherals and links first, so a link can name a peripheral declared below
it. A name stands for what it declares: `zynq.uart1` is an interface of the
device `zynq`, `zynq.mio[7]` one of its pins, `flash0` a peripheral,
`chip.IRQ` one of a peripheral's outputs, `chip.gpio[0]` one of its inputs
and `adc.channel[3]` one of its analog inputs. A name declared twice is an
error with its file and line, and so is a name that is already an option, a
constant, `workdir`, or one of the board file's constructors: `choice`,
`memory`, `cosim`, `airhdl`, `build` and `command`.

## Settings

Lines of the form `name: value` set board-wide properties. An unknown
setting is an error.

```{list-table}
:header-rows: 1
:widths: 20 80

* - Setting
  - Value
* - `description`
  - One line, which `jig boards` prints under the board's name.
* - `root`
  - The board's root (see Paths).
* - `models`
  - A list of globs of `.cs` files: the board's C# models, compiled by
    Renode when a run starts. Each pattern matches at least one file, and
    only `.cs` files.
* - `plugin`
  - The board plugin: a `.py` file or a package directory
    ({doc}`../how_to/write_a_plugin`).
* - `rtc`
  - The real-time clock `board.set_time` and `board.time()` use: a
    peripheral, or a device's own clock (`mcu.rtc`). It implements Jig's
    real-time clock contract.
* - `storage`
  - A list of peripherals that keep an image file across resets, such as
    `[flash0]`. Each image's size is the peripheral's `size` property. The
    Zynq-7000 family keeps one image file per peripheral in each run, so
    writes survive resets, and a system's `storage` setting gives initial
    images.
* - `backup_supply`
  - Peripherals a power cycle leaves running, such as a clock on a coin
    cell. Every other peripheral whose model implements Jig's power-cycle
    contract restarts with the board.
* - `power`
  - `"on"` (the default) or `"off"`: the board's state when the plan
    starts. A system's board line can override it.
* - `repl`
  - A list of hand-written platform-description fragments, loaded after
    the description Jig writes, for what the notation can't express.
```

## Options

```jig
option node: choice(1, 2)
option pl: choice({model: "a C# model", rtl: "the RTL under Verilator"})
option address: 10
```

`choice(...)` lists the values a system may pick; the first is the default
unless `default:` names another. The map form gives each value the
description `jig boards` prints. An option given a plain value accepts any
value of that type and defaults to it. Options are declared at the board's
top level, and none can be called `boot`, `mips`, `storage` or `power`,
the settings every board takes from a system.

A system sets options on the board's line, `board b: "zedboard" (node:
2)`; `jig run --option node=2` sets them for every board that has the
option, and jig.toml's `[defaults] options` apply first. Values given as
text, from jig.toml or `--option`, are read as the option's type.

## Devices

`device NAME: FAMILY(arguments) { body }`. The arguments describe the
hardware; the body describes what runs on it.

```{list-table}
:header-rows: 1
:widths: 15 85

* - Family
  - Arguments
* - `zynq7000`
  - `part` (`"XC7Z020-CLG484"`); `xsa`, the hardware design; `ps_clk` and
    `ps_version`, both needed with an XSA; `straps` (`"jtag"`, `"qspi"`,
    `"nor"`, `"nand"`, `"sd"`); `ddr`, the size of the board's DDR;
    `clocks`, for a board without an XSA; `mips`.
* - `stm32`
  - `part`, which picks the platform; `clock`, the core clock;
    `vector_table` (`0x08000000` by default); `mips`; `platform`, a
    platform description for a part Jig has no table entry for.
```

`mips` is the core's performance in the emulation, 100 million
instructions per second by default. Plugins add families with
`api.device_family`, and so can distributions through the
`jig.device_families` entry-point group.

The body takes these lines:

```{list-table}
:header-rows: 1
:widths: 35 65

* - Line
  - Meaning
* - `firmware: ...`
  - What the device runs. A Zynq-7000 takes images by role,
    `{app, fsbl, bitstream}`. An STM32 takes an ELF path, `build(script,
    sources: ..., output: ...)` or `command([...], output: ...)`.
* - `boot MODE` or `boot MODE: {settings}`
  - A boot mode a system may pick; the first listed is the default.
    Without boot lines the family's modes apply: `jtag` (the default) and
    `qspi` for a Zynq-7000, `flash` for an STM32. The settings go to the
    family and the board's plugin: `boot qspi: {storage: flash0}`.
* - `protocol LAYER(...)` or `protocol NAME: LAYER(...)`
  - One protocol instance for the whole device, such as a CSP node with
    one address on two interfaces. Stacks name it by its layer (`csp`), or
    by `NAME` when a device runs two of one kind.
* - `interface NAME: STACK`
  - The protocol stack the firmware runs on one of the device's
    interfaces.
* - `ip NAME: MODEL`
  - A model for a block of programmable logic (below).
* - `unmodelled NAME: LOW .. HIGH`
  - An address range that reads 0, without a warning per access, for a
    block the firmware touches that has no model.
* - `PERIPHERAL.PROPERTY = VALUE`
  - A property of one of the device's own peripherals:
    `fdcan1.kernelFrequency = 16 MHz`.
* - `commands: [...]`
  - Monitor commands run in the device's machine after it loads.
* - `driver: "file.py:Class"`
  - A driver class for this device alone, as `module:Class` or with a
    `.py` file relative to the board file.
```

### zynq7000

Every Zynq-7000 part loads Jig's `xilinx/zynq7000` platform. The family's
interfaces are `uart0`, `uart1`, `can0`, `can1`, `spi0`, `spi1`, `qspi`,
`i2c0` and `i2c1`; its pins are `mio[0]` to `mio[53]` (GPIO pins 0 to 53),
`emio[0]` to `emio[63]` (GPIO pins 54 to 117), and `ps_por_b` and
`ps_srst_b`, the SLCR's reset inputs.

With an `xsa` whose block design Jig reads, every instance and every port
the design exports is a name of the device too, spelled as in Vivado. An
AXI Quad SPI is an SPI controller, so `spi(controller: zynq.axi_quad_spi_0,
targets: {0: clock})` puts `clock` at `axi_quad_spi_0` chip select 0. `ip`
lines name exported ports in `on` and `irq`, and a memory such as
`axi_bram_ctrl_0` as a property value. A link or a stack on an instance Jig
has no model for, or on a name the device doesn't have, is an error that
names it, and `jig check` reports it.

With an `xsa`, Jig computes the PS clocks from the register writes in the
design's `ps7_init`, starting from `ps_clk`. Without one, the firmware sets
the clocks it uses and `clocks` gives them by name; the family writes each
to the peripherals it drives:

```{list-table}
:header-rows: 1
:widths: 25 75

* - Clock
  - Peripherals
* - `cpu_3x2x`
  - `globalTimer`, `privateTimer`
* - `cpu_1x`
  - `swdt`, `ttc0`, `ttc1`
* - `uart_ref`
  - `uart0`, `uart1`
* - `spi_ref`
  - `spi0`, `spi1`
* - `can_ref`
  - `can0`, `can1`
* - `lqspi_ref`
  - `qspi`
* - `pcap`
  - `devc`
```

The timers and the watchdog take no default, so `cpu_3x2x` and `cpu_1x`
are needed. `cpu_6x4x`, `cpu_2x` and `fclk0` to `fclk3` are accepted and
clock no PS peripheral. `ddr` maps that much DDR from 0x00100000, the first
address every bus master sees as DDR (UG585 table 4-1).

Boot modes:

`jtag`
: What a debugger does: the core is held while the BootROM's hardware
  set-up runs, the design's register writes are replayed and the
  programmable logic configured when the board has a design, and the `app`
  ELF loads at its physical addresses with the program counter at its
  entry. The straps stay as they are, so the firmware reads the BOOT_MODE
  it reads on the bench.

`qspi`
: Power-on through the BootROM model, which searches the flash on chip
  select 0 for a boot header and starts the first-stage boot loader it
  finds. The flash starts from an image passed to the run or one the
  board's plugin builds, loaded into the storage peripheral `boot qspi:
  {storage: ...}` names (`flash0` by default).

After a reset, the BootROM follows the straps, as on the board.

### stm32

```{list-table}
:header-rows: 1
:widths: 25 75

* - Part
  - Platform
* - `STM32F407...`
  - Renode's `platforms/cpus/stm32f4.repl` with the F407's 128 KiB of
    SRAM1 and SRAM2 (the stock file maps 256 KiB), and without the SVD
    file the stock file's init downloads: its own `Tag` lines stay. `clock`
    sets the SysTick frequency.
* - `STM32U575...`, `STM32U595...`
  - Jig's `st/stm32u575` and `st/stm32u595`: the STM32U5 core and
    peripherals plus that part's memories, with Jig's models of the RCC,
    LPTIM, FDCAN, RTC, TAMP and ADC4 ({doc}`renode_defects` says what each
    fixes). `clock` sets the DWT cycle counter's frequency.
```

Any other part takes `platform`, a path relative to the board file or a
Renode platform (`"platforms/cpus/stm32l072.repl"`). The core's name comes
from the platform: `cpu` on all three table entries, the first CPU entry of
a `platform` file.

The family's interfaces and internal peripherals are the platform's names
(`usart2`, `can1`, `fdcan1`, `i2c4`, `spi3`, `adc4`, `rtc`), and its pins
`pa[0]` to `pk[15]`. The three firmware forms:

- `"build/app.elf"`: a prebuilt ELF, relative to the root.
- `command(["cmake", "--build", "--preset", "app"], output:
  "build/app/app.elf")`: run in the board's root once per process before
  the first boot; `output` is the ELF it leaves, relative to the root.
- `build("firmware/build.py", sources: "firmware", output: "app.elf")`: a
  Python script run as `build.py <output-dir> --cc <gcc>` and the directory
  it reads, both relative to the board file. The ELF is cached, keyed by
  the sources and the compiler's version line.

One boot mode, `flash`: the ELF's segments load at their physical
addresses, as a debugger loads them, and the core starts from the vector
table. A reset restarts the core from the vector table with flash intact.
On the STM32U5 platforms the RCC records why the core last reset, as the
firmware reads it from RCC_CSR: `board.reset()` reaches the firmware as a
reset-pin cause, a power cycle as power-on. The RTC and TAMP keep their
contents through system resets; power-on and a backup-domain reset clear
them, as on a board without a backup battery.

## Peripherals

```jig
peripheral flash0: "SPI.Micron_MT25Q" (size: 32 MiB)
peripheral temp: "Sensors.TI_TMP108"
```

The string is a model class in Renode's spelling, or one of the board's
models. The arguments are the model's properties and constructor
parameters with units, so `32 MiB` reaches Renode as `0x2000000`. A
peripheral is wired by the links that name it, and one no link names is an
error. In the machine's monitor a peripheral is `<controller>.<name>`,
such as `qspi.flash0`.

## Links

`link NAME: KIND(arguments)`, with `external` in front for a link that
leaves the board.

```{list-table}
:header-rows: 1
:widths: 13 22 65

* - Kind
  - Settings
  - Ends
* - `uart`
  - `baud`; `frame`, `"8N1"` by default
  - `members`: one or two device interfaces
* - `can`
  - `bitrate`
  - `members`: device interfaces
* - `spi`, `qspi`
  -
  - `controller`, a device interface; `targets`, a map from chip select to
    a peripheral
* - `i2c`
  -
  - `controller`; `targets`, a map from 7-bit address to a peripheral
* - `gpio`
  - `pull`, `"up"` or `"down"`
  - `from`, one output; `to`, an input or a list of inputs
```

- A link is a net or a bus on the schematic: give it the net's name. Plans
  and systems use it.
- A device interface or a pin belongs to one link at most.
- The physical layer's settings belong to the link, since both ends must
  agree on them.
- A device can't be an SPI or QSPI target. Renode's SPI interface returns
  the reply byte inside the controller's own call (`byte Transmit(byte
  data)`), and none of the SPI models of these families has a target mode,
  so a link between two machines would need new models on both ends. A
  board file that makes a device a target is an error, and so is a device
  as an I2C target in this release.
- An external UART link gets a console in every run, recorded from the
  start. An external CAN link joins the harness only through a system
  link; one that no system link names stays unconnected.

Pins follow Renode's conventions: outputs by the model's GPIO property
name (`chip.IRQ`), inputs by number (`chip.gpio[0]`). A pull on a
Zynq-7000 pin sets the pin's level until something drives it (the GPIO
model's `SetPull`); on an STM32 pin, `"up"` drives the input high until the
other end changes it.

## Sensors

```jig
sensor supply_imon: adc.channel[3]
sensor cpu_temp: mcu.adc4.channel[13] = 736 mV
```

A sensor is an analog input a plan drives, in volts at the converter's
pin; its name is what `board.set_sensor`, `board.sensor_code` and
`board.drive` take. A value after `=` holds the input from the start of
the run. A converter that implements Jig's analog-input contract takes the
voltage through `SetChannelVoltage`. A converter of Renode's own on an
STM32 gets an `Analog.ADCChannelSource` on the channel, which the
machine's monitor calls `<converter>.<sensor>_input` (`adc1.rail_input`),
and reports no codes to the harness. In Renode 1.17.0 the converters
built on `STM32_ADC_Common` take one: the STM32F0, F3, G0, H7, L5 and WBA
models. Renode's STM32F4 platform has no converter, and ADC4 on Jig's
STM32U5 platforms is Jig's model.

## Protocol stacks

`over` stacks layers, top first: `csp over kiss` is CSP packets in KISS
frames, and the bottom layer sits on the interface. A layer named alone
takes its defaults: `kiss` means `kiss()`.

```{list-table}
:header-rows: 1
:widths: 10 25 20 45

* - Layer
  - Runs on
  - Provides
  - Arguments
* - `kiss`
  - a byte stream
  - packets
  -
* - `cfp`
  - CAN frames
  - packets
  -
* - `csp`
  - packets
  - CSP services
  - `address`; `housekeeping: {port, request, codec, ...}`; `rdp:
    {window_size, conn_timeout, packet_timeout, delayed_acks, ack_timeout,
    ack_delay_count}`, with the times in units (`ack_timeout: 100 ms`)
* - `vt100`
  - a byte stream
  - terminal text
  -
* - `cli`
  - terminal text or a byte stream
  - commands and replies
  - `eol`: `"\r"` (the default), `"\n"` or `"\r\n"`; `prompt`, text or a
    regex
```

A UART interface gives a byte stream and a CAN interface gives frames.
`jig check` rejects a stack whose layers don't fit and names the missing
one: "csp needs packets and uart0 gives a byte stream: put kiss between
them". Plugins add layers with `api.protocol`.

`board.cli`, `board.send` and `board.expect` use the board's one interface
whose stack has `cli`, ending lines with its `eol`. With a `prompt`,
`board.cli` returns when the prompt comes back, waiting up to 10 s of
simulated time, instead of when the console has been quiet for `settle`
seconds: `cli(prompt: "zed> ")`. With `vt100` under `cli`, the console
reads what the board sent as a terminal shows it, and a plan can press
keys by name ([Consoles that run vt100](library.md#consoles-that-run-vt100)).

The CSP layer's housekeeping table names the port `node.hk(board)` asks,
the request payload (bytes, hex digits, a byte or a list of bytes) and a
codec a plugin registered with `api.hk_codec`; any other key reaches the
codec as an option. Its RDP options override libcsp 1.6's defaults for
connections to the board, and a node's declaration overrides them.

## Programmable logic

`ip` lines put models in the programmable logic of a Zynq-7000. With an
XSA that holds a block design, Jig writes a model for each AXI GPIO, AXI
Quad SPI, AXI CDMA and AXI BRAM controller in it, and an `ip` line either
replaces one of those models or places a new block behind ports the design
exports; {doc}`programmable_logic` says what Jig reads from the design and
writes. Without a block design, either because the device has no XSA or
because its XSA holds only `ps7_init` and a bitstream, the `ip` lines are
the whole of the programmable logic:

```jig
ip block: "Example.Block" (on: 0x43C00000 .. 0x43C00FFF, irq: 0,
    registerMap: airhdl("hdl/regs_pkg.sv", "hdl/regs.sv"))
ip buffer: memory(on: 0x40000000 .. 0x4000FFFF)
ip filter: cosim(on: 0x43C10000 .. 0x43C10FFF, irq: 1,
                 build: "cosim/build.sh", sources: "cosim/sources.f",
                 library: "libfilter.so", frequency: 100 MHz)
```

- `on` places the block: an AXI port the block design exports (`on:
  M00_AXI`), a map from such ports to the window each takes (`on:
  {M00_AXI: 4 KiB}`), or an address range, the one form a device without
  a block design takes. An `ip` line named after an instance of the design
  takes no `on`: it keeps the instance's address.
- `irq` connects the model's `IRQ` output to an interrupt port the block
  design exports, or to an `IRQ_F2P` line from 0 to 15. Lines 0 to 7 are
  shared peripheral interrupts 61 to 68 and lines 8 to 15 are 84 to 91
  (UG585 v1.7 table 7-3).
- `memory(size: ...)` is block RAM; with an address range in `on`, the
  size is the range's. Jig's model of the PL clears it on a system reset
  and whenever the PL is configured again, since block RAM loses its
  contents then (UG585 section 26.2).
- `airhdl(package, module)` makes a property value from airhdl sources:
  Jig generates the JSON register map each run and fills the property in.
- `cosim(...)` puts RTL under Verilator in place of a model
  ({doc}`../how_to/cosimulate_rtl`): `build`, `sources`, `inputs`,
  `library` and `context`, plus the co-simulated peripheral's properties,
  such as `frequency`, the clock the RTL is built for. Its `irq` also sets
  the peripheral's `cosimToRenodeSignalRange`.
- A property value can name a peripheral of the board or of the platform
  (`gic`), another `ip` line, or an instance of the block design that has
  a model: `bram: axi_bram_ctrl_0`.

## The platform description Jig writes

Jig writes each machine's Renode platform description when a run starts,
to `<machine directory>/machine.repl`, in this order:

1. the family's platform: `xilinx/zynq7000`, `st/stm32u575`, `st/stm32u595`
   or Renode's STM32F4 platform;
2. what the device's arguments fix: PS clocks, boot straps, DDR, a part's
   memory sizes, the core clock;
3. the programmable logic: the XSA's block design with the device's `ip`
   lines applied, or the `ip` lines alone;
4. peripherals at their controllers (`@ spi0 1`, `@ i2c4 0x40`), GPIO
   connections and pulls, and sensor inputs;
5. unmodelled ranges and property assignments;
6. the board's `repl` fragments;
7. what Jig adds per run: CPU performance and register maps.

`jig platform BOARD [--option NAME=VALUE] [-o FILE]` prints it, so a
board's author can read what Renode loads.

## Power

A power cycle halts the board's cores and restarts every peripheral whose
model implements Jig's power-cycle contract, except those on
`backup_supply`. Jig asks the machine which models implement it once per
run, so the board file names none.

## Files checked before a run

Before Renode starts, Jig checks that every file each board of the plan
needs exists: its `repl` fragments, its `airhdl(...)` sources and
co-simulation inputs, the images its storage starts from, every firmware
file and the XSA. A `command(...)` or `build(...)` firmware makes its ELF
at the first boot, so it has no file to check yet. A board that starts
without power is checked too, since a plan can power it on. A missing file
stops the run with exit code 2, naming each one.

## From board.yaml

Jig 0.2 reads no board.yaml. Each schema 2 key has a new place:

```{list-table}
:header-rows: 1
:widths: 40 60

* - board.yaml
  - Board file
* - `schema`, `name`
  - `board "name"`
* - `description`, `root`, `models`, `plugin`, `rtc`
  - Settings of the same names.
* - `soc`, `soc_platform`
  - The device's family and `part`.
* - `soc_driver`
  - The device's `driver` setting.
* - `platform`, `clocks`
  - The description Jig writes; `repl` for hand-written fragments.
* - `performance_mips`
  - The device argument `mips`.
* - `zynq7000:` `xsa`, `ps_clk_hz`, `ps_version`, `straps`
  - The device arguments `xsa`, `ps_clk`, `ps_version`, `straps`.
* - `stm32:` `firmware`, `cpu`, `vector_table`
  - The device's `firmware` and its `vector_table` argument; the family
    knows its core's name.
* - `firmware`, `boot`
  - The device's `firmware` and `boot` lines.
* - `storage`
  - `storage: [...]`; sizes come from the peripherals.
* - `interfaces`
  - External links, with `interface` lines for their stacks; `eol` moves
    to `cli`.
* - `sensors`
  - `sensor` lines.
* - `power.restart`, `power.initial`
  - Peripherals that implement the power-cycle contract restart unless on
    `backup_supply`; the `power` setting.
* - `register_maps`
  - `airhdl(...)` property values.
* - `options`
  - `option` lines; a choice's platform fragment, register maps, cosim
    and commands become declarations under `if`.
* - `csp`
  - `protocol csp(...)` in the device.
```
