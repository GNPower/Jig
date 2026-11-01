# Tutorial 2: Two ZedBoards on CAN with a host computer

This tutorial puts zed-a and zed-b on one CAN bus, keeps the host computer
on zed-a's serial link, and follows packets across the bus: pings between
the boards' consoles, pings the host computer sends through zed-a, a ping
large enough to need 26 CAN frames each way, and the status records each
board keeps.

**Prerequisites**: {doc}`one_zedboard`.

## What we are building

The plan `examples/zedboard/plans/two_zedboards.jig`, for this system:

```text
host (10) <-- KISS on zed-a's UART0 --> zed-a (1) <-- can0 --> zed-b (2)
```

zed-a sends packets for address 10 to its serial link and every other
packet to the bus. zed-b sends everything to the bus. A packet from the
host computer to zed-b therefore crosses the bus once each way, with
zed-a forwarding it.

## Step 1: Declare two boards and a bus

```jig
import "zed.jig"

system "two ZedBoards on CAN" {
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
```

Both boards come from the one `zedboard` board file; the `node` option
gives `a` the zed-a firmware and `b` the zed-b firmware. `link can0:
can(...)` joins the boards' `can` links, the external CAN link the board
file declares on each, into one bus. The harness carries the frames
between them: it puts each frame on the bus at the bit rate, acknowledges
frames like any other node on the bus, and watches every frame that
passes. The bit rate comes from the boards' links, and a system link that
states one, as this one does, must agree with them. `link ground` joins
the host computer to zed-a's `kiss` link, as in tutorial 1. Declarations
come in any order: Jig declares the boards first, then the nodes, then
the links.

`import "zed.jig"` brings in the procedures of
`examples/zedboard/lib/zed.jig`. Jig looks for a plain file name beside the
plan first, then in each directory that `[paths] lib` in the project's
`jig.toml` lists, and the example lists `lib`. The file holds one
procedure:

```{literalinclude} ../../examples/zedboard/lib/zed.jig
:language: jig
```

Both boards start at the same instant, so the setup waits for both banners
at once. `parallel` starts its tasks together and ends when every task has
ended; if one fails, the others are cancelled and the block fails. Each
step in the report names the task it ran in: here zed-b's prompt arrives at
0.020 s and zed-a's, after a longer banner, at 0.030 s.

## Step 2: Ping between the consoles, at the same time

Each board's console has a `ping <addr>` command that sends 32 bytes and
prints the reply. Two tasks run both pings at once:

```jig
test "the boards ping each other over CAN" {
    a.console.clear()
    b.console.clear()
    parallel {
        task from_a {
            a.send("ping 2")
            a.expect(/reply from 2: 32 bytes/, within: 2 s)
        }
        task from_b {
            b.send("ping 1")
            b.expect(/reply from 1: 32 bytes/, within: 2 s)
        }
    }
}
```

Both consoles print `reply from ...: 32 bytes in 4 ms`; the firmware
times its pings with its 1 ms tick. `send` returns at once and `expect`
waits for the reply line itself; `cli` would return once the console had
been quiet for 0.3 s, which a slow reply outlasts.

## Step 3: Follow the host computer's packets

```jig
test "zed-a forwards the host computer's packets to zed-b" {
    check host.ping(a) < 20 ms
    check host.ping(b) < 50 ms
    let routes = can0.traffic().routes
    check routes["10->2:1"] == 1
    check not ("10->1:1" in routes)
}
```

The round trips are 5.0 ms to zed-a and 9.0 ms to zed-b, the bus and
zed-a's forwarding adding 4 ms. The bus observer reassembles every CSP
packet from the frames on the bus, whoever sent them, and
`traffic().routes` counts packets per source, destination and destination
port. After this test it reads:

```text
{1->2:1: 1, 1->2:49: 1, 2->1:1: 1, 2->1:49: 1, 2->10:26: 1, 10->2:1: 1}
```

