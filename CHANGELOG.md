# Changelog

Every release of Jig is listed here, ordered as newest first.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Breaking changes open with **Breaking** under Changed.

## [Unreleased]

## [0.2.0a1] - 2026-11-01

### Added

- Board files, `boards/<name>/board.jig`, in the Jig language: settings
  (`description`, `root`, `models`, `plugin`, `rtc`, `storage`,
  `backup_supply`, `power`, `repl`), options declared with `choice(...)` or
  a plain value, the device with its family's arguments and its body
  (firmware, boot modes, protocol instances, interface stacks, `ip` lines,
  `unmodelled` ranges, property assignments, monitor commands, a driver),
  peripherals, links (`uart`, `can`, `spi`, `qspi`, `i2c`, `gpio`;
  `external` for one that leaves the board) and sensors. `if`, `elif` and
  `else` pick declarations by option values, strings see the options and
  `workdir`, and declarations can name what is declared below them.
- Jig writes each machine's Renode platform description from its board
  file, to `machine.repl` in the machine's run directory: the family's
  platform, the Zynq-7000 PS clocks from an XSA's `ps7_init` or from the
  `clocks` argument, boot straps, DDR, peripherals at their controllers,
  GPIO links and pulls, sensor inputs, `ip` lines with their `IRQ_F2P`
  interrupts, unmodelled ranges as silent tags, property assignments,
  `repl` fragments and the per-run overlay. A peripheral's monitor
  commands go into one `init add:` block. `jig platform BOARD [--option
  NAME=VALUE] [-o FILE]` prints it.
- The programmable logic of a Zynq-7000 comes from the block design in its
  XSA (`jig.ext.zynq7000.blockdesign`): a model for each AXI GPIO, AXI Quad
  SPI, AXI CDMA and AXI BRAM controller under its block-design name and at
  its address, interrupts traced through `xlconcat` blocks to `IRQ_F2P`,
  reset domains, AXI GPIO inputs driven by the FCLK resets, the Quad SPI's
  SCK from `C_SCK_RATIO` and the CDMA's reachable ranges from its address
  map. IP without a model becomes a silent tag. `ip` lines replace an
  instance's model or place a model behind ports the design exports. A
  device without a block design places them by address range. The
  design's instances and exported ports are names of the device: an AXI
  Quad SPI is an SPI link's controller (`zynq.axi_quad_spi_0`), and an `ip`
  line's property can name a block RAM (`bram: axi_bram_ctrl_0`). The user
  manual's programmable-logic page describes it.
- Protocol layers `kiss`, `cfp`, `csp`, `vt100` and `cli`, stacked with
  `over` in a device's `interface` lines and a node's declaration. A stack
  whose layers don't fit raises an error that names the missing layer: "csp
  needs packets and uart0 gives a byte stream: put kiss between them".
  Plugins add layers with `api.protocol`.
- The `vt100` layer renders what a board writes as a 24-row terminal shows
  it (`jig.transports.terminal`): cursor moves, saved positions, inserted,
  deleted and erased characters and erased lines take effect, and colors
  and other sequences drop out, so a console whose stack has `vt100`
  matches `expect`, `seen` and `cli` against the text a person at a
  terminal reads, and `cli(prompt: ...)` waits for the prompt on the
  cursor's line. Such a console adds `press(key)`, which sends one of 40
  named keys (the four arrows, `home`, `end`, `insert`, `delete`,
  `page_up`, `page_down`, `tab`, `enter`, `backspace`, `escape`, and
  `ctrl-a` to `ctrl-z`), `raw`, the bytes as the board sent them, and
  `raw_buffer`, the bytes since the last `clear()`, which keep the lines
  a terminal overwrites.
- The STM32 family's part table: `part` picks Renode's STM32F4 platform
  (with the F407's 128 KiB of SRAM1 and SRAM2, and the stock file's own
  tags in place of the SVD file its init downloads) or Jig's STM32U575 and
  STM32U595 platforms. `platform` takes another part's description. The
  core's name comes from the platform, `clock` sets the SysTick frequency
  on an STM32F407 and the DWT cycle counter's on an STM32U5.
- Sensors on a device's own converter (`mcu.adc4.channel[13]`), with a
  starting voltage (`= 736 mV`). A Renode STM32 converter gets an
  `Analog.ADCChannelSource`, and `set_sensor` returns `none` for it.
- `cli(prompt: ...)` in a stack: `board.cli` returns when the prompt comes
  back instead of after `settle` seconds of quiet.
- Map literals take integer keys, which stay numbers: `{0: flash0}`,
  `{0x68: rtc0}`.
- Board handles reach the board's devices (`a.zynq`: `name`, `family`,
  `monitor`), peripherals (`a.flash0`: `name`, `model`) and links by name.
  A CSP target can be a device.
