# Functions and handles

A plan reaches the emulation only through the functions on this page and
the handles its system declaration creates: one per board, bus and node.
Each handle has a fixed set of members, listed here; board plugins can add
members to the handles of their own boards. Members that wait are steps in
the report, and every duration they take is in seconds of simulated time.

## Built-in functions

```{list-table}
:header-rows: 1
:widths: 45 55

* - Function
  - Returns
* - `now()`
  - Simulated seconds since the emulation started.
* - `len(x)`, `abs`, `min`, `max`, `round`, `floor`, `ceil`, `sum`,
    `range(n)` or `range(a, b)`
  - As in Python.
* - `text(x)`, `number(x)`
  - Conversions; `number` accepts `0x...` and `0b...` text.
* - `match(pattern, text)`
  - A match or `none`. A match has `m.text`, `m[1]`, `m.group(name)`,
    `m.groups`, `m.number(1)`, `m.start` and `m.end`.
* - `regex(text)`
  - A regular expression built from text.
* - `hex(n, width)` or `hex(bytes)`, `from_hex(text)`
  - Hexadecimal text, and bytes from hexadecimal text.
* - `read_file(path)`
  - A file's bytes, relative to the plan, else to the project root.
* - `random_bytes(count, seed: 0)`
  - Repeatable pseudo-random bytes.
* - `crc32(data)`, `crc32c(data)`
  - Checksums as numbers: CRC-32/ISO-HDLC, and CRC-32C (Castagnoli), which
    the bundled CSP extension adds.
* - `during(records, start, end)`
  - The records whose `time` is from `start` up to, not including, `end`,
    in simulated seconds: packets from a bus, for example.
* - `off_profile(board, sensor, profile, records, since, lag: 3 s,
    every: 1 s)`
  - The records whose code for `sensor` lies outside the codes the
    profile gave that input in the `lag` seconds before each record, for a
    profile `board.drive` started at `since`. Each is `{time, code,
    expected}`; an empty list means the readings followed the profile.
```

## Profiles

A profile is a value as a function of the seconds since it started.
`board.drive` applies one to a sensor input; a plan can read one with
`profile.at(t)`.

```{list-table}
:header-rows: 1
:widths: 40 60

* - Profile
  - Value at `t` seconds
* - `constant(value)`
  - `value`.
* - `sine(mean, amplitude, period, phase: 0)`
  - A sine around `mean`; `phase` is in seconds.
* - `ramp(start, end, over)`
  - A straight line from `start` to `end` over `over` seconds, then
    `end`.
* - `table([[t, v], ...])`
  - Linear between points; the first value before the first point and the
    last value after the last.
* - `steps([[t, v], ...])`
  - Each value from its time until the next point.
```

Profiles add to each other and to numbers:
`sine(mean: 1.25 V, amplitude: 50 mV, period: 10 min) + 10 mV`.

## Calendar time

Calendar times are text, as a clock prints them: `2027-03-21T06:00:00`,
with a space accepted in place of the `T` and a fraction of a second
accepted at the end.

```{list-table}
:header-rows: 1
:widths: 40 60

* - Function
  - Returns
* - `time_after(time, seconds)`
  - The time `seconds` later, with `seconds` rounded to a whole second
    and any fraction of a second in `time` dropped.
* - `seconds_between(earlier, later)`
  - The difference in seconds.
* - `date_fields(time)`
  - `{year, month, day, hour, minute, second}`, as a clock's set command
    takes them.
```

## Boards

```{list-table}
:header-rows: 1
:widths: 40 60

* - Member
  - Meaning
* - `name`, `board`, `boot_mode`, `address`
  - Instance name, definition, boot mode, CSP address.
* - `console`
  - The console on the board's `console` interface.
* - `cli(command, settle: 0.3 s)`, `send(line)`,
    `expect(pattern, within: 10 s)`
  - Shortcuts for the console's members of the same names.
* - `kiss`, `can` or `interface(name)`
  - An interface, by its name in board.yaml, for `link:` and `members:`.
* - `set_sensor(name, volts)`
  - Set an analog input (board.yaml `sensors`); returns the code the
    converter will report.
* - `sensor_code(name)`, `sensor_code(name, volts: v)`, `sensors`
  - The code an input reads now, or would read at `v`; the input names.
* - `drive(sensor, profile, every: 1 s)`
  - Set an input from a profile every `every` seconds in the background;
    returns a handle with `name`, `running`, `updates` and `stop()`.
* - `set_time(iso)`, `time()`
  - Set and read the board's real-time clock model (board.yaml `rtc`), as
    calendar time.
* - `reset()`
  - Pulse the system-reset input.
* - `power_off()`, `power_on()`, `power_cycle(off: 1 s)`
  - Remove power, restore it, or both with `off` seconds between. Without
    power the board's cores are halted while simulated time runs on, and
    the chips on its rails (board.yaml `power.restart`) restart. Restoring
    power runs the SoC's power-on sequence from reset: a board that boots
    from its own flash starts its firmware again, and a Zynq-7000 booted
    over JTAG comes back waiting in its BootROM for its debugger until
    `boot()` runs its boot flow again. A board that started without power
    runs its whole boot flow at its first `power_on()`.
* - `boot()`
  - Run the board's boot flow again, as at the start of the run, for any
    boot mode and without a reset first: for a JTAG boot, the debugger's
    load. An error while the board has no power.
* - `powered`
  - Whether the board has power: false after `power_off()`, and from the
    start for a board whose `power.initial` is `off`.
* - `image(role)`
  - A firmware image by role, as an update would send it, from the board's
    image builder.
* - `monitor(command)`
  - A Renode monitor command in the board's context, for diagnostics.
```

