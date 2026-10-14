# A first run

This page runs the ZedBoard example: two emulated ZedBoards on one CAN bus,
and a ground station that reaches them over a serial link to the first. It
assumes Jig and Renode are installed ({doc}`installation`, {doc}`renode`)
and that `arm-none-eabi-gcc` is on `PATH`.

## The example system

```text
ground station (10) <-- KISS, UART0, 115200 baud --> zed-a (1)
zed-a (1) <-- CAN0, 1 Mbit/s --> zed-b (2)
```

Both boards run the same bare-metal firmware on Cortex-A9 core 0, built
twice with a different CSP address. zed-a forwards packets between its
serial link and the bus, so the ground station reaches zed-b through it.
Each board has a console on UART1 with the prompt `zed>`. Each serves CSP
ping on port 1, uptime on port 6 and a status record on port 10. The
example's `README.md` gives the wire formats and the status layout.

The example is a Jig project of its own, in `examples/zedboard`:

```{list-table}
:header-rows: 1
:widths: 35 65

* - Path
  - Contents
* - `jig.toml`
  - The project file: the board root `boards`, the plan library `lib`,
    and the Renode versions the example accepts.
* - `boards/zed-a/board.yaml`, `boards/zed-b/board.yaml`
  - The two board definitions.
* - `boards/zedboard.py`
  - The board plugin both definitions name: the status record's decoder,
    the plan function `cfp_frames` and two board members.
* - `zedboard.repl`
  - The ZedBoard's clocks, boot straps and DDR, on top of the Zynq-7000
    platform Jig ships.
* - `lib/zed.jig`
  - Procedures the plans share.
* - `plans/`
  - `one_zedboard.jig`, `two_zedboards.jig` and `profiles_timelines.jig`,
    which the tutorials build, and `multiboard.jig`.
* - `firmware/`
  - The firmware's sources and `build.py`.
```

## Build the firmware

From a clone of the repository:

```bash
git clone https://github.com/GNPower/Jig.git
cd Jig
python examples/zedboard/firmware/build.py
```

The script compiles `examples/zedboard/firmware/src` once per board and
writes `zed-node-1.elf` (zed-a) and `zed-node-2.elf` (zed-b) to
`examples/zedboard/firmware/build`, where the board definitions look for
them. It takes the compiler from `--cc`, then from `ARM_NONE_EABI_GCC`,
then from `PATH`, and fails on any compiler warning. On Ubuntu 24.04,
`sudo apt-get install gcc-arm-none-eabi` installs GCC 13.2.1, which builds
both ELFs without a warning:

```text
compiler: /usr/bin/arm-none-eabi-gcc
          arm-none-eabi-gcc (15:13.2.rel1-2) 13.2.1 20231009
   text    data     bss     dec     hex filename
  12972      88   37184   50244    c444 .../firmware/build/zed-node-1.elf
  11372      44   33836   45252    b0c4 .../firmware/build/zed-node-2.elf
```

Without the ELFs, `jig check` reports each one as not found, and
`jig run` stops before it starts Renode, with exit code 2 and one line per
missing file under `ABORTED: 2 files the boards need are missing:`, such as
`a: firmware app .../zed-node-1.elf not found (build the firmware first)`.

## Run a plan

```bash
jig run examples/zedboard/plans/multiboard.jig
```

Jig finds the example's project file, `examples/zedboard/jig.toml`, by
searching up from the plan's directory, and the board definitions `zed-a`
and `zed-b` through it. It starts Renode, builds one machine per board,
loads each ELF the way a JTAG debugger would, connects both CAN controllers
to one bus, and attaches a harness CSP node to zed-a's serial link as the
ground station. Then it runs the plan's setup block and each test in
order, all in one emulation.

