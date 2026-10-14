# Tutorial 1: Test one ZedBoard

This tutorial builds a plan for zed-a on its own. The plan waits for the
board's boot banner, runs console commands, checks the board's uptime
against simulated time, and talks to the board over its serial link from a
CSP node that the harness runs.

**Prerequisites**: {doc}`../getting_started/first_run`, with the example's
firmware built.

## What we are building

The finished plan is `examples/zedboard/plans/one_zedboard.jig`: one
system, one procedure, a setup block and six tests. To follow along, start
an empty file beside it, `examples/zedboard/plans/my_first.jig`, and add
each step to it. Jig finds the example's project,
`examples/zedboard/jig.toml`, by searching up from the plan, so any plan in
that directory sees the example's boards. By the end you will have seen
each kind of step a plan reports: console expectations, commands, waits,
checks, CSP requests and log lines.

## Step 1: Read the board definition

A plan names boards by definition. zed-a's is
`examples/zedboard/boards/zed-a/board.yaml`:

```{literalinclude} ../../examples/zedboard/boards/zed-a/board.yaml
:language: yaml
```

`root: ../..` makes the example's directory the board's root, and
`firmware.app` is relative to it: the ELF that `build.py` writes.
`soc_platform` loads the Zynq-7000 platform Jig ships, and `platform`, a
Renode platform description relative to this file, sets the ZedBoard's
clocks, boot straps and DDR on top of it. `plugin` names the board plugin
that zed-a and zed-b share; tutorial 2 uses it. The three interfaces are
the names a plan uses: `a.console`, `a.kiss` and `a.can` for a board
instance called `a`. {doc}`../user_manual/board_yaml` lists every key.

## Step 2: Declare the system and wait for the banner

Give the plan a system of one board and a setup block:

```jig
system "one ZedBoard" {
    board a: "zed-a"
}

setup {
    a.console.expect(/zed-a: CSP address 1/, within: 2 s)
    a.console.expect("zed> ")
}
```

`board a: "zed-a"` builds a Renode machine called `a` from the zed-a
definition. The setup block runs before any test. `expect` waits, in
simulated time, until the console has printed text matching the regular
expression; `within: 2 s` sets the limit, and the default is 10 s. If the
banner never comes, the setup fails, the plan stops, and the report shows
the last console output the board produced.

Run it:

```bash
jig run examples/zedboard/plans/my_first.jig
```

The firmware prints its banner 7 ms after it starts. Jig hands each
console its output every 10 ms of simulated time, so the report stamps
the first `expect` at 0.010 s and the prompt at 0.030 s. With no tests
yet, the output lists the system and ends with `0 passed, 0 failed,
0 errors`.

## Step 3: Use the console

Add a test that runs the `status` command:

```jig
test "the console names the node and its links" {
    let status = a.cli("status")
    check status contains "zed-a, CSP address 1"
    check status matches /kiss\s+uart0, 115200 baud/
    check status matches /can0\s+1000 kbit\/s, normal mode, error active/
}
```

`cli` clears the console buffer, sends the command with the interface's
line ending (`eol: cr` in board.yaml), and returns everything the board
printed until it stayed quiet for 0.3 s of simulated time. Here that is:

```text
status
node     zed-a, CSP address 1
uptime   0.031 s
can0     1000 kbit/s, normal mode, error active, TEC 0, REC 0
kiss     uart0, 115200 baud
routes   10 -> kiss, others -> can0
services ping 1, uptime 6, status 10
zed>
```

`contains` looks for text; `matches` searches with a regular expression.
Each `check` that is false fails the test, and the report prints the
expression with the values on both sides.

A loop checks that `help` lists every command, each on a line that starts
with two spaces:

```jig
test "help lists every command" {
    let help = a.cli("help")
    for command in ["help", "status", "stats", "uptime", "ping"] {
        check help contains "  {command} "
    }
}
```

`{command}` inside a string is replaced by the variable's value.

The firmware's `uptime` command prints the milliseconds since boot. A
procedure turns that into a number, so tests can compare it:

```jig
procedure uptime_ms(board) {
    let found = match(/uptime (\d+) ms/, board.cli("uptime"))
    check found != none, "no uptime in the console output"
    return found.number(1)
}

test "uptime follows simulated time" {
    let before = uptime_ms(a)
    wait 2 s
    let after = uptime_ms(a)
    check after - before in 2000 .. 2500
}
```

