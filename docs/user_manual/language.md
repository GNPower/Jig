# The plan language

A plan, a `.jig` file, declares a system of emulated boards and the nodes
the harness runs, then runs blocks of statements against it in simulated
time. `jig run` gives each plan its own emulation. The tests inside one plan
run in order and share it, so a test starts where the previous one left the
boards.

```jig
import "../lib/zed.jig"

system "two ZedBoards on CAN" {
    board a: "zedboard" (node: 1)
    board b: "zedboard" (node: 2)
    link can0: can(members: [a.can, b.can])
    node host: csp(address: 10) over kiss
    link ground: uart(members: [a.kiss, host])
}

setup {
    parallel {
        task zed_a { booted(a, 1) }
        task zed_b { booted(b, 2) }
    }
}

test "the host computer reads node 2's status" [smoke] {
    let status = host.hk(b)
    check status.node == 2
}
```

The plan runs as it stands from `examples/zedboard/plans`: `booted`, from
the example's `lib/zed.jig`, waits for a board's boot banner and its first
prompt. Board files are written in the same language
({doc}`board_files`).

## File structure

```{list-table}
:header-rows: 1
:widths: 18 37 45

* - Item
  - Form
  - Notes
* - Import
  - `import "path.jig"` or `import "path.jig" as name`
  - Imported files hold only constants and procedures. With `as`, their
    names are reached as `name.procedure(...)`.
* - System
  - `system "title" { declarations }`
  - At most one per plan; the title is optional.
* - Constant
  - `const NAME = expression`
  - Evaluated once when the plan loads; it cannot wait.
* - Procedure
  - `procedure name(a, b = default) { ... }`
  - Parameters can have defaults; `return value` ends it.
* - Setup
  - `setup { ... }`
  - Runs first. If it does not pass, the plan stops and reports why.
* - Test
  - `test "name" { ... }` or `test "name" [tag, tag] { ... }`
  - `jig run -k text` selects tests by name, `--tags tag` by tag.
* - Teardown
  - `teardown { ... }`
  - Runs last, unless the emulation stopped. A failing teardown fails the
    run.
```

Statements are separated by whitespace. A line break carries no meaning, so
several short statements can share a line. `#` starts a comment that runs
to the end of the line.

These words are reserved and cannot name a variable: `let if elif else
repeat times every for in while parallel race task timeline at try catch
check wait until within log record fail return break continue and or not
true false none as over`. Words such as `board`, `link`, `node`, `test` and
`system` open a declaration only at the place a declaration goes, so they
remain usable as variable and parameter names.

## Imports and the library path

```jig
import "../lib/zed.jig"
import "zed.jig" as zed
import "std:text.jig"
```

- A path that starts with `./` or `../` is relative to the importing file.
- Any other plain path is looked up beside the importing file, then in
  each directory of `[paths] lib` in the project's `jig.toml`: the
  ZedBoard example's plans find `zed.jig` in its `lib` directory that way.
- `"NAME:file.jig"` names a file in the `lib` directory beside the board
  file of the board `NAME`, so a board repository can ship the procedures
  its plans share.
- `"std:file.jig"` names a file of Jig's own plan library.

A library reference cannot reach outside its directory with `../`.

## Declaring the system

```jig
system "title" {
    board NAME: "board" (boot: "jtag", mips: 100, OPTION: VALUE)
    node NAME: csp(address: 10) over kiss
    node NAME: csp(address: 11) over cfp
    link NAME: uart(members: [BOARD.LINK, NODE])
    link NAME: can(members: [BOARD.LINK, BOARD.LINK, NODE])
}
```

`board`
: An instance of a board, found by name on the board search path
  ({doc}`board_files`). Settings in parentheses: `boot`, one of the
  device's boot modes (`jtag` or `qspi` for a Zynq-7000, `flash` for an
  STM32); `mips`, the virtual CPU speed in millions of instructions per
  second; `storage`, initial images for the board's storage peripherals,
  as a map from peripheral to file relative to the plan; `power`, `"on"`
  or `"off"`, over the board file's `power` setting; and the board's
  options by name. `jig run --boot`, `--mips` and `--option NAME=VALUE`
  override them for every board.

