# Add a board

A board definition is a directory: a `board.yaml`, the Renode platform
description it names, and any files the YAML refers to, such as a board
plugin or C# models. Plans name the board by its directory's name. Adding
one changes nothing in Jig itself, unless the board's SoC family has no
driver yet.

## 1. Pick a board root

Jig looks for board directories under these roots, in order:

1. the `boards` list in the `[paths]` table of the project's `jig.toml`,
   or the project's `boards` directory when the list is absent;
2. the directories in `JIG_BOARD_PATH`, separated by the platform's path
   separator (`:` on Linux and macOS, `;` on Windows);
3. directories that installed Python distributions register under the
   `jig.boards` entry-point group.

The same name under two roots is an error that prints both paths.
`jig boards` lists every board Jig can see and the root it came from.

## 2. Describe the hardware for Renode

The platform description (a `.repl` file) adds the board to its SoC: the
memory fitted, the clocks the board's oscillator gives each peripheral,
the boot-mode straps, and the chips outside the SoC. For a Zynq-7000
board, `soc_platform: xilinx/zynq7000` in board.yaml loads Jig's
processing-system platform first, and the board's file only sets what
differs. This is part of the ZedBoard's:

```text
globalTimer:
    frequency: 333333333

uart1:
    clockFrequency: 50000000

can0:
    referenceClockFrequency: 100000000

slcr:
    bootMode: 0

ddr: Memory.MappedMemory @ sysbus 0x00100000
    size: 0x1FF00000
```

For an STM32U575 or STM32U595, `soc_platform: st/stm32u575` or
`st/stm32u595` loads Jig's platform for the part. Its RCC records reset
causes, so the firmware reads a Jig reset as a reset-pin cause and a power
cycle as power-on, and its RTC keeps the time through system resets. The
board's file then adds only the board's chips.

For another STM32 part, start from Renode's own platform description for
the chip, for example `using "platforms/cpus/stm32f4.repl"`, then add the
board's external chips.

## 3. Write board.yaml

A Zynq-7000 board that boots an application ELF over JTAG, with a console,
a CAN controller and a CSP address:

```yaml
schema: 2
name: my-zynq-board
description: Zynq-7020 board running the application over JTAG
soc: zynq7000
soc_platform: xilinx/zynq7000
platform: my-zynq-board.repl
performance_mips: 100
zynq7000:
  straps: jtag
firmware:
  app: build/app.elf
boot:
  default: jtag
interfaces:
  console: {kind: uart, peripheral: uart1, baud: 115200, eol: cr}
  can: {kind: can, peripheral: can0, bitrate: 1000000}
csp:
  address: 4
```

An STM32 board on Renode's `stm32f4.repl`, with prebuilt firmware:

```yaml
schema: 2
name: my-stm32-board
soc: stm32
platform: my-stm32-board.repl
performance_mips: 168
stm32:
  firmware:
    elf: build/app.elf
  vector_table: 0x08000000
boot:
  default: flash
interfaces:
  console: {kind: uart, peripheral: usart2, baud: 115200}
```

Paths in `firmware` resolve against the board's root: the nearest
directory at or above board.yaml that holds a `.git` file or directory (so
a board inside a git submodule resolves against the submodule), else
board.yaml's own directory, unless board.yaml sets `root`. `platform`,
`models` and `plugin` resolve against board.yaml's own directory.
{doc}`../user_manual/board_yaml` describes every key, including storage
devices, analog inputs, the real-time clock, power, board options and the
CSP section.

## 4. Check and boot it

```bash
jig check my-zynq-board
jig boot my-zynq-board --seconds 5
```

`jig check` reads the definition without running the firmware: it rejects
unknown top-level keys and missing files, and for a Zynq-7000 board with
a Vivado hardware design it compares the `clocks` pairs board.yaml lists
against the clocks the design sets. Keys under the SoC's own section, such
as `zynq7000`, are read by the driver, which ignores the ones it doesn't
know. `jig boot` builds
the machine, boots it and copies its console to the terminal for the given
number of simulated seconds.

## 5. Fill the gaps

A chip Renode has no model for needs one: {doc}`write_a_model`. Functions,
board members or decoders that plans should have for this board belong in
a board plugin: {doc}`write_a_plugin`.

A board on an SoC family that Jig has no driver for needs a driver class.
Name it in board.yaml as `soc_driver: drivers.py:MyDriver` (a module or a
`.py` file beside board.yaml, then the class), register it from the board's
plugin, or publish it under the `jig.socs` entry-point group. A driver
writes the platform overlay, configures the machine after its platform
loads, boots it in each boot mode it supports, and handles reset and power;
{class}`jig.system.socs.SocDriver` lists the methods. Jig's STM32 driver is
the shortest example: it loads an ELF at its physical addresses and starts
the core from the vector table, with one boot mode.

A driver can also define `required_files(config)`, returning the files a
boot needs that board.yaml's `firmware` doesn't list, as a mapping from a
description to a path. Jig checks them with the board's other files
before Renode starts, so a run without them stops at once with exit code
2. Jig's own drivers name `stm32.firmware.elf` and `zynq7000.xsa` this
way.
