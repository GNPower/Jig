# What Jig is

Jig is a test harness for firmware that runs on emulated boards. You write a
plan in the Jig language, name the boards it needs, and run it with
`jig run`. Jig starts Renode, builds one Renode machine per board from the
board's board file, loads the firmware, and executes the plan against the
running system in simulated time.

## A plan and its system

A plan file (`.jig`) declares a system and the blocks that test it:

- `board` instances, each built from a board file (`board.jig`, in the
  same language) that describes the board's device, the chips wired to
  it, the firmware and the links a plan talks to, and from which Jig
  writes the Renode platform description;
- `link` declarations that join boards' CAN links on one CAN bus, or put
  a harness node on a board's serial link;
- `node` declarations for protocol endpoints the harness runs itself, such
  as a CSP host computer with its protocol stack (`csp(address: 10) over
  kiss`);
- `setup`, `test` and `teardown` blocks of statements: console commands and
  expectations, CSP requests, `check` conditions, waits, loops, parallel
  tasks and recorded measurements.

Each `jig run` gives each plan a fresh emulation. The tests inside one plan
run in order and share it, so a test starts where the previous one left the
boards.

## Simulated time

Every duration in a plan is simulated time. `wait 2 s` advances the
emulation by two seconds of the boards' time, and a console expectation
with `within: 5 s` times out after five simulated seconds, however long the
host takes to compute them. The harness advances the emulation in steps of
at most 10 ms and delivers the UART and CAN traffic of each step before the
next one starts, so a slow host changes the wall time of a run and nothing
else.

Each run gets a fingerprint: a SHA-256 digest of every status, step,
simulated time stamp and recorded value, and of nothing that depends on
the host. `jig run --repeat 3` runs a plan three times and fails
if the fingerprints differ. {doc}`../user_manual/time` explains how Jig
keeps several boards repeatable.

## What runs where

Renode runs the firmware. Its CPU models execute the firmware's
instructions, and its peripheral models, written in C#, stand in for the
SoC's controllers and the board's chips. Jig adds the models Renode lacks
for the parts it supports and Renode compiles them when a session starts,
so you need no .NET SDK.

Jig itself runs in Python: the plan interpreter, the protocol endpoints
(CSP nodes, KISS framing, the CAN bus adapter), the recorder and the report
writers. It controls Renode through a bridge that listens on the loopback
interface only and checks a token created for each run
({doc}`../user_manual/bridge`).

## Supported parts and protocols

Jig's Zynq-7000 driver boots a board the way a JTAG debugger does (the ELF
loaded into memory, the core started at its entry point) or through the
BootROM model from a Quad-SPI flash image. Jig ships models for the
Zynq-7000 processing system (SLCR, device configuration, Quad-SPI, CAN,
GPIO, DDR controller, eFuse, the BootROM), for the Cadence UART, SPI and
watchdog controllers it contains, for the Cortex-A9 timers and the PL310
cache controller, and for the Xilinx AXI GPIO, AXI CDMA and AXI Quad SPI
cores.

The STM32 driver loads an ELF at its physical addresses and starts the core
from the vector table, on Renode's STM32F4 platform or Jig's STM32U575
and STM32U595 platforms. A board plugin can register a driver for any
other device family.

The bundled CSP extension speaks the libcsp 1.6 wire format: the 32-bit
CSP header, CRC-32C on KISS frames, CFP fragmentation on CAN, and RDP
connections. Plain consoles need nothing beyond a UART.

## Limits of emulation

Renode measures a core's time by the instructions it executes, at the
`mips` rate of the device in the board file. It does not model caches
or pipelines cycle by cycle, so a plan can test what firmware does and the
order it does it in, and timing only to the accuracy of that rate and of
the peripheral models. Analog inputs exist as far as a model exposes them:
a sensor input is a voltage at a converter pin, set by the plan. Jig drives
no real hardware.