- `jig check` checks the device's arguments, each protocol stack, names
  that clash with board members, and that the platform description can be
  written, it takes `--option NAME=VALUE`. `jig boards` prints each
  board's description and options.
- For plugins, a CSP node's handle keeps the link it joined as
  `node.link`: the system link's name and kind, the harness's port on it,
  the layers under the node and `board_links`, a `BoardLinkRef` for each
  board's link the system link joins, with the machine, the link's name
  and the device interfaces it joins (`interfaces`).
- `jig.testing`: the CAN links of a `FakeSimulation` are `FakeCanPort`s,
  which acknowledge the frames harness nodes write, keep them in `written`
  and hand them to the link's bus observers and nodes, so a plugin test
  runs a CSP node over `cfp` without Renode.

### Changed

- **Breaking**: board.yaml is removed. Jig reads board files only, and no
  converter is provided. The board-files page of the user manual maps each
  board.yaml schema 2 key to its new place.
- **Breaking**: the plan language is grammar version 3, and `over` is a
  reserved word.
- **Breaking**: a system declares `link` where it declared `bus`:
  `link can0: can(members: [a.can, b.can])`. A system link joins boards'
  external links and nodes. Its rate comes from the boards' links, and a
  rate it states must agree with them. A link inside a board, or a UART
  link between two boards, is refused in this release.
- **Breaking**: a node declares the layers under it and joins a link:
  `node host: csp(address: 10) over kiss` with
  `link ground: uart(members: [a.kiss, host])` replaces
  `csp(address: 10, link: a.kiss)`, and `over cfp` on a CAN link replaces
  `bus:`. A node that joins no link is an error.
- **Breaking**: CSP settings belong to the device,
  `protocol csp(address: 5, housekeeping: {...}, rdp: {...})`, in place of
  board.yaml's `csp` section. RDP times take units and lose their `_ms`
  suffixes (`ack_timeout: 100 ms`).
- **Breaking**: `board.console` is the board's external link named
  `console`, and `board.cli`, `board.send` and `board.expect` use the one
  external UART link whose interface's stack has `cli`, ending lines with
  that layer's `eol`. `board.interface(name)` is gone: plans name a link
  as `board.kiss`.
- **Breaking**: a power cycle restarts every peripheral whose model
  implements `IPowerCycled`, which Jig finds in the machine at run time,
  except those in `backup_supply`, board.yaml's `power.restart` list is
  gone.
- **Breaking**: `jig check` no longer compares hand-written clocks with a
  design's `ps7_init`, since Jig writes the clocks.
- **Breaking**: plugin API version 2 (`JIG_PLUGIN_API = 2`).
  `api.soc_driver` is now `api.device_family`. Node kinds lose their
  `link` and `bus` arguments: when the system starts, Jig calls the node
  handle's `start(system, link)` with the link the node joined. A board
  member can't be called `console`.
- **Breaking**: device families register under the `jig.device_families`
  entry-point group, which replaces `jig.socs`. A family's driver writes
  its part of the platform description (`platform_lines`) and checks the
  device's arguments (`arguments`), `jig.system.socs` lists the optional
  methods.
- **Breaking**: for plugins and drivers, `Board` keeps its fields for the
  board's one device, except `platform`, `clocks` and `soc_platform`, which
  are gone. `options` holds `OptionSpec` values, and option values keep
  their types. `MachineConfig.choices()` is gone.
- **Breaking**: the ZedBoard example has one board, `zedboard`, with
  `option node` choosing the firmware, in place of `zed-a` and `zed-b`.
  `zedboard.repl` is gone: `renode/two-boards.resc` loads the descriptions
  `jig platform` writes. The fingerprints of `two_zedboards.jig`,
  `profiles_timelines.jig` and `multiboard.jig` changed, because `booted`
  in `lib/zed.jig` matched the boot banner against the board's name and
  now matches the address alone, every other step is unchanged.
- Run reports write a CAN link's bit rate with its unit and a UART link's
  baud rate in whole bauds: `link can0: CAN 1 Mbit/s` where 0.1.0a2 wrote
  `bus can0: CAN 1e+06 bit/s`, and `UART 2000000 baud`. Errors about a
  link's rate write them the same way.

## [0.1.0a2] - 2026-10-14

### Changed

- The ZedBoard example calls CSP node 10 the host computer: plans name it
  `host`, the stand-alone tool is `tools/host.py`, and zed-a's console
  reports its KISS link as going to host computer 10. Each plan's
  fingerprint changed with the new test names.
- The documentation is at <https://jig.readthedocs.io>.

## [0.1.0a1] - 2026-10-13

### Added

- The Jig plan language for `.jig` files: system declarations, setup, test
  and teardown blocks, procedures, `parallel`, `race`, `timeline`,
  `every`, `try` and `catch`, units on numbers, regular expressions and
  byte literals. Imports resolve relative paths, the project's library
  path, `NAME:file.jig` in a board definition's `lib` directory and
  `std:file.jig` in Jig's standard library (`wait_ready`, `restart_via`,
  `counter`, `number_in`, `hex_dump_bytes`, `count_during`).
