# ZedBoard example: CSP over CAN and KISS

Two Digilent ZedBoards (Xilinx Zynq-7020, 512 MB of DDR3) run the bare-metal
firmware in `firmware/` on Cortex-A9 core 0. They share one CAN bus, and a
host computer reaches zed-a over a KISS serial link; zed-a relays the host
computer's packets to zed-b and back. Every packet is CubeSat Space Protocol
(CSP) in libcsp 1.6's wire format: its header, its KISS framing with a CRC-32C,
and its CAN fragmentation protocol.

<!-- markdownlint-disable MD013 -->
<!-- pyml disable md013-->

```text
host computer (10) <-- KISS, UART0, 115200 baud --> zed-a (1) <-- CAN0, 1 Mbit/s --> zed-b (2)
```

<!-- pyml enable md013-->
<!-- markdownlint-enable MD013 -->

Everything here has run in Renode 1.17.0 on Jig's Zynq-7000 models. The firmware
has not run on ZedBoard hardware yet.

## Testing the boards with Jig

The directory is a Jig project: `jig.toml` names the board root `boards` and the
plan library `lib`. From the repository root, build the firmware, then run the
plans:

```bash
python examples/zedboard/firmware/build.py
jig run examples/zedboard/plans/*.jig
```

`jig run` finds `jig.toml` by searching up from each plan. `jig boards`,
`jig check`, `jig check --contracts` and `jig boot zed-a` search up from the
working directory, so run those inside `examples/zedboard`.

<!-- markdownlint-disable MD013 -->
<!-- pyml disable md013-->

| Path | Contents |
|---|---|
| `jig.toml` | the project: board root, plan library, accepted Renode versions (`>=1.17.0,<1.18`) |
| `boards/zed-a/board.yaml`, `boards/zed-b/board.yaml` | the board definitions (schema 2): Jig's `xilinx/zynq7000` platform plus `zedboard.repl`, JTAG boot without an XSA, the ELF, the console on UART1 (`eol: cr`), the KISS link on UART0 (zed-a only), CAN0, the CSP address and the status codec |
| `boards/zedboard.py` | the board plugin both definitions name |
| `lib/zed.jig` | `booted(board, address)`: wait for a board's banner and first prompt |
| `plans/one_zedboard.jig` | zed-a on its own: banner, console commands, uptime against simulated time, ping, the status record as bytes, uptime in whole seconds on port 6 |
| `plans/two_zedboards.jig` | both boards and the host computer: console pings over CAN, routed packets, a 200-byte ping in 26 frames each way, decoded status records, a ping nobody answers |
| `plans/profiles_timelines.jig` | a status poll every second, pings whose size follows a ramp, pings across a link outage |
| `plans/multiboard.jig` | both boards and the host computer: pings to 1 and 2, a 200-byte ping through zed-a, both status records, the frame and `routed` counters against what the bus carried, a link outage and recovery, a power cycle of zed-b |
| `tests/test_zedboard_plugin.py` | the plugin and the board definitions, without Renode: `python -m pytest examples/zedboard/tests` |

<!-- pyml enable md013-->
<!-- markdownlint-enable MD013 -->

The documentation's tutorials build the first three plans step by step, and its
first-run page runs `multiboard.jig`.

### The board plugin

`boards/zedboard.py` registers:

- the housekeeping codec `zed-status`, which each board.yaml names in
  `csp.housekeeping` with port 10 and the request `01`; `host.hk(a)` and
  `can0.hk(b)` return the status record below by field name;
- the plan function `cfp_frames(size)`, the CAN frames of a CSP payload of
  `size` bytes: 26 for 200 bytes, 33 for 256;
- the board member `uptime()`, seconds since boot from the console's `uptime`
  command.

