# Add a board

A board is a directory holding a board file, `board.jig`, and any files it
refers to, such as a board plugin or C# models. Plans name the board by its
directory's name, which the file's `board "<name>"` line repeats. Adding
one changes nothing in Jig itself, unless the board's device family has no
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

## 2. Write the board file

A Zynq-7000 board that boots an application ELF over JTAG, with a console,
a CAN controller and a CSP address, in `boards/my-zynq-board/board.jig`:

```jig
board "my-zynq-board" {
    description: "Zynq-7020 board running the application over JTAG"

    device zynq: zynq7000(part: "XC7Z020-CLG484", straps: "jtag", ddr: 512 MiB,
                          clocks: {cpu_3x2x: 333.333333 MHz, cpu_1x: 111.111111 MHz,
                                   uart_ref: 50 MHz, can_ref: 100 MHz}) {
        firmware: {app: "build/app.elf"}
        boot jtag
        protocol csp(address: 4)
        interface uart1: cli
        interface can0: csp over cfp
    }

    external link console: uart(baud: 115200, members: [zynq.uart1])
    external link can: can(bitrate: 1 Mbit/s, members: [zynq.can0])
}
```

The device line describes the hardware: the part, the boot-mode straps,
the DDR fitted and the processing-system clocks the firmware programs.
Jig writes the Renode platform description from it, starting from the
Zynq-7000 platform it ships. With a Vivado hardware design,
`xsa: "hw/design.xsa", ps_clk: 33.333333 MHz, ps_version: 3` takes the
place of `clocks`, and Jig computes the clocks from the design's
`ps7_init`. The body says what the firmware runs: the ELF, the boot mode,
a CSP node with address 4, a command line on UART1 and CSP over CAN. The
two external links leave the board; plans and systems use their names,
`a.console` and `a.can`.

An STM32F407 board with prebuilt firmware and a console on USART2:

```jig
board "my-stm32-board" {
    device mcu: stm32(part: "STM32F407VG", clock: 168 MHz) {
        firmware: "build/app.elf"
        interface usart2: cli
    }

    external link console: uart(baud: 115200, members: [mcu.usart2])
}
```

The part picks Renode's STM32F4 platform; Jig's STM32U575 and STM32U595
platforms come the same way, from `part: "STM32U575..."`, and their RCC
records reset causes, so the firmware reads a Jig reset as a reset-pin
cause and a power cycle as power-on. Another STM32 part takes `platform:`,
Renode's platform description for the chip, such as
`"platforms/cpus/stm32l072.repl"`.

Chips on the board are peripherals wired by links. A NOR flash on the
Zynq-7000's Quad-SPI controller, kept in an image file between resets:

```jig
board "my-zynq-board" {
    storage: [flash0]
    device zynq: zynq7000(part: "XC7Z020-CLG484",
                          clocks: {cpu_3x2x: 333.333333 MHz,
                                   cpu_1x: 111.111111 MHz}) {
        firmware: {app: "build/app.elf"}
    }
    peripheral flash0: "SPI.Micron_MT25Q" (size: 32 MiB)
    link boot_flash: qspi(controller: zynq.qspi, targets: {0: flash0})
}
```

Firmware paths resolve against the board's root: the nearest directory at
or above the board file that holds a `.git` file or directory, else the
board file's own directory, unless the file sets `root`. `models`,
`plugin` and `repl` resolve against the board file's directory.
{doc}`../user_manual/board_files` describes every line, including sensors,
the real-time clock, power, options, protocol stacks and programmable
logic.

## 3. Check it, read its platform and boot it

```bash
jig check my-zynq-board
jig platform my-zynq-board
jig boot my-zynq-board --seconds 5
```

`jig check` reads the board file without Renode: it reports names that
clash, device arguments the family rejects, missing files, protocol stacks
that don't fit their interfaces, and any error writing the platform
description. `jig platform` prints the description Jig writes, which is
what Renode loads. `jig boot` builds the machine, boots it and copies its
console to the terminal for the given number of simulated seconds.

## 4. Fill the gaps

A chip Renode has no model for needs one: {doc}`write_a_model`. Lines the
notation can't express go in a hand-written fragment that `repl:
["extra.repl"]` loads after the description Jig writes. Functions, board
members, decoders or protocol layers that plans should have for this board
belong in a board plugin: {doc}`write_a_plugin`.

A device whose family Jig has no driver for needs a driver class. Name it
in the device as `driver: "drivers.py:MyDriver"` (a module, or a `.py`
file relative to the board file, then the class), register it from the
board's plugin with `api.device_family`, or publish it under the
`jig.device_families` entry-point group. A driver checks the device's
arguments, writes the family's part of the platform description, maps
pins to Renode connections, configures the machine after its platform
loads, boots it in each boot mode it supports, and handles reset and
power; {class}`jig.system.socs.SocDriver` lists the methods. Jig's STM32
driver is the shortest example: it loads an ELF at its physical addresses
and starts the core from the vector table, with one boot mode.

A driver can also define `required_files(config)`, returning the files a
boot needs that the device's `firmware` doesn't list, as a mapping from a
description to a path. Jig checks them with the board's other files
before Renode starts, so a run without them stops at once with exit code
2. Jig's Zynq-7000 driver names the device's `xsa` this way.