- Board power states: `power.initial` in board.yaml, `power: "on"` or
  `"off"` in a system declaration, and `power_on()`, `power_off()`,
  `power_cycle()` and `powered` on boards. `boot()` runs a board's boot
  flow again, as a debugger does for a JTAG-booted Zynq-7000 after a power
  cycle.
- Consoles and their transcripts hold everything a board printed from the
  start of the run, including output before a plan first reads them.
- Before Renode starts, Jig checks every file the plan's boards need
  (firmware, platform descriptions, register-map sources, co-simulation
  inputs, storage images, and the files SoC drivers name through
  `required_files`) and stops with exit code 2, naming each missing one.
- The `jig` command: `run`, `list`, `check` (with `--contracts`), `boot`,
  `boards`, `doctor`, `renode` (`install`, `list`, `path`, `remove`),
  `models` (`check`, `check --sdk`, `ide`) and `cosim build`, with exit
  codes that separate failed tests (1), configuration errors (2) and
  infrastructure failures (3). Every command that starts Renode takes
  `--renode PATH` and `--allow-untested-renode`, and `-v` and `-q` work
  before or after the subcommand.
- Project files: `jig.toml` with board roots, the library path, the Renode
  version and defaults, and an untracked `jig.local.toml` for local
  overrides.
- Board definitions in board.yaml schema 2, with a per-board root, C#
  model sets, plugins, SoC platforms Jig ships, power states and a board
  search path (`jig.toml`, `JIG_BOARD_PATH`, `jig.boards` entry points).
- Renode discovery from eight sources, and `jig renode install`, which
  downloads Renode 1.17.0 for Windows x64, Linux x64 and arm64 or macOS on
  Apple silicon only after a prompt or `--yes`, and checks each archive
  against a size and SHA-256 table. Jig accepts Renode `>=1.17.0,<1.18`.
- A localhost bridge to Renode with a random token per run, replacing
  Renode's Robot Framework server.
- C# models compiled by Renode at session start, then each board's model
  set against them: harness models, the Zynq-7000 processing system
  (SLCR, BootROM, Quad-SPI, device configuration, GPIO, CAN, DDR
  controller, eFuse, programmable-logic domain), the Cadence UART, SPI and
  watchdog, the Cortex-A9 global and private timers, GIC hooks and PL310,
  the Xilinx AXI GPIO, AXI CDMA and AXI Quad SPI cores, and the STM32U5
  RCC with reset causes, LPTIM, FDCAN with 29-bit extended filters,
  backup-domain RTC and TAMP, and ADC4.
- SoC platforms Jig ships: `xilinx/zynq7000`, `st/stm32u575` and
  `st/stm32u595`.
- Contract interfaces for board chip models (storage, power cycling,
  analog inputs, real-time clocks, SPI NOR flash), checked by
  `jig check --contracts`.
- SoC drivers for the Zynq-7000 (JTAG and Quad-SPI boot) and STM32 (ELF
  boot, with the STM32U5 RCC, a Jig reset reads as the reset pin and a
  power cycle as power-on), registered through the `jig.socs` entry-point
  group.
- The CSP extension: libcsp 1.6's header, KISS with CRC-32C, CAN
  fragmentation, RDP, connectionless packets, pings of any size, CMP
  requests, a bus observer and housekeeping codecs.
- The board plugin API, `jig.plugin` version 1.
- Reports: a terminal summary, HTML with plots, JUnit XML, a JSON summary,
  run directories, and fingerprints that make repeated runs comparable.
  Each report names the Renode version, build and runtime and the time
  each C# model set took to compile.
- Test doubles for plugin and library authors in `jig.testing`.
- `jig cosim build`, which builds an AXI4-Lite RTL block with Verilator
  5.020 or newer into a library Renode co-simulates.
- The ZedBoard example: firmware for two Zynq-7020 boards that speak CSP
  over CAN, with a ground station on a KISS serial link, as a Jig project
  with board definitions, a plugin and four plans.
- Documentation on Read the Docs, with a page on the Renode 1.17.0
  defects Jig works around, and a Pygments lexer for `.jig` files.
- Continuous integration: nox sessions, GitHub workflows for checks,
  releases through PyPI trusted publishing, and security scans, and
  Dependabot updates.

[Unreleased]: https://github.com/GNPower/Jig/compare/v0.2.0a1...HEAD
[0.2.0a1]: https://github.com/GNPower/Jig/compare/v0.1.0a2...v0.2.0a1
[0.1.0a2]: https://github.com/GNPower/Jig/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/GNPower/Jig/releases/tag/v0.1.0a1