`node`
: A protocol endpoint the harness runs, with the layers below it after
  `over`: `csp(address: 10) over kiss` sends CSP packets in KISS frames, for
  a UART, and `csp(address: 11) over cfp` in CFP fragments, for a CAN bus.
  The bundled CSP extension provides `csp(...)`. Optional RDP settings:
  `window`, `conn_timeout`, `packet_timeout`, `ack_timeout`,
  `delayed_acks` and `ack_delay_count`; `broadcast: true` lets the node
  take packets sent to the broadcast address 31. A CSP node answers pings
  like a libcsp node. Board plugins can add other node kinds. A node joins
  one link, and a node no link joins is an error.

`link`
: Joins boards' external links and nodes. A `uart` link joins one board's
  external UART link and the nodes on it. A `can` link joins the external
  CAN links of any number of boards and the nodes on that bus; the harness
  carries the frames, acknowledges them and watches the bus. A `can` link
  with one board's link and no node attaches the harness to that board's
  bus, so a plan can send and watch frames on one board.

Rules for links:

- Rates come from the boards' links. A system link that states `baud` or
  `bitrate` must agree with them; a CAN bus whose boards state none runs
  at 1 Mbit/s.
- A board's link joins one system link at most.
- A node's stack must fit the link: `csp(address: 10) over kiss` on a CAN
  link is an error that names the layer that does not fit.
- An external UART link of a board needs no system link: the harness reads
  and writes it in every run, and its output is recorded from the start.
  An external CAN link that no system link names stays unconnected.
- This release refuses a system link on a link inside a board (a probe)
  and a UART link between two boards; a later release adds both, with
  boards of several devices.

Declarations come in any order: Jig declares the boards first, then the
nodes, then the links, so a link can name a node declared below it.
Different boards can share a system and a link.

A plan without a system runs on one board named `dut`: the board given by
`jig run --board NAME`, else the one in `[defaults] board` of `jig.toml`.

## Statements

```{list-table}
:header-rows: 1
:widths: 40 60

* - Statement
  - Meaning
* - `let x = expression`
  - Declare a variable in the current block.
* - `x = e`, `x += e`, `x -= e`, `list[i] = e`, `map[k] = e`
  - Assign to an existing variable or item, in this block or an enclosing
    one.
* - `expression`
  - Evaluate for its effect, such as a call.
* - `check condition` or `check condition, "message"`
  - A false condition fails the test. A failed comparison reports the
    values on both sides, then the message.
* - `fail "message"`
  - Fail the test.
* - `wait 2 s`
  - Let simulated time pass.
* - `wait until condition within 10 s`
  - Re-evaluate the condition after every step of the emulation; fail the
    test if the time runs out (60 s without `within`). The condition
    cannot wait itself.
* - `log "text"`
  - Add a line to the report.
* - `record name = value`
  - Store a measurement with its simulated time. The HTML report plots
    numeric series.
* - `if c { } elif c { } else { }`
  - Conditional blocks.
* - `repeat N times as i { }`
  - Repeat a block; `i` counts from 0.
* - `while c { }`
  - Repeat while the condition holds.
* - `for item in list_or_range_or_map { }`
  - Iterate; a map gives its keys, and `1 .. 5` gives 1 to 5.
* - `every 1 s for 2 min as i { }`
  - Start iterations on a fixed grid (0, 1 s, 2 s and so on) until the
    duration ends. `every P times N` and `every P until c` end by count
    or by condition. An iteration that overruns its period skips to the
    next slot and logs it.
* - `parallel { task a { } task b { } }`
  - Run the tasks at once; the block ends when all have ended. A task
    that fails cancels the others and fails the test.
* - `race { task a { } task b { } }`
  - End with the first task to finish; the others are cancelled.
* - `timeline { at 0 s as first { } at 2 min { } }`
  - Run each entry as a task starting at its offset from the start of the
    timeline; the block ends when every entry has ended. Entries due at the
    same moment start in the order written. An entry is named by `as`,
    else `at <offset>`; names must differ. A failing entry cancels the
    others and fails the test.
* - `try { } catch error { }`
  - Run the `catch` block instead of failing the test when something
    inside `try` fails: a false check, a timeout, a refused request.
    `error.message` describes it; `error.kind` is `CheckFailed` or
    `StepFailed`.
* - `break`, `continue`, `return value`
  - Leave a loop, start its next iteration, leave a procedure.
```

