# Jig

[![PyPI version](https://img.shields.io/pypi/v/jig-dsl.svg)](https://pypi.org/project/jig-dsl/)
[![Python versions](https://img.shields.io/pypi/pyversions/jig-dsl.svg)](https://pypi.org/project/jig-dsl/)
[![Tests](https://github.com/GNPower/Jig/actions/workflows/ci.yml/badge.svg)](https://github.com/GNPower/Jig/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/GNPower/Jig/branch/main/graph/badge.svg)](https://codecov.io/gh/GNPower/Jig)
[![Documentation](https://readthedocs.org/projects/jig/badge/?version=latest)](https://jig.readthedocs.io/en/latest/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/GNPower/Jig/blob/main/LICENSE)

**A test language and harness for firmware running on the Renode
emulator.**

Full documentation: <https://jig.readthedocs.io>

## Overview

Jig runs test plans, written as `.jig` files, against a user's firmware
on [Renode](https://renode.io). A plan declares the boards it needs, the
links between them and the protocol nodes the harness runs, then drives
the system over those links in simulated time:

```jig
import "zed.jig"

system "two ZedBoards and a host computer" {
    board a: "zedboard" (node: 1)
    board b: "zedboard" (node: 2)
    link can0: can(bitrate: 1 Mbit/s, members: [a.can, b.can])
    node host: csp(address: 10) over kiss
    link ground: uart(members: [a.kiss, host])
}

setup {
    parallel {
        task zed_a { booted(a, 1) }
        task zed_b { booted(b, 2) }
    }
}

test "the host computer reaches zed-b through zed-a" {
    check host.ping(b) < 50 ms
    check can0.traffic().routes["10->2:1"] >= 1
}
```

This is the start of `examples/zedboard/plans/multiboard.jig`. Both
boards are ZedBoards; the `node` option picks the firmware build each one
runs, zed-a or zed-b. The host computer is a CSP node the harness runs on
zed-a's KISS serial link. The setup waits, on both boards at once, for the
boot banner and the first prompt (`booted` comes from the example's plan
library, `lib/zed.jig`). The test then pings zed-b from the host computer
through zed-a using the lightweight network protocol
[LibCSP](https://github.com/libcsp/libcsp).

Every check's result is printed in a report with the simulated time it ran
at. Each run gets a fingerprint, a SHA-256 digest of everything it decided
and measured in simulated time, so `jig run --repeat 3` proves a plan gives
the same result three times, on any host.

Boards are described in the same language: a `board.jig` file declares
the board's device (in this case the [ZedBoard][zedboard]'s Zynq-7020
SoC), the chips wired to it, the links that leave the board, the protocol
stacks its firmware runs, and the firmware. Jig writes the Renode platform
description from it, and `jig platform` prints it. Users must bring their
own C# models and Python plugins for any peripherals not directly
supported by Jig.

[zedboard]: https://www.avnet.com/americas/products/avnet-boards/avnet-board-families/zedboard/

## Requirements

- Python 3.11 or later.
- Renode 1.17.0. Jig finds an installed copy, or `jig renode install`
  downloads the release from Renode's GitHub page after you agree, and
  checks its size and SHA-256 digest.
- For the ZedBoard example's firmware, `arm-none-eabi-gcc`.
- For RTL co-simulation, Linux with Verilator.

## Installation

```bash
python -m pip install jig-dsl
jig renode install
jig doctor
```

## Quick Start

The repository's ZedBoard example runs two Zynq-7020 boards on one CAN
bus, with a host computer on a serial link to the first:

```bash
git clone https://github.com/GNPower/Jig.git
cd Jig
python examples/zedboard/firmware/build.py
jig run examples/zedboard/plans/multiboard.jig --html out/report.html
```

`build.py` needs `arm-none-eabi-gcc` on the `PATH`. The tutorials in the
documentation build plans like this one step by step.

## Features

- **A plan language for firmware tests**: consoles, network requests, checks,
  waits, loops, parallel tasks, timelines and recorded values, all in
  simulated time.
- **Several boards in one emulation**, on CAN and serial links the harness
  carries and watches, with repeatable timing between them.
- **Zynq-7000 and STM32 drivers**: JTAG-style and Quad-SPI boots through
  Jig's Zynq-7000 models, and ELF boots on Renode's STM32 platforms.
- **CSP in libcsp 1.6's wire format**: KISS and CAN links, RDP
  connections, and a bus observer that reassembles every packet.
- **Board plugins** through `jig.plugin`, a versioned API for plan
  functions, board members, decoders, protocol layers and device
  families.
- **C# models compiled by Renode at start-up**, with interfaces that board
  chip models implement so plans can drive them.
- **Reports for people and CI**: an HTML page with plots, JUnit XML, a JSON
  summary, run directories with every console transcript and packet, and
  exit codes that differentiate failed tests from broken setups.
- **RTL co-simulation** of programmable-logic blocks with Verilator.
- **A localhost bridge to Renode** with a token per run, in place of
  Renode's network-facing Robot Framework server.

## License

MIT: see [LICENSE](https://github.com/GNPower/Jig/blob/main/LICENSE).
