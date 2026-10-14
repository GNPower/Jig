# board.yaml reference

A board definition is a directory under a board root, named after the
board: a `board.yaml`, the Renode platform description it names, and the
files it refers to. This page describes schema 2, the only schema Jig
reads. {doc}`../how_to/add_a_board` walks through writing one.

## A complete example

zed-a, the first board of the ZedBoard example,
`examples/zedboard/boards/zed-a/board.yaml`:

```{literalinclude} ../../examples/zedboard/boards/zed-a/board.yaml
:language: yaml
```

## Paths

`root` names the board's root, relative to board.yaml. Without it, the
root is the nearest directory at or above board.yaml that holds a `.git`
file or directory, so a board inside a git submodule resolves against the
submodule's checkout; failing that, it is board.yaml's own directory.

- Relative to the root: `firmware`, `register_maps`, the files of an
  option's `cosim`, and files that SoC settings name, such as
  `zynq7000.xsa`.
- Relative to board.yaml: `platform`, an option's `platform`, `models` and
  `plugin`.

## Files checked before a run

Before Renode starts, Jig checks that every file each board of the plan
needs exists: its platform descriptions, the register-map sources and
co-simulation inputs of the options chosen, the images its storage starts
from and, when it boots, every `firmware` file plus the files its SoC
driver names: `stm32.firmware.elf` (a prebuilt ELF; the `command` and
`build` forms make theirs at the first boot) and `zynq7000.xsa`. A board
that starts without power is checked too, since a plan can power it on. A
missing file stops the run with exit code 2, naming each one.

## Top-level keys

Unknown keys are errors at the top level and in `interfaces`, `storage`,
`sensors`, `power`, `options` and the `csp` section, which the bundled CSP
extension defines. The keys under `<soc>`, `boot` and `firmware` go to the
SoC driver, which reads the ones it knows and ignores the rest, so a
misspelt key there has no effect.

```{list-table}
:header-rows: 1
:widths: 22 13 65

* - Key
  - Required
  - Meaning
* - `schema`
  - yes
  - `2`.
* - `name`
  - yes
  - The board directory's name.
* - `description`
  - no
  - One line.
* - `root`
  - no
  - The board's root (see Paths).
* - `soc`
  - yes
  - The SoC family: `zynq7000`, `stm32`, or one a plugin or an installed
    distribution registers.
* - `soc_driver`
  - no
  - The driver class as `module:Class`, where the module is importable or
    a `.py` file beside board.yaml.
* - `soc_platform`
  - no
  - A platform description Jig ships, loaded before the board's:
    `xilinx/zynq7000`, `st/stm32u575` or `st/stm32u595`.
* - `platform`
  - yes
  - The board's Renode platform description.
* - `performance_mips`
  - no
  - Virtual CPU speed in millions of instructions per second; 100 by
    default.
* - `<soc>`
  - per driver
  - Settings for the SoC driver, under the family's name (below).
* - `firmware`
  - per driver
  - Firmware files by role.
* - `boot`
  - no
  - `default`, the boot mode plans get unless they choose one: `jtag`
    when it is missing, so an STM32 board sets `flash`. For the Zynq-7000,
    `qspi.storage` names the storage device a Quad-SPI boot loads its image
    into, `flash0` by default.
* - `storage`
  - no
  - Non-volatile devices: `name: {peripheral, size}`, where `size` equals
    the model's `Size` (`jig check --contracts` compares them). The
    zynq7000 driver keeps one image file per device in each run, so writes
    survive resets, and a plan's `storage` setting gives initial images;
    the stm32 driver attaches none.
* - `interfaces`
  - no
  - The UARTs and CAN controllers plans use (below).
* - `sensors`
  - no
  - Analog inputs plans drive: `name: {adc, channel}`. The converter
    implements Jig's analog-input contract.
* - `rtc`
  - no
  - The real-time clock peripheral `board.set_time` and `board.time()`
    use; it implements Jig's real-time clock contract.
* - `power`
  - no
  - `initial`: `on` (the default) or `off`, the board's state when the
    plan starts. `restart`: peripherals that lose power with the board and
    implement `PowerCycle`.
* - `clocks`
  - no
  - `peripheral.property: clock` pairs that `jig check` compares with the
    clocks the hardware design's `ps7_init` sets, so they need
    `zynq7000.xsa`. The clocks are `ps_clk`, `arm_pll`, `ddr_pll`,
    `io_pll`, `cpu_6x4x`, `cpu_3x2x`, `cpu_2x`, `cpu_1x`, `lqspi_ref`,
    `uart_ref`, `spi_ref`, `can_ref`, `pcap` and `fclk0` to `fclk3`.
* - `register_maps`
  - no
  - Register maps generated from RTL for programmable-logic models:
    `peripheral.parameter: {package, module}`.
* - `options`
  - no
  - Choices a plan makes per board instance (below).
* - `models`
  - no
  - Globs of `.cs` files the board needs; each pattern must match at
    least one file, and only `.cs` files.
* - `plugin`
  - no
  - The board plugin: a `.py` file or a package directory.
* - `csp`
  - no
  - The CSP extension's section (below).
```

## Interfaces

```{list-table}
:header-rows: 1
:widths: 15 85

* - Kind
  - Keys
* - `uart`
  - `peripheral`; `baud`; `eol`, the line ending `send` and `cli` add:
    `cr` (the default), `lf` or `crlf`.
* - `can`
  - `peripheral`; `bitrate`.
```

The interface named `console` is the one `board.console` uses. Other names
are free: a plan reaches each interface as `board.<name>`, for `link:` and
`members:` in a system declaration.

## zynq7000 settings

Every setting is optional.