A JTAG-booted Zynq that loses power comes back in its BootROM, waiting for the
debugger, because nothing on the board holds the application. So after
`b.power_cycle()` zed-b prints nothing and answers nothing until `b.boot()` runs
its boot flow again (for JTAG boot, the debugger's load), as `multiboard.jig`
checks.

### Firmware for the runs

The board definitions load `firmware/build/zed-node-1.elf` and `zed-node-2.elf`,
the files `firmware/build.py` writes, and Jig doesn't build them: run `build.py`
once before the plans and again after changing the sources. Without them
`jig check` reports each ELF as not found, and `jig run` stops before it starts
Renode, with exit code 2 and one line per missing file under
`ABORTED: 2 files the boards need are missing:`, such as
`a: firmware app .../zed-node-1.elf not found (build the firmware first)`. In CI
on Ubuntu 24.04:

```bash
sudo apt-get install --yes gcc-arm-none-eabi
python examples/zedboard/firmware/build.py
jig run examples/zedboard/plans/*.jig --repeat 3 --junit out/zedboard.xml
```

A run's fingerprint hashes what it decided and measured in simulated time, so it
depends on what the firmware does and when. With the ELFs Ubuntu 24.04's GCC
13.2.1 builds
(SHA-256 `ef3135ba80723e7c20eca1d6e7ff80128190d06a8573d516cba602e954c6c9e1` for
`zed-node-1.elf`,
`208bdc6e566e612e49d5ef14c14fac77f3ddebd8d7d7ba088914bd20969f40b9` for
`zed-node-2.elf`), each plan gives one fingerprint on Windows and on Linux:

| Plan | Tests | Simulated | Fingerprint |
|---|---|---|---|
| `one_zedboard.jig` | 6 | 4.0 s | `848445d787d95a77` |
| `two_zedboards.jig` | 5 | 0.6 s | `9b6d46e974fbfced` |
| `profiles_timelines.jig` | 3 | 23.4 s | `3afb28c2182a55f7` |
| `multiboard.jig` | 8 | 6.0 s | `32996f04f7e70f0d` |

ELFs from GCC 12.2 (Vitis 2024.1 on Windows) differ from these byte for byte
(`zed-node-1.elf` has 12928 bytes of text against 12968) and give the same four
fingerprints: the firmware acts on its 1 ms tick, and the instructions the two
compilers chose don't move any of its answers to another tick.

## Nodes

| Node | CSP address | Firmware | Links | Console |
|---|---|---|---|---|
| zed-a | 1 | `firmware/build/zed-node-1.elf` | CAN0; KISS on UART0 | UART1 |
| zed-b | 2 | `firmware/build/zed-node-2.elf` | CAN0 | UART1 |
| host computer | 10 | `tools/host.py` | KISS to zed-a | |

## Routes and services

zed-a sends packets for address 10 to KISS and every other address to CAN0.
zed-b sends everything to CAN0, so its answers to the host computer cross the
bus to zed-a, which forwards them on KISS. A node keeps packets for its own
address and for the broadcast address 31, and never sends a packet back out on
the link it arrived on.

Each node serves three ports:

| Port | Service | Request | Answer |
|---|---|---|---|
| 1 | ping | 0 to 256 bytes | the same bytes |
| 6 | uptime | empty | 4 bytes, big-endian: whole seconds since boot |
| 10 | status | 1 byte, 0x01 | 36 bytes, laid out below |

Port 6 counts seconds because libcsp 1.6 does: its `CSP_UPTIME` handler answers
with `csp_get_uptime_s()`. The status answer carries milliseconds.

An answer keeps the request's priority and its CRC32 option (header flag 0x01).
With that option the last 4 bytes of the payload are a CRC-32C of the rest; the
node checks them, drops the packet on a mismatch and adds a fresh CRC to its
answer. Packets asking for RDP, XTEA or HMAC (flags 0x02, 0x04, 0x08) get no
answer, and neither does a payload over 256 bytes.

### Status answer on port 10

All fields are big-endian.

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | magic, 0x5A454453 ("ZEDS") |
| 4 | 1 | layout version, 1 |
| 5 | 1 | node CSP address |
| 6 | 1 | CAN transmit error counter (ECR.TEC) |
| 7 | 1 | CAN receive error counter (ECR.REC) |
| 8 | 4 | uptime in milliseconds; wraps after 2^32 ms, 49.7 days |
| 12 | 4 | CAN frames sent |
| 16 | 4 | CAN frames received |
| 20 | 4 | KISS frames sent |
| 24 | 4 | KISS frames received with a good CRC |
| 28 | 4 | CSP packets forwarded between CAN and KISS |
| 32 | 4 | CRC errors: KISS CRC-32C and CRC32 option failures |

Python decodes it with `struct.unpack(">IBBBBIIIIIII", payload)`.

## Wire formats

All four follow libcsp 1.6.

- **CSP header**: one 32-bit word in network byte order. From the most
  significant bit: priority 2 bits, source address 5, destination address 5,
  destination port 6, source port 6, flags 8.
- **KISS** (UART0, 115200 baud, 8 data bits, no parity, 1 stop bit): FEND
  (0xC0), the command byte 0x00, the CSP header, the payload, a CRC-32C of the
  payload alone (big-endian), FEND. Inside a frame, 0xC0 travels as 0xDB 0xDC
  and 0xDB as 0xDB 0xDD. Every frame carries the CRC, whatever the header flags
  say.
- **CAN** (CAN0, 1 Mbit/s): CSP's CAN fragmentation protocol (CFP) on extended
  data frames. The 29-bit identifier holds, from the most significant bit:
  source 5 bits, destination 5, type 1 (0 on a packet's first frame, 1 on the
  rest), frames still to come 8, transfer identifier 10. The first frame carries
  the CSP header, the payload length (16 bits, big-endian) and the first 2
  payload bytes; each later frame carries up to 8, so a 200-byte payload takes
  26 frames.
- **CRC-32C**: polynomial 0x1EDC6F41 processed LSB first, initial value and
  final XOR 0xFFFFFFFF. The CRC of the ASCII string "123456789" is 0xE3069283.

The CAN acceptance filters pass frames whose CFP destination is the node's own
address or 31. zed-a also accepts destination 10, which is how zed-b's answers
to the host computer reach it.

## Console commands

UART1, the ZedBoard's USB-UART, carries a line console at 115200 baud with the
prompt `zed>` and a space. At boot each board names itself and its links:

<!-- markdownlint-disable MD013 -->
<!-- pyml disable md013-->

```text
zed-a: CSP address 1 on a ZedBoard (Zynq-7020, Cortex-A9 core 0)
links: can0 1000 kbit/s, CSP with 29-bit CFP identifiers; kiss on uart0 115200 baud to host computer 10
console: uart1 115200 baud, type help
zed>
```

<!-- pyml enable md013-->
<!-- markdownlint-enable MD013 -->

<!-- markdownlint-disable MD013 -->
<!-- pyml disable md013-->

| Command | Prints |
|---|---|
| `help` | the commands |
| `status` | node, uptime, CAN0 mode and error counters, KISS link, routes, services |
| `stats` | CAN, KISS and CSP counters, and dropped packets by reason |
| `uptime` | milliseconds since boot |
| `ping <addr> [size]` | sends `size` bytes (default 32, at most 256) to port 1 of `addr`, then the answer's size and round-trip time, or a timeout after 1000 ms |

<!-- pyml enable md013-->
<!-- markdownlint-enable MD013 -->

In Renode a ping over CAN takes 4 ms, because each hop waits for the next 1 ms
tick:

```text
zed> ping 2
reply from 2: 32 bytes in 4 ms
```

## Building the firmware

`firmware/build.py` needs Python 3.11 or newer and `arm-none-eabi-gcc`. It takes
the compiler from `--cc`, then the `ARM_NONE_EABI_GCC` environment variable,
then `PATH`, and finds `arm-none-eabi-size` in the same directory.

```bash
python examples/zedboard/firmware/build.py
python examples/zedboard/firmware/build.py --cc <Vitis>/gnu/aarch32/nt/gcc-arm-none-eabi/bin/arm-none-eabi-gcc.exe
```

It compiles `firmware/src/` once per node, with `-DNODE_ADDRESS=1` and
`-DNODE_ADDRESS=2`, writes `firmware/build/zed-node-1.elf` and `zed-node-2.elf`
with their link maps, and prints `arm-none-eabi-size` for both. Any compiler or
linker message fails the build. GCC 12.2 (Vitis 2024.1 on Windows) and GCC
13.2.1 (Ubuntu 24.04's `gcc-arm-none-eabi`) build both ELFs without a warning.

The compiler flags are
`-mcpu=cortex-a9 -marm -mfloat-abi=soft -mno-unaligned-access -std=c11 -O2`
`-ffreestanding -Wall -Wextra -Werror`, and the link uses `-nostdlib` with
`firmware/link.ld`. No C library goes in;
`lib.c` supplies the four memory functions GCC may call.

What the firmware does with the hardware:

- `start.S` holds the vector table, sets stacks for the SVC, IRQ, abort and
  undefined modes, clears BSS and calls `main()`. The MMU and the caches stay
  off, so every data access is strongly ordered and must be aligned;
  `-mno-unaligned-access` keeps GCC to aligned accesses.
- The ELF runs from DDR at 0x00100000, the first address every bus master sees
  as DDR (UG585 Table 4-1).
- `clock.c` sets the clocks it needs from the ZedBoard's 33.333 MHz PS_CLK,
  following UG585 Table 25-3 example 1: ARM PLL x40 with the CPU at 666.7 MHz,
  IO PLL x30 (1000 MHz), UART reference clock 50 MHz (UG585 19.4.1), CAN
  reference clock 100 MHz. A PLL that already runs at its planned multiplier, as
  after a ps7_init, is left alone.
- One interrupt is used: the Cortex-A9 private timer, every 1 ms, through the
  GIC. The tick moves bytes and frames between the UART and CAN hardware FIFOs
  and software rings; the main loop handles the rest and waits in WFI between
  ticks.

## Running the boards in Renode without Jig

Without Jig, the boards run in plain Renode 1.17.0 from a checkout of the Jig
repository: the script compiles Jig's models from `src/jig/models` and builds
each machine from Jig's Zynq-7000 platform,
`src/jig/platforms/xilinx/zynq7000.repl`, with `zedboard.repl` on top. From the
repository root:

```bash
python examples/zedboard/firmware/build.py
renode examples/zedboard/renode/two-boards.resc
```

`renode/two-boards.resc` creates the machines zed-a and zed-b, each from one
platform description that names both files (the platform's timers and watchdogs
can't be built without the clock frequencies `zedboard.repl` gives them, so the
two files can't load one after the other), and connects both CAN0 controllers to
one Renode `CANHub`. It boots each board as a JTAG debugger would: the BootROM
model runs its power-on step, which switches the system watchdog off, and holds
the core; the script loads the ELF and the core starts at the ELF's entry point.
Then the emulation starts.

| TCP port | Connected to |
|---|---|
| 4560 | zed-a UART0: the KISS link for the host computer |
| 4561 | zed-a UART1: console |
| 4562 | zed-b UART1: console |

The sockets carry raw bytes, so use a raw TCP client for a console, such as
`nc 127.0.0.1 4561` or PuTTY's Raw mode; the console echoes what you type. With
Renode's GUI each console also opens in an analyzer window. With
`renode --disable-gui`, the analyzers write each console line to Renode's log.

In a second terminal, run the host computer:

```bash
python examples/zedboard/tools/host.py
```

It pings zed-a and zed-b, decodes both status answers, sends zed-b a 200-byte
ping and exits 0 when all five checks pass. That payload cycles through 0xC0,
0xDB, 0xDC, 0xDD and a counter, so 80 of its bytes need a KISS escape in each
direction. One run on Windows:

<!-- markdownlint-disable MD013 -->
<!-- pyml disable md013-->

```text
host computer 10 on 127.0.0.1:4560
ping 1: 32 of 32 bytes in 11.5 ms, payload intact (14 KISS escapes each way)
ping 2: 32 of 32 bytes in 15.9 ms, payload intact (14 KISS escapes, 5 CAN frames each way)
status 1: node 1, uptime 0.418 s, CAN sent 15 received 15 frames (TEC 0, REC 0), KISS sent 2 received 3 frames, routed 2, CRC errors 0
status 2: node 2, uptime 0.427 s, CAN sent 15 received 16 frames (TEC 0, REC 0), KISS sent 0 received 0 frames, routed 0, CRC errors 0
ping 2: 200 of 200 bytes in 62.0 ms, payload intact (80 KISS escapes, 26 CAN frames each way)
5 of 5 checks passed
```

<!-- pyml enable md013-->
<!-- markdownlint-enable MD013 -->

The times are wall-clock and change from run to run. `host.py` also takes
single commands, `ping 2 --size 256`, `status 1` and `uptime 2`, and `--host`
and `--port` for the socket.

### Changing ports and firmware paths

Set the script's variables before including it. In bash or PowerShell:

```bash
renode -e '$kiss_port=5560; $console_a_port=5561; $console_b_port=5562; include @examples/zedboard/renode/two-boards.resc'
python examples/zedboard/tools/host.py --port 5560
```

The other variables are `$zed_a_elf`, `$zed_b_elf`, `$jig_models` and
`$jig_platforms`; the last two can name the `models` and `platforms` directories
of an installed `jig` package instead of the checkout's. Windows reserves TCP
port ranges for Hyper-V, and the ranges move between boots:
`netsh interface ipv4 show excludedportrange protocol=tcp` lists them, and
Renode reports `Unable to create '0.0.0.0:<port>' socket` for a port inside one.

## Running on ZedBoard hardware

On a board, the DDR controller has to be set up before the ELF can be loaded, as
in any Zynq JTAG flow. Set the boot-mode jumpers to JTAG, run the ZedBoard's
ps7_init from the debugger (with Vitis's xsct: `source ps7_init.tcl`,
`ps7_init`, `ps7_post_config`), download the ELF and run core 0 from `_start`.
The firmware then sets its clocks and routes its pins (UG585 appendix B, MIO_PIN
registers):

| Signal | MIO pins |
|---|---|
| CAN0 Rx, Tx | 10, 11 |
| UART0 RxD, TxD (KISS, zed-a only) | 14, 15 |
| UART1 TxD, RxD (console, the USB-UART) | 48, 49 |

Only each pin's multiplexer and output enable change; the I/O standard and
pull-up stay as the board's ps7_init set them. MIO 10 to 15 reach the ZedBoard's
MIO Pmod header. A real bus needs a CAN transceiver on MIO 10 and 11 and 120-ohm
termination at each end (ISO 11898-2); the KISS link needs a USB-serial adapter
on MIO 14 and 15.

## Hardware the firmware touches

<!-- markdownlint-disable MD013 -->
<!-- pyml disable md013-->

| Block | Base address | Registers | Reference |
|---|---|---|---|
| SLCR | 0xF8000000 | SLCR_LOCK, SLCR_UNLOCK, ARM_PLL_CTRL, ARM_PLL_CFG, IO_PLL_CTRL, IO_PLL_CFG, PLL_STATUS, ARM_CLK_CTRL, APER_CLK_CTRL, UART_CLK_CTRL, CAN_CLK_CTRL, CAN_MIOCLK_CTRL, UART_RST_CTRL, CAN_RST_CTRL, MIO_PIN_10, 11, 14, 15, 48, 49 | UG585 chapters 4, 25, 26; B.28 |
| UART0, UART1 | 0xE0000000, 0xE0001000 | Control_reg0, mode_reg0, Intrpt_dis_reg0, Chnl_int_sts_reg0, Baud_rate_gen_reg0, Rcvr_timeout_reg0, Channel_sts_reg0, TX_RX_FIFO0, Baud_rate_divider_reg0 | UG585 chapter 19; B.33 |
| CAN0 | 0xE0008000 | SRR, MSR, BRPR, BTR, ECR, ESR, SR, ISR, IER, ICR, TXFIFO_ID, TXFIFO_DLC, TXFIFO_DATA1, TXFIFO_DATA2, RXFIFO_ID, RXFIFO_DLC, RXFIFO_DATA1, RXFIFO_DATA2, AFR, AFMR1 to AFMR3, AFIR1 to AFIR3 | UG585 chapter 18; B.5 |
| GIC CPU interface | 0xF8F00100 | ICCICR, ICCPMR, ICCIAR, ICCEOIR | UG585 chapter 7, B.24; Arm IHI 0048 |
| GIC distributor | 0xF8F01000 | ICDDCR, ICDISER0, ICDICER0 to ICDICER2, ICDICPR0 to ICDICPR2, ICDIPR7 | UG585 chapter 7, B.24; Arm IHI 0048 |
| Private timer | 0xF8F00600 | Private_Timer_Load_Register, Private_Timer_Control_Register, Private_Timer_Interrupt_Status_Register | UG585 8.2, B.24; Arm DDI 0407 4.1 |
| CP15 | | MPIDR, SCTLR, VBAR; DFAR and DFSR in the fault report | Arm DDI 0406 |

<!-- pyml enable md013-->
<!-- markdownlint-enable MD013 -->

## Files

<!-- markdownlint-disable MD013 -->
<!-- pyml disable md013-->

| Path | Contents |
|---|---|
| `firmware/src/` | the C11 sources and `start.S` |
| `firmware/link.ld` | memory layout: DDR from 0x00100000, mode stacks |
| `firmware/build.py` | the build script |
| `zedboard.repl` | the ZedBoard on Jig's Zynq-7000 platform: clocks, JTAG boot straps, DDR |
| `jig.toml` | the Jig project |
| `boards/` | the board definitions zed-a and zed-b, and their plugin `zedboard.py` |
| `lib/zed.jig` | procedures the plans share |
| `plans/` | the Jig test plans |
| `tests/` | tests of the plugin and the board definitions |
| `renode/two-boards.resc` | both boards, the CAN bus and the sockets, in plain Renode |
| `tools/host.py` | the stand-alone host computer, standard library only |

<!-- pyml enable md013-->
<!-- markdownlint-enable MD013 -->

Sources: Xilinx UG585 "Zynq-7000 SoC Technical Reference Manual" v1.7; Arm DDI
0407 "Cortex-A9 MPCore Technical Reference Manual"; Arm IHI 0048 "ARM Generic
Interrupt Controller Architecture Specification"; Arm DDI 0406 "ARM Architecture
Reference Manual, ARMv7-A and ARMv7-R edition"; the Digilent ZedBoard Hardware
User's Guide; libcsp 1.6.
