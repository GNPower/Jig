# Tutorial 3: Profiles, timelines and recorded values

The plans so far ran their steps one after another. This tutorial runs work
on a fixed schedule with `every`, records values into series the HTML
report plots, varies an input over time with a profile, and uses a
timeline to cut the ground station's link part-way through a stream of
pings.

**Prerequisites**: {doc}`two_zedboards`.

## What we are building

The plan `examples/zedboard/plans/profiles_timelines.jig`, on the system of
tutorial 2, with three tests: a status poll every second, pings whose size
follows a ramp, and pings across a one-second link outage.

```jig
import "zed.jig"

system "two ZedBoards on CAN" {
    board a: "zed-a"
    board b: "zed-b"
    bus can0: can(bitrate: 1 Mbit/s, members: [a.can, b.can])
    node ground: csp(address: 10, link: a.kiss)
}

setup {
    parallel {
        task zed_a { booted(a, 1) }
        task zed_b { booted(b, 2) }
    }
}
```

## Step 1: Poll on a schedule and record the answers

```jig
test "zed-b's counters over ten seconds" {
    let previous = none
    every 1 s for 10 s as i {
        let status = ground.hk(b)
        record uptime = status.uptime_ms / 1000
        record can_received = status.can_received
        if previous != none {
            check status.uptime_ms - previous in 999 .. 1001
        }
        previous = status.uptime_ms
    }
}
```

`every 1 s for 10 s` starts an iteration at 0 s, 1 s, 2 s and so on from
the start of the loop, and stops at 10 s, after 10 iterations. The grid is
fixed: each status request takes 20 ms here, which leaves 980 ms of
waiting before the next one, and an iteration that overruns its period
skips to the next free slot and logs that it did. So the uptime zed-b
reports grows by exactly 1000 ms per iteration, from 0.033 s to 9.033 s.
`as i` names the iteration count, from 0. `every 1 s times 5` and
`every 1 s until condition` end the loop by count or by condition instead.

`record name = value` stores a value with the simulated time it was taken.
The HTML report shows each series with its count, its last value and a
plot, and the run directory's `telemetry.csv` lists every recorded value
with its time, block and name. Each request is one CAN frame, so
`can_received` climbs from 1 to 10.

## Step 2: Shape an input with a profile

A profile is a value that changes with the seconds since it started.
`ramp(start, end, over)` rises in a straight line and then holds its end
value; `profile.at(t)` reads it at `t` seconds:

```jig
test "ping sizes follow a ramp" {
    let size = ramp(16, 256, over: 10 s)
    let start = now()
    every 500 ms for 10 s {
        let bytes = round(size.at(now() - start))
        let rtt = ground.ping(b, size: bytes)
        record ping_bytes = bytes
        record ping_ms = rtt * 1000
        check rtt < 100 ms
    }
}
```

`now()` is the simulated time in seconds since the emulation started. The
20 pings grow from 16 bytes to 244, the size at 9.5 s, and their round
trips from 10.5 ms to 58.5 ms. The report plots `ping_ms` and `ping_bytes`
against the same time axis, so each round trip sits next to the payload
size that produced it.

The other profiles are `constant(value)`, `sine(mean, amplitude, period,
phase: 0)`, `table([[t, v], ...])`, which interpolates between points, and
`steps([[t, v], ...])`, which holds each value until the next point.
Profiles add to each other and to numbers: `sine(...) + 0.01` is a sine
with an offset.

## Step 3: Schedule events with a timeline

```jig
test "pings across a link outage" {
    let answered = 0
    let lost = 0
    timeline {
        at 0 s as pinger {
            every 200 ms for 5 s {
                try {
                    ground.ping(a, timeout: 100 ms)
                    answered += 1
                } catch {
                    lost += 1
                }
            }
        }
        at 2 s as outage {
            ground.link_down()
            wait 1 s
            ground.link_up()
        }
    }
    record answered = answered
    record lost = lost
    check lost >= 4
    check answered >= 15
}
```

Each `at` entry runs as a task that starts at its offset from the start of
the timeline, and the timeline ends when every entry has ended. Entries due
at the same instant start in the order they are written. `link_down` cuts
the ground station off: from then until `link_up`, it sends nothing and
receives nothing, as at a loss of signal.

The pinger's 25 iterations meet the one-second gap, and 6 pings time out:
19 answered, 6 lost. Four leave in the middle of the outage. Two more land
on its edges, where both entries are due at once and the pinger, listed
first, runs first: the ping at 2 s goes out before `link_down` runs and
its answer arrives during the outage, and the ping at 3 s goes out before
`link_up` runs. The checks leave room for either edge to fall the other
way.

Tasks see the variables of the block around them, so both entries update
`answered` and `lost`. If an entry fails, the others are cancelled and the
test fails.

## Profiles on sensor inputs

The ZedBoard example has no analog inputs, but boards with an
analog-to-digital converter model can list their inputs in `board.yaml`
under `sensors`. A plan then sets an input in volts, or drives it from a
profile in the background. Here `dut` is a board whose definition names an
input `supply`:

```jig
test "the supply input follows a slow sine" {
    let drift = sine(mean: 1.25 V, amplitude: 50 mV, period: 10 min)
    let supply = dut.drive("supply", drift + 10 mV, every: 1 s)
    wait 2 min
    check supply.running
    supply.stop()
}
```

`drive` returns a handle with `name`, `running`, `updates` and `stop()`.
The work belongs to the block that started it: a test's drive ends with the
test, and one started in `setup` runs for the whole plan.

## The complete plan

```{literalinclude} ../../examples/zedboard/plans/profiles_timelines.jig
:language: jig
```

## Experiment

- Replace the ramp with `steps([[0, 16], [5, 200]])` and compare the
  plotted round trips.
- Add `record lost_sent = ground.stats.lost_sent` after the timeline: it
  counts the packets the ground station sent while its link was down, 5
  here, the four in the middle of the outage and the one at 3 s.
- Move the outage entry to `at 2100 ms`, off the pinger's 200 ms grid. The
  edges no longer meet a ping: 20 are answered and only the 5 sent during
  the outage are lost.

## Where to go next

{doc}`../how_to/index` covers single tasks such as adding a board or
writing a board plugin. {doc}`../user_manual/language` is the complete
reference for the statements used here.