```{list-table}
:header-rows: 1
:widths: 22 78

* - Key
  - Meaning
* - `xsa`
  - The Vivado hardware design. With it, a JTAG boot replays the design's
    `ps7_init` and `ps7_post_config` register writes and configures the
    programmable logic with its bitstream, and `jig check` compares the
    board's clock properties with the clocks `ps7_init` sets. Without it,
    the SLCR keeps its power-on state and the firmware sets the clocks it
    uses, as the ZedBoard example's does.
* - `ps_version`, `ps_clk_hz`
  - The silicon version that selects `ps7_init`'s tables, and the PS_CLK
    frequency; both needed with `xsa`.
* - `straps`
  - The BOOT_MODE straps: `jtag`, `qspi`, `nor`, `nand` or `sd`. Without
    it, the straps stay as the platform description sets them.
```

Firmware roles: the driver loads `app`, an ELF, in a JTAG boot, and
`bitstream` into the programmable logic (else the bitstream inside the
XSA). Other roles, such as `fsbl`, are for the board plugin's image hooks;
`jig check` reports every role whose file is missing. Boot modes:

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
  board's plugin builds, loaded into the storage device `boot.qspi.storage`
  names (`flash0` by default); without a `storage` entry of that name the
  flash starts empty.

After a reset, the BootROM follows the straps, as on the board.

## stm32 settings

```{list-table}
:header-rows: 1
:widths: 30 70

* - Key
  - Meaning
* - `firmware.elf`
  - A prebuilt ELF, relative to the board's root.
* - `firmware.command`, `firmware.output`
  - A command (a list of arguments) run in the board's root once per
    process before the first boot, and the ELF it leaves, relative to the
    root.
* - `firmware.build`, `firmware.sources`, `firmware.output`
  - A Python script run as `build.py <output-dir> --cc <gcc>` and the
    directory it reads, both relative to board.yaml, and the ELF it
    writes, relative to the output directory. The output is cached, keyed
    by the sources and the compiler's version line.
* - `cpu`
  - The core's name in the platform description; `cpu` by default.
* - `vector_table`
  - Where the core reads its initial stack pointer and program counter;
    `0x08000000`, the start of flash, by default.
```

One boot mode, `flash`: the ELF's segments load at their physical
addresses, as a debugger loads them, and the core starts from the vector
table. A reset restarts the core from the vector table with flash intact.

`soc_platform: st/stm32u575` or `st/stm32u595` loads Jig's STM32U575 or
STM32U595 platform, the STM32U5 core and peripherals plus that part's
memories, with Jig's models of the RCC, LPTIM, FDCAN, RTC, TAMP and ADC4
({doc}`renode_defects` says what each fixes). The board's own platform
file then adds the board's chips and no `using` line of its own. On these
platforms the RCC records why the core last reset, as the firmware reads
it from RCC_CSR: `board.reset()` reaches the firmware as a reset-pin
cause, a power cycle as power-on, and resets the firmware or a watchdog
causes as those. The RTC and TAMP keep their contents through system
resets; power-on and a backup-domain reset (RCC_BDCR's BDRST) clear them,
as on a board without a backup battery. The platform's `rtc` and `adc4`
can serve board.yaml's `rtc` and `sensors` keys.

## Options

An option names a default and its choices. Each choice can add a platform
description fragment (loaded after the board's own file), register maps,
monitor commands run after loading (`{workdir}` expands to the machine's
work directory), and a co-simulated RTL model
({doc}`../how_to/cosimulate_rtl`):

```yaml
options:
  pl:
    default: model
    model:
      description: the register block as a C# model
      register_maps:
        block.registerMap: {package: hdl/regs_pkg.sv, module: hdl/regs.sv}
    rtl:
      description: the register block as Verilated RTL
      platform: rtl.repl
      cosim:
        peripheral: block
        build: cosim/build.sh
        sources: cosim/sources.f
        library: libblock.so
```

A plan picks a value in the board declaration, `board dut: "my-board"
(pl: "rtl")`, and `jig run --option pl=rtl` sets it for every board that
has the option, over the plan's choice. The report's system lines list
each option's value.

## The csp section

The bundled CSP extension reads this section.

```yaml
csp:
  address: 5
  housekeeping:
    port: 10
    request: "01"
    codec: zed-status
  rdp:
    window_size: 4
    packet_timeout_ms: 1000
```

```{list-table}
:header-rows: 1
:widths: 25 75

* - Key
  - Meaning
* - `address`
  - The board's CSP address, 0 to 31.
* - `housekeeping.port`
  - The port `node.hk(board)` sends its request to, 0 to 63.
* - `housekeeping.request`
  - The request payload: hex digits, a byte, or a list of bytes.
* - `housekeeping.codec`
  - A codec a plugin registered with `api.hk_codec`. Any other key under
    `housekeeping` reaches the codec as an option.
* - `rdp`
  - RDP options for connections to this board: `window_size`,
    `conn_timeout_ms`, `packet_timeout_ms`, `delayed_acks`,
    `ack_timeout_ms`, `ack_delay_count`. They override libcsp 1.6's
    defaults, and a plan's `csp(...)` declaration overrides them.
```

## Changes from schema 1

Jig no longer reads schema 1 and says so when it meets one.

- New keys: `root`, `soc_driver`, `soc_platform`, `models`, `plugin`,
  `power.initial` and a UART's `eol`.
- `plugin` replaces `dsl`; a plugin's entry point is `register(api)`
  instead of `register(define)`.
- Repository paths resolve against the board's root instead of the
  project root.
- `csp` belongs to the CSP extension, and `housekeeping` is a table of
  port, request and codec instead of a layout file.
- Unknown keys are errors.
