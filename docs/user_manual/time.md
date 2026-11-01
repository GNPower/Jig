# Simulated time and repeatability

Jig never waits on the host's clock. Every duration in a plan is time on
the emulated boards, and a run's result depends only on the plan, the
firmware, the models and the seed.

## Steps

The harness advances the emulation in steps of at most 10 ms of simulated
time, shorter when a wait ends sooner. Each step is one exchange with
Renode, which returns what every harness port saw during the step: the
bytes each UART sent, with a time stamp per byte, and the frames on each
CAN bus. Consoles, CSP nodes and the plan's tasks then run until they wait
again, before the next step starts.

That gives each kind of wait its precision:

- `wait 2 s` ends at exactly 2 s after it started.
- `expect`, `wait until` and a CSP reply finish at the end of the step in
  which their condition became true, at most 10 ms late; the round trip
  `ping` returns uses the time stamp of the reply's last byte, not the end
  of the step.
- `every` loops start their iterations on their grid.

A slow host makes a run take longer in wall time and changes nothing
else. `jig boot` prints how fast a board ran against real time.

## Time on a board

Renode advances each core by the instructions it executes, at the
`mips` rate of its device in the board file, and runs each
peripheral's timers from that time. A timing test therefore checks the
firmware against the models' clocks, not against the cycle counts of real
silicon.

## Several boards

Boards in one system run in one emulation. Each board gets a time source
of its own under the emulation's master clock, so its timers and its
peripherals' scheduled work follow its own cores rather than the slowest
core of all boards. The machines take turns through each quantum of
Renode's time, 100 µs by default, instead of running on parallel threads,
so an event one board sends another arrives at the same point in every
run. A frame crosses from one board to another at the receiver's next
quantum boundary, up to 200 µs after it left.

On a CAN bus, the harness puts frames on the wire at the bus bit rate.
Jig's CAN controller models time their own frames; frames from Renode's
other CAN models, which appear the moment firmware fills a mailbox, wait
in a queue per controller and go out back to back. Frames from different
senders are not arbitrated against each other.

## The seed

`jig run --seed N`, 0 by default, seeds Renode's emulation (`emulation
SetSeed`) and the harness's own random choices, such as the initial
sequence numbers of RDP connections. Two runs with one seed produce one
fingerprint; a different seed can change the fingerprint without anything
being wrong.