```text
PLAN  examples\zedboard\plans\multiboard.jig
  board a: zed-a, jtag boot, 100 MIPS
  board b: zed-b, jtag boot, 100 MIPS
  bus can0: CAN 1e+06 bit/s, a.can, b.can
  node ground: CSP 10 on link a.kiss
  PASS  the ground station reaches zed-b through zed-a  (0.01 s)
  PASS  the ground station pings both boards  (0.02 s)
  PASS  the boards ping each other from their consoles  (0.01 s)
  PASS  a 200-byte ping crosses the gateway in fragments  (0.05 s)
  PASS  both boards answer with their status  (0.03 s)
  PASS  the counters agree with what the bus carried  (0.03 s)
  PASS  pings stop during a link outage and resume after it  (4.81 s)
  PASS  zed-b waits for its debugger after a power cycle  (1.06 s)
  8 passed, 0 failed, 0 errors (6.0 s simulated, 17.7 s wall); fingerprint 7e336f8fcf0140a9
```

That run was on Windows, and two log lines about Renode's start, which
name the Renode found and the run directory, are left out after the first
line. The output names the plan, then lists the system: one line per
board, bus and node. Each test follows on a line of its own, with its
status (`PASS`, `FAIL`, `ERROR` or `SKIP`) and the simulated seconds it
took; a test that did not pass prints the reason below it, such as the
failed check with the values on both sides. The last line counts the
outcomes, gives the simulated and the wall time, and prints the first 16
hex digits of the run's fingerprint; `-v` before `run` also prints every
step as it happens.

The fingerprint covers everything the run decided and measured in
simulated time, so it depends on what the firmware does and on nothing
about the host. With ELFs built by GCC 13.2.1, `multiboard.jig` gives
`7e336f8fcf0140a9` on Windows and on Linux. ELFs from GCC 12.2 (Vitis
2024.1) differ from those byte for byte and give the same fingerprint for
each of the example's plans, because the firmware acts on its 1 ms timer
tick; a change to what the firmware does or when, a different message or
an answer in another tick, gives another fingerprint.

`jig run` exits with 0 when every test passed, 1 when a test failed, 2 for
a mistake in the plan or a configuration file or a file a board needs that
is missing, and 3 when Renode, a model compile or a firmware build failed
({doc}`../user_manual/results`).

## Reports and repeated runs

```bash
jig run examples/zedboard/plans/multiboard.jig --html out/report.html
jig run examples/zedboard/plans/multiboard.jig --repeat 3
jig run examples/zedboard/plans/*.jig
```

`--html` writes one page with every step of every test, plots of recorded
values, and links to the console transcripts. `--junit PATH` writes JUnit
XML for CI and `--json PATH` a summary of every plan run. `--repeat 3` runs
the plan three times and fails unless all three runs have the same
fingerprint. A wildcard runs every plan of the example, one emulation each.

Every run also leaves a directory in the project, under
`examples/zedboard/out/runs/`, named after the UTC time and the plan. It
holds each console's transcript (`a/console.log`), Renode's log
(`renode/renode.log`) and the recorder's files: events, steps, recorded
values (`telemetry.csv`) and the CSP packets seen on each bus
(`can0.packets.csv`).

## Check the board definitions

`jig boards`, `jig check` and `jig boot` find the project from the working
directory, so run them inside the example:

```bash
cd examples/zedboard
jig boards
jig check
jig check --contracts
```

`jig boards` lists `zed-a` and `zed-b` with the file and the search-path
entry each came from. `jig check` loads each definition and its plugin and
reports what is missing, such as an ELF not built yet; both boards print
`ok`. `--contracts` also builds each board's machine in Renode, without
booting it, and checks the peripherals board.yaml names against the
interfaces Jig calls on them.

## Watch a board boot

`jig boot` starts one board and copies its console to the terminal:

```bash
jig boot zed-a --seconds 2
```

<!-- The banner's links line is 104 columns, as the firmware prints it. -->
<!-- markdownlint-disable MD013 -->
<!-- pyml disable md013-->

```text
zed-a: CSP address 1 on a ZedBoard (Zynq-7020, Cortex-A9 core 0)
links: can0 1000 kbit/s, CSP with 29-bit CFP identifiers; kiss on uart0 115200 baud to ground station 10
console: uart1 115200 baud, type help
zed>
--- 2 s simulated in 0.5 s (4.00x real time); total 6.1 s
```

<!-- pyml enable md013-->
<!-- markdownlint-enable MD013 -->

zed-a prints its banner, naming its CSP address and its links, then the
prompt. The command needs no plan, which makes it the first check for a new
board definition.

Next, {doc}`../tutorials/one_zedboard` writes a plan from the start.
