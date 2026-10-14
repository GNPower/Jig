# Changelog

Every release of Jig is listed here, ordered as newest first.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Breaking changes open with **Breaking** under Changed.

## [Unreleased]

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
  boot; with the STM32U5 RCC, a Jig reset reads as the reset pin and a
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

[Unreleased]: https://github.com/GNPower/Jig/commits/main
