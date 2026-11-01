# The localhost bridge and its security model

Jig runs Renode as a child process and controls it through a bridge: a
small server that Jig's C# harness starts inside Renode. This page
describes what the bridge accepts, who can reach it, and what running a
plan trusts.

## Why Jig has its own bridge

Renode's Robot Framework server, which Renode's own test suites drive it
through, has a keyword that runs IronPython code with full .NET access
inside Renode. In Renode 1.17.0 the server registers `http://*:<port>/`
on Linux and macOS, so it listens on every network interface. On Windows
it registers `http://localhost:<port>/`, yet it answered requests that
reached the machine's other addresses with `localhost` in their `Host`
header. Anyone who could reach the port during a run could run code as the
user running Renode. Jig does not start that server.

## What the bridge does

Jig starts Renode with `--disable-gui --console`: no window, and the
monitor on a pipe that Jig holds. With `--disable-gui` alone, Renode
1.17.0 serves its monitor over telnet on port 1234 of every interface.
Renode also gets a start-up script in the run directory,
`jig-bootstrap.py`, which compiles Jig's C# models and then runs one
monitor command, `emulation StartJigBridge "bridge.json"`, to start the
bridge. The bridge then:

- listens on `127.0.0.1` only, on a port the operating system picks;
- writes `bridge.json` into the run directory: the protocol version, the
  host and port, a random token of 64 hex digits (256 bits) made for this
  run, Renode's process ID and how long Jig's models took to compile. On
  Linux and macOS only the owner can read the file;
- serves one client. Every request carries the token, which the bridge
  compares in constant time: a wrong token gets an `auth` error and the
  connection closes, and a second client gets a `busy` error. Until a
  connection sends the token, the bridge reads at most 64 KiB from it and
  drops it when 10 seconds pass without a request;
- runs the client's monitor commands, IronPython snippets and model
  compiles one at a time, on the monitor's own thread, through the same
  Renode internals the monitor and Robot Framework use, with the same
  error texts;
- reports its protocol version when the client connects, and Jig stops
  at once when it differs from its own, so a mismatched Jig and model set
  fail before a plan starts;
- makes Renode exit when its client disconnects without saying goodbye,
  or when no client has connected within 60 seconds, so an emulation
  never outlives the process that drives it.

## Who can reach it

```{list-table}
:header-rows: 1
:widths: 35 65

* - Who
  - Can they control Renode?
* - Another machine on the network
  - No. The bridge listens on the loopback interface only.
* - Another user on the same machine
  - Not without the token. Loopback ports are open to every local user,
    so the token is the protection: keep run directories where other
    users cannot read them. On Linux and macOS the token file is
    owner-only; on Windows it takes the permissions of the directory it
    is in.
* - A process running as you
  - Yes. It can read the token, but it can already run any code you can,
    so the bridge gives it nothing new.
```

## What a run trusts

Running a plan runs code, and Jig treats every input below as code you
chose to run:

- plans, which can send any Renode monitor command through
  `board.monitor(...)`;
- board files and their plugins, which are Python that Jig imports;
- C# models, which Renode compiles and runs inside its process;
- firmware, which runs inside the emulator, with no more access to the
  host than Renode's models give it;
- build commands that board files name, which Jig runs on the host.

Run plans and board repositories you would run as programs. Renode
itself comes from its GitHub releases, checked against a size and SHA-256
table in Jig's source before Jig unpacks it
({doc}`../getting_started/renode`).

Report security problems as {doc}`../project/security` describes.