Tasks see the variables of the block around them. Each step in the report
names the task it ran in; steps outside any task run in `main`.

## Expressions

Numbers
: `42`, `0x1F`, `0b101`, `1_000`, `2.5`, `1e-3`. A unit after a number, on
  the same line, converts it to SI base units: `100 ms` is 0.1, `2 min` is
  120, `1.25 V` is 1.25, `1 Mbit/s` is 1000000. The units are `ns us ms s
  min h`, `mV V`, `uA mA A`, `Hz kHz MHz GHz`, `B KiB MiB` and `bit/s
  kbit/s Mbit/s`. Every duration a function takes is in seconds.

Text
: `"..."` with the escapes `\n`, `\r`, `\t`, `\0`, `\"`, `\\` and
  `\xHH`. Fields in braces are evaluated: `"rtt {rtt * 1000:.3f} ms"`, with
  a Python format after the last top-level colon. `{{` and `}}`, or `\{`
  and `\}`, are literal braces. Text ends on its line.

Regular expressions
: `/reply from (\d+)/`, with the flags `i m s x` after the closing slash.
  Write a slash inside as `\/`.

Bytes
: `x"01 0a ff"`; spaces and underscores between the digits are ignored.

Collections and constants
: Lists `[a, b]`, maps `{key: value, "spaced key": value}` (either may end
  with a comma, for one item per line), ranges `low .. high` with both ends
  included, `none`, `true` and `false`.

Members, calls and indexing
: `a.console.expect(/ready/, within: 30 s)`, with named arguments after
  positional ones; `list[0]`, `map["key"]`, `match[1]`, `data[5]`.

Operators, loosest first: `over`, which stacks protocol layers (`csp over
kiss`); `or`; `and`; `not`; the comparisons `==`, `!=`, `<`, `<=`, `>`,
`>=`, `in`, `not in`, `contains` and `matches`; `..`; `+` and `-`; `*`, `/`
and `%`; unary `-` and `+`. `and` and `or` stop at the first operand that
decides the result, and return that operand.

- `a contains b`: text contains text or a regular expression match, a list
  contains an item, a map has a key.
- `a matches /re/`: a search anywhere in the text.
- `x in 1 .. 5`: a range test; `x in list`: membership.
- `+` joins text, bytes and lists; text plus any other value converts the
  other value to text.

Text has the methods `upper lower strip split splitlines startswith
endswith replace find count encode`; bytes have `hex decode startswith
endswith find count`; lists have `append index count`.

## Procedures and scope

A procedure runs in a scope of its own, with its parameters as variables.
It sees the names of the file it is defined in: that file's constants and
procedures and, for a procedure in the plan itself, the system's boards,
links and nodes. Procedures in imported files take boards as arguments. A
block (`if`, a loop body, a task) opens a scope inside the one around it:
`let` declares a variable there, and assignment finds the nearest
enclosing variable of that name. Procedures can wait, call other
procedures and recurse, up to 200 calls deep.

## Standard library

Jig ships `.jig` procedures for patterns that recur across boards. Import
them with the `std:` prefix:

```{list-table}
:header-rows: 1
:widths: 45 55

* - Procedure
  - Does
* - `std:console.jig`: `wait_ready(board, pattern, timeout: 30 s,
    settle: 500 ms)`
  - Wait for `pattern` on the board's console, then for the line to stay
    quiet for `settle`; returns the match.
* - `std:console.jig`: `restart_via(board, command, ready,
    timeout: 40 s)`
  - Send a restart command and wait for `ready` again.
* - `std:console.jig`: `counter(board, name, command: "stats")`
  - The number on the `name` line of the command's output; fails when no
    line carries it.
* - `std:text.jig`: `number_in(output, pattern)`
  - The first group of `pattern` in `output`, as a number; fails when the
    pattern is not there.
* - `std:text.jig`: `hex_dump_bytes(text)`
  - The bytes of every line that holds only two-digit hex bytes, in order.
* - `std:records.jig`: `count_during(records, start, end)`
  - How many records fall from `start` up to, not including, `end`.
```
