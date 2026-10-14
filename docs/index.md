# Jig

**Jig** runs test plans, written in its own language as `.jig` files,
against unmodified firmware on the [Renode](https://renode.io) emulator. A
plan declares the boards, CAN buses and protocol nodes it needs, drives them
over UART and CAN in simulated time, and reports every check with the
simulated instant it ran at.

```{literalinclude} ../examples/zedboard/plans/multiboard.jig
:language: jig
:start-at: import "zed.jig"
:end-before: test "the ground station pings both boards"
```

The plan above, the start of the example's `multiboard.jig`, puts two
emulated ZedBoards (Digilent's board built on the Xilinx Zynq-7020) on one
CAN bus and a ground station on a serial link to the first. Its setup waits
for both boards' boot banners; its test checks that a CSP ping from the
ground station reaches the second board, and that the packet crossed the
bus. Nothing in it waits on the host's clock: `50 ms` is simulated time, so
the same plan gives the same result on a laptop and on a CI runner.

Jig ships on PyPI as `jig-dsl` for Python 3.11 and newer, and drives
Renode 1.17. Install it with `pip install jig-dsl`; the
{doc}`getting_started/installation` page explains why the bare name `jig`
installs something else.

## Where to start

- {doc}`getting_started/what_is_jig` describes what Jig does and what it
  leaves to Renode.
- {doc}`getting_started/installation` and {doc}`getting_started/renode`
  set up a machine; {doc}`getting_started/first_run` runs the ZedBoard
  example.
- {doc}`tutorials/index` build plans step by step on the ZedBoard example.
- {doc}`how_to/index` cover single tasks: adding a board, writing a C#
  model or a board plugin, running plans in GitHub Actions, co-simulating
  RTL.
- {doc}`user_manual/index` is the reference for the language, the board and
  project files, the command line and the reports.
- {doc}`api_reference/index` documents the Python modules a board plugin
  uses.

```{toctree}
:maxdepth: 2
:hidden:
:caption: Getting Started

getting_started/what_is_jig
getting_started/installation
getting_started/renode
getting_started/first_run
```

```{toctree}
:maxdepth: 2
:hidden:
:caption: Tutorials

tutorials/index
```

```{toctree}
:maxdepth: 2
:hidden:
:caption: How-To Guides

how_to/index
```

```{toctree}
:maxdepth: 2
:hidden:
:caption: User Manual

user_manual/index
```

```{toctree}
:maxdepth: 2
:hidden:
:caption: API Reference

api_reference/index
```

```{toctree}
:maxdepth: 1
:hidden:
:caption: Project

project/changelog
project/contributing
project/security
project/license
```