`10->2:1` is the host computer's ping to zed-b and `2->10:26` zed-b's
answer, sent to the port the host computer's request came from. The
other four are the console pings of step 2. The ping to zed-a never
touches the bus, so `10->1:1` is missing.

## Step 4: Send a payload that needs fragments

CSP's CAN fragmentation protocol puts the header, the length and 2 bytes of
payload in the first frame and up to 8 bytes in each frame after it, so a
200-byte ping takes 26 frames each way. The example's board plugin,
`examples/zedboard/boards/zedboard.py`, gives plans that arithmetic as the
function `cfp_frames`:

```jig
test "a 200-byte ping crosses the bus in fragments" {
    let start = now()
    let frames = can0.frames
    let rtt = host.ping(b, size: 200)
    log "{can0.frames - frames} CAN frames, {rtt * 1000:.1f} ms"
    check can0.frames - frames == 2 * cfp_frames(200)
    let requests = can0.packets(src: 10, dst: 2, since: start)
    check len(requests) == 1
    check requests[0].length == 200
    check can0.traffic().incomplete == 0
}
```

The log line reads `52 CAN frames, 48.6 ms`. `packets` returns the
reassembled packets as records with `time`, `src`, `dst`, `sport`,
`dport`, `flags`, `length`, `crc_ok` and `data`, filtered by the arguments
given. `incomplete` counts fragment sequences that never completed a
packet. The run directory keeps every packet the bus carried in
`can0.packets.csv`.

## Step 5: Decode the status records

The board plugin also registers a decoder for the 36-byte status record,
and the board file names it as the board's housekeeping codec on port 10,
in the `housekeeping` map of the device's `protocol csp(...)`.
`host.hk(board)` sends the request and returns the decoded record:

```jig
test "zed-a counts the packets it forwards" {
    let before = host.hk(a)
    host.ping(b)
    let after = host.hk(a)
    check after.node == 1
    check after.routed - before.routed == 2
}
```

One ping to zed-b is two forwarded packets: the request onto the bus and
the reply back to the serial link. The record's fields are `magic`,
`version`, `node`, `can_tec`, `can_rec`, `uptime_ms`, `can_sent`,
`can_received`, `kiss_sent`, `kiss_received`, `routed` and `crc_errors`.
{doc}`../how_to/write_a_plugin` writes that decoder.

## Step 6: Expect a timeout

No node has address 5. zed-a still forwards a packet for it to the bus,
where nothing answers:

```jig
test "a ping to an absent node times out" {
    let answered = true
    try {
        host.ping(5, timeout: 500 ms)
    } catch error {
        answered = false
        log "{error.kind}: {error.message}"
    }
    check not answered
}
```

Inside `try`, a failure (a false check, a timeout, a refused request) runs
the `catch` block instead of failing the test. `error.message` describes
it and `error.kind` is `StepFailed` for a timeout or `CheckFailed` for a
false check. The log line here reads `StepFailed:
two_zedboards.jig:69:9: <plain connection 10:31 -> 5:1>: no reply within
0.5 s`.

## The complete plan

```{literalinclude} ../../examples/zedboard/plans/two_zedboards.jig
:language: jig
```

## Experiment

- Add `log text(host.stats)` at the end of a test to see the host
  computer's counters: packets sent and received, CRC errors,
  retransmissions.
- Change the system link's bit rate to `500 kbit/s`. The plan stops
  before Renode starts, with exit code 2: the boards' `can` links state
  1 Mbit/s, the rate the firmware sets its controller to, and a system
  link's rate has to agree with them.
- Open `can0.packets.csv` in the run directory and find the two halves of
  the 200-byte ping.

## Where to go next

`examples/zedboard/plans/multiboard.jig` runs on this system too, and adds
a check of each board's frame counters against what the bus carried, a
link outage and a power cycle of zed-b. {doc}`profiles_timelines` runs
work on a schedule and records values over time.
{doc}`../user_manual/library` lists every member of boards, links and
nodes.