`wait 2 s` lets two seconds of simulated time pass, and the board's timer
counts them. The board reported 691 ms, then 3001 ms: 2310 ms apart,
because the first `cli` waited 0.3 s for quiet after the board answered.
Procedures go at the top level of the plan, next to `setup` and the
tests, and can take boards as arguments.

## Step 4: Add a ground station

The example's ground station is CSP node 10 on zed-a's serial link. Add
one to the system, so the harness runs it:

```jig
system "one ZedBoard" {
    board a: "zed-a"
    node ground: csp(address: 10, link: a.kiss)
}
```

The harness speaks KISS on that UART, adds the CRC-32C the firmware
expects, and answers pings like a libcsp node. Two tests use it:

```jig
test "the ground station pings zed-a" {
    let rtt = ground.ping(a)
    log "round trip {rtt * 1000:.1f} ms"
    check rtt < 20 ms
}

test "the status record names node 1" {
    let reply = ground.request(a, 10, x"01")
    check len(reply) == 36
    check reply.startswith(x"5A454453")
    check reply[5] == 1
}
```

`ping` sends 10 bytes and returns the round trip in seconds, from the
request leaving the harness to the last byte of the reply arriving; the
log line reads `round trip 5.0 ms`. `{rtt * 1000:.1f}` formats the value
the way Python's format specifications do. `request` sends one packet to
port 10 and returns the reply's payload as bytes. The status record starts
with the magic number `0x5A454453` (`ZEDS` in ASCII), and its sixth byte
is the node's CSP address. {doc}`two_zedboards` decodes the whole record
by name.

Port 6 answers an empty request with the uptime in whole seconds, 4 bytes
big-endian, as libcsp 1.6's uptime service does:

```jig
test "port 6 answers the uptime in whole seconds" {
    let before = uptime_ms(a)
    let reply = ground.request(a, 6)
    let after = uptime_ms(a)
    check len(reply) == 4
    let seconds = number("0x" + reply.hex())
    log "uptime {seconds} s, console {before} ms then {after} ms"
    check seconds in floor(before / 1000) .. floor(after / 1000)
}
```

`reply.hex()` gives the bytes as hexadecimal text and `number` reads
`0x00000003` as 3. The console read 3331 ms before the request and
3651 ms after it, so the answer has to lie in `3 .. 3`.

## Step 5: Run the plan and read the report

```bash
jig run examples/zedboard/plans/one_zedboard.jig --html out/one_zedboard.html
```

```text
PLAN  examples\zedboard\plans\one_zedboard.jig
  board a: zed-a, jtag boot, 100 MIPS
  node ground: CSP 10 on link a.kiss
  PASS  the console names the node and its links  (0.33 s)
  PASS  help lists every command  (0.33 s)
  PASS  uptime follows simulated time  (2.62 s)
  PASS  the ground station pings zed-a  (0.01 s)
  PASS  the status record names node 1  (0.01 s)
  PASS  port 6 answers the uptime in whole seconds  (0.63 s)
  6 passed, 0 failed, 0 errors (4.0 s simulated, 11.5 s wall); fingerprint f84766f63cb63613

report: ...\out\one_zedboard.html
```

Two log lines about Renode's start are left out above. The time after
each test is simulated time; the wall time at the end includes Renode's
start and the compile of Jig's models, 6.5 s of the 11.5 s in this run on
Windows.

Open `out/one_zedboard.html`. Each test lists its steps with the simulated
time each one finished at, the task it ran in, and its file, line and
column. The `log` line carries the round-trip time. The run directory
named at the top of the page, under `examples/zedboard/out/runs/`, holds
`a/console.log`, the full console transcript.

## The complete plan

```{literalinclude} ../../examples/zedboard/plans/one_zedboard.jig
:language: jig
```

## Experiment

- Narrow the uptime range to `2000 .. 2100` and run again. The test fails,
  and the failure line shows the difference the board reported:
  `check after - before in 2000 .. 2100 failed: 2310 in 2000 .. 2100`.
- Run with `--repeat 3`. The three runs print one fingerprint, and the
  plan ends with `repeatable: 3 runs, identical results`, because nothing
  in the plan depends on the host.
- Run `jig run examples/zedboard/plans/one_zedboard.jig -k ground` to run
  only the tests whose names contain `ground`; the other five are reported
  as `SKIP`.

## Where to go next

{doc}`two_zedboards` adds zed-b on a CAN bus and follows packets across it.
{doc}`../user_manual/language` describes every statement used here.