A board's plugin can add members to its own boards, and only to those
({doc}`../how_to/write_a_plugin`).

## Consoles

```{list-table}
:header-rows: 1
:widths: 40 60

* - Member
  - Meaning
* - `cli(command, settle: 0.3 s)`
  - Clear the buffer, send the command, return everything printed until
    the line was quiet for `settle`.
* - `send(line)`, `write(text)`
  - Send a line with the interface's line ending (`eol` in board.yaml,
    CR by default), or characters as they are.
* - `expect(pattern, within: 10 s)`
  - Wait for a regular expression or text in the buffer; returns the
    match.
* - `seen(pattern)`
  - The match in the buffer now, or `none`.
* - `clear()`, `drain(settle: 0.5 s)`
  - Empty the buffer; wait for the line to go quiet.
* - `buffer`, `text`, `label`
  - What arrived since the last clear; everything the console received
    since the run started, including what the board printed before the
    plan first read the console; the console's name in reports.
```

`cli` and `expect` look at the same buffer, which `cli` and `clear`
empty. A task that waits for lines while another task sends commands
should expect only lines that come after the last command it knows about.

### Background work

`drive` starts work that runs beside the plan. It belongs to the block
that started it: work started in `setup` runs through the whole plan, and
work started in a test stops when the test ends. If it fails, for example
on a sensor the board cannot set, the block running when Jig collects the
failure ends with ERROR and the reason, so work started in `setup` that
fails during a test makes that test ERROR. `handle.stop()` ends it sooner.

## Buses

```{list-table}
:header-rows: 1
:widths: 40 60

* - Member
  - Meaning
* - `name`, `bitrate`
  - The bus's name and bit rate.
* - `frames`
  - CAN frames seen on the bus so far.
* - `acknowledge(enabled)`
  - Whether the harness's adapter acknowledges frames; it does by
    default.
* - `packets(src: n, dst: n, port: n, since: t)`
  - The CSP packets on the bus, whoever sent them, reassembled from each
    sender's fragments: `{time, src, dst, sport, dport, flags, length,
    crc_ok, data}`. The bus keeps the latest 20000; the run directory has
    all of them.
* - `traffic()`
  - `{frames, packets, incomplete, crc_errors, routes}`: fragment
    sequences that completed no packet, packets whose CRC32 does not
    match, and packets per route (`routes["10->2:1"]`).
* - `hk(board)`
  - Housekeeping records the board sent on the bus to any node, decoded
    with its codec, each with the time it finished on the wire and its
    destination (`dst`). The harness only listens, so this reads records
    two emulated boards exchange.
```

## CSP nodes

```{list-table}
:header-rows: 1
:widths: 40 60

* - Member
  - Meaning
* - `ping(target, size: 10, timeout: 2 s)`
  - Round trip in seconds, from the request leaving to the reply's last
    byte arriving. The request carries `size` bytes counting 0 to 255 and
    wrapping, or the bytes `payload:` gives.
* - `request(target, port, data, timeout: 2 s, crc: false,
    priority: 2)`
  - One request and its reply on a plain connection; returns the reply's
    payload.
* - `hk(target, port, timeout: 2 s)`
  - Request housekeeping from a board and decode it, with the request and
    codec in the board's `csp.housekeeping`, sent to its port unless
    `port` names another.
* - `hk_published(target, port)`
  - Housekeeping that boards sent this node unasked, decoded; both
    arguments are optional filters.
* - `connect(target, port, rdp: true, crc: true, timeout)`
  - A connection with `send(data)`, `read(timeout)`,
    `transaction(data, timeout)`, `close()` and `port`, the connection's
    own source port.
* - `send(target, port, data, sport, priority: 2, crc: false)`
  - One packet without a connection, to a board, an address or the
    broadcast address 31; priority runs from 0 (critical) to 3 (low).
* - `receive(port, source, sport, timeout: 2 s, since)`
  - The first packet outside any connection that matches the filters
    given, arriving from `since` on: `{time, source, sport, dport,
    priority, data}`.
* - `ident(target)`, `route_set(target, destination, interface)`,
    `clock(target, seconds)`
  - CSP management requests: the target's identity (hostname, model,
    revision, build date and time), a route change, and its clock, set
    first when `seconds` is given.
* - `link_down()`, `link_up()`, `linked`
  - Cut and restore the node's link, as at a loss of signal. While it is
    down the node sends nothing and receives nothing, and a partial packet
    in flight is dropped.
* - `stats`, `received`, `name`, `address`
  - Counters (`sent`, `received`, `crc_errors`, `retransmissions`,
    `unmatched`, `pings_answered`, `lost_sent`, `lost_bytes`); packets
    that matched no connection, each `{time, source, sport, dport, data}`.
```

A node takes packets sent to the broadcast address 31 only when its
declaration says so, `csp(address: 10, link: a.kiss, broadcast: true)`;
otherwise `receive` and `received` never see them.

A target is a board or a CSP address. A node answers pings sent to it. RDP
options start from libcsp 1.6's defaults (a window of 4, a 10 s connection
timeout, a 1 s packet timeout, and delayed acknowledgments sent after
250 ms or every 2 packets); the target board's `csp.rdp` overrides them,
and the node's declaration overrides both.
