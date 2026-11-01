# Functions and handles

A plan reaches the emulation only through the functions on this page and
the handles its system declaration creates: one per board, link and node,
and through a board, one per device, peripheral and link of the board.
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
  - Instance name, board name, boot mode, and the address of the board's
    `protocol csp(...)`.
* - A device, peripheral or link name
  - That part of the board: `a.zynq`, `a.flash0`, `a.console`, `a.can`
    (below).
* - `cli(command, settle: 0.3 s)`, `send(line)`,
    `expect(pattern, within: 10 s)`
  - The console members of the same names, on the board's one external
    UART link whose interface's stack has `cli`.
* - `set_sensor(name, volts)`
  - Set an analog input (a board file's `sensor` line); returns the code
    the converter will report, or `none` for a converter that reports no
    codes to the harness.
* - `sensor_code(name)`, `sensor_code(name, volts: v)`, `sensors`
  - The code an input reads now, or would read at `v`; the input names.
* - `drive(sensor, profile, every: 1 s)`
  - Set an input from a profile every `every` seconds in the background;
    returns a handle with `name`, `running`, `updates` and `stop()`.
* - `set_time(iso)`, `time()`
  - Set and read the real-time clock the board file's `rtc` names, as
    calendar time.
* - `reset()`
  - Pulse the system-reset input.
* - `power_off()`, `power_on()`, `power_cycle(off: 1 s)`
  - Remove power, restore it, or both with `off` seconds between. Without
    power the board's cores are halted while simulated time runs on, and
    every peripheral whose model implements Jig's power-cycle contract
    restarts, except those on the board's `backup_supply`. Restoring
    power runs the device's power-on sequence from reset: a board that
    boots from its own flash starts its firmware again, and a Zynq-7000
    booted over JTAG comes back waiting in its BootROM for its debugger
    until `boot()` runs its boot flow again. A board that started without
    power runs its whole boot flow at its first `power_on()`.
* - `boot()`
  - Run the board's boot flow again, as at the start of the run, for any
    boot mode and without a reset first: for a JTAG boot, the debugger's
    load. An error while the board has no power.
* - `powered`
  - Whether the board has power: false after `power_off()`, and from the
    start for a board whose `power` setting is `"off"`.
* - `image(role)`
  - A firmware image by role, as an update would send it, from the board's
    image builder.
* - `monitor(command)`
  - A Renode monitor command in the board's context, for diagnostics.
```

A board's plugin can add members to its own boards, and only to those
({doc}`../how_to/write_a_plugin`). A board whose device, peripheral or
link has the name of a board member, or of a member its plugin adds, fails
to load with both named.

### Devices, peripherals and links of a board

```{list-table}
:header-rows: 1
:widths: 40 60

* - Handle
  - Members
* - A device, `a.zynq`
  - `name`, `family` (`zynq7000`, `stm32`), `monitor(command)`.
* - A peripheral, `a.flash0`
  - `name`, `model`.
* - An external UART link, `a.console`, `a.kiss`
  - The console members (below). The harness is on every external UART
    link from the start of the run.
* - An external CAN link that a system link joins, `a.can`
  - That system link's handle (see CAN links).
* - Any other link
  - `name`, `kind`, `machine`, `external`. Its traffic is out of a plan's
    reach: an external CAN link joins the harness through a system link,
    and a link inside the board needs a probe, which a later release adds.
```

## Consoles

```{list-table}
:header-rows: 1
:widths: 40 60

* - Member
  - Meaning
* - `cli(command, settle: 0.3 s)`
  - Clear the buffer, send the command, return everything printed until
    the line was quiet for `settle`, or, when the interface's `cli` layer
    names a `prompt`, through the prompt (within 10 s).
* - `send(line)`, `write(text)`
  - Send a line with the line ending of the `cli` layer in the
    interface's stack (CR by default), or characters as they are.
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
* - `press(key)`, `raw`, `raw_buffer`
  - With `vt100` in the interface's stack only: send a key by name; the
    bytes the board sent since the run started, as it sent them; and the
    bytes it sent since the last clear (below).
```

`cli` and `expect` look at the same buffer, which `cli` and `clear`
empty. A task that waits for lines while another task sends commands
should expect only lines that come after the last command it knows about.

Without `vt100`, a console reads the bytes as Latin-1 text, escape
sequences included.

### Consoles that run vt100

`vt100` in an interface's stack (`interface uart0: cli over vt100`)
renders what the firmware writes as a terminal would show it. Cursor
moves, line edits, erases and colors take effect, so `expect`, `seen`,
`cli` and `buffer` work on the text a person at a terminal reads. The
layer runs on any byte stream.

Line editors change their input line on the screen as keys arrive:
funbiscuit's embedded-cli echoes a character typed in the middle of a line
as `ESC [ @` and the character, and rewrites the whole line when the user
recalls a command. The bytes never hold the line as the screen shows it.

What the terminal renders:

- Text overwrites the screen at the cursor. CR returns to the start of the
  line, LF starts the next line (a bare LF reads like CR LF), BS moves left
  without erasing, and a tab moves to the next multiple of 8 columns, so it
  reads as spaces.
- The cursor moves (`ESC [ n A`, `B`, `C`, `D` and `G`, `ESC [ r ; c H` and
  `f`), saves and restores its place (`ESC 7` and `ESC 8`, `ESC [ s` and
  `ESC [ u`), inserts, deletes and erases characters (`ESC [ n @`, `P` and
  `X`) and erases in the line and on the screen (`ESC [ n K` and `J`).
  Colors, window titles and every other sequence are dropped. A sequence
  split between two reads of the line works.
- The screen has 24 rows, as a VT100's does. Lines that scroll off its top
  are history, and the cursor can't move back into them. Clearing the
  screen (`ESC [ 2 J`) moves what was on it into history, so `text` keeps
  what the board printed before it cleared the screen.
- Lines don't wrap, however long the firmware writes them.
- Spaces at the end of a line aren't text, except on the cursor's line up
  to the cursor: a prompt `"> "` keeps its space while the cursor waits
  after it.
- Each byte from 0x80 up reads as one Latin-1 character, as it does
  without `vt100`.

`clear()` marks where the cursor is. From then on `buffer` holds the
cursor's line from that place on, and every line the board writes, erases
or moves down onto with a line feed, each as it stands now. A line the
board rewrites from its start, such as a progress bar or a recalled
command, is in `buffer` whole. Lines the board doesn't touch stay out, so a
status line redrawn at the top of the screen doesn't bring back the lines
below it.

With a `prompt` in the `cli` layer, `cli` returns when the prompt stands
where the next command would be typed: at the end of the cursor's line,
with the cursor right after it, on a line the board reached after the
command went out. A line editor that redraws its prompt with a half-typed
command after it, as embedded-cli does when it prints a message, doesn't
end the wait.

`press(key)` sends the bytes a terminal sends for the key; an unknown name
is an error that lists the names:

```{list-table}
:header-rows: 1
:widths: 45 55

* - Names
  - Bytes
* - `up`, `down`, `right`, `left`
  - `ESC [ A`, `ESC [ B`, `ESC [ C`, `ESC [ D`
* - `home`, `end`
  - `ESC [ H`, `ESC [ F`
* - `insert`, `delete`, `page_up`, `page_down`
  - `ESC [ 2 ~`, `ESC [ 3 ~`, `ESC [ 5 ~`, `ESC [ 6 ~`
* - `tab`, `enter`, `backspace`, `escape`
  - HT (0x09), CR (0x0D), DEL (0x7F), ESC (0x1B)
* - `ctrl-a` to `ctrl-z`
  - 0x01 to 0x1A
```

Terminals differ on two of these. A VT100's backspace key sends BS (0x08);
Jig sends DEL, the erase character of a Unix terminal, and embedded-cli
and linenoise read either as backspace. A VT100 in new line mode sends CR
LF for its return key; Jig sends CR alone. `write` sends any other bytes.

A plan whose patterns matched the bytes needs these changes when its
console gains `vt100`:

- A pattern written across an escape sequence, such as
  `/jig\x1b\[0m> /`, no longer matches. Write the visible text: `"jig> "`.
- Lines end with `\n` alone: `/ok\r\n/` becomes `/ok\n/`.
- Spaces at the end of a line are gone: `/(\w\w )+$/` becomes
  `/(\w\w ?)+$/`.
- A tab reads as spaces: `/name\tvalue/` becomes `/name\s+value/`.

Read data a board prints, such as a memory dump or a table, from
`raw_buffer`, the bytes since the last `clear()` or `cli`, or from `raw`.
A line of the data that another writer's output reaches between the line's
CR and its LF is gone from the rendered text. Here a board answers `dump`
with four bytes per line, and another task logs a message after the first
line's CR:

```text
dump\r\n00 01 02 03\r[adc] sample ready\r\n\n04 05 06 07\r\n
```

The CR returns the cursor to the start of `00 01 02 03` and the message
overwrites it, so `cli("dump")` returns
`"dump\n[adc] sample ready\n\n04 05 06 07\n"`, while `raw_buffer` holds
every byte above. A message shorter than the line leaves the line's end
behind: `[adc] ok` turns it into `[adc] ok 03`.

### Background work

`drive` starts work that runs beside the plan. It belongs to the block
that started it: work started in `setup` runs through the whole plan, and
work started in a test stops when the test ends. If it fails, for example
on a sensor the board cannot set, the block running when Jig collects the
failure ends with ERROR and the reason, so work started in `setup` that
fails during a test makes that test ERROR. `handle.stop()` ends it sooner.

## CAN links

A system's `can` link has these members, and a plan reaches the same
handle through each board's CAN link that the system link joins:
`can0.frames` and `a.can.frames` are one count.

```{list-table}
:header-rows: 1
:widths: 40 60

* - Member
  - Meaning
* - `name`, `bitrate`
  - The system link's name and the bus's bit rate.
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
    codec of the board's `protocol csp(housekeeping: ...)`, sent to its
    port unless `port` names another.
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
declaration says so, `csp(address: 10, broadcast: true) over kiss`;
otherwise `receive` and `received` never see them.

A target is a board, one of its devices (`a.zynq`) or a CSP address. A
node answers pings sent to it. RDP options start from libcsp 1.6's
defaults (a window of 4, a 10 s connection timeout, a 1 s packet timeout,
and delayed acknowledgments sent after 250 ms or every 2 packets); the
`rdp` of the target board's `protocol csp(...)` overrides them, and the
node's declaration overrides both.
