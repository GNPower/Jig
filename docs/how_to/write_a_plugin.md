# Write a board plugin

A board plugin is Python code that a board brings along. It gives plans
functions and board members the core language lacks, decoders for the
board's own data formats, and protocol layers or device families Jig does
not ship. This guide writes the ZedBoard
example's plugin: a decoder for the 36-byte status record, a plan function
and a board member.

## How Jig loads a plugin

The board file names the plugin, relative to the board file: a `.py`
file or a package directory holding `__init__.py`.

```jig
board "zedboard" {
    plugin: "../zedboard.py"
    device zynq: zynq7000(part: "XC7Z020-CLG484",
                          clocks: {cpu_3x2x: 333.333333 MHz,
                                   cpu_1x: 111.111111 MHz}) {
        firmware: {app: "firmware/build/zed-node-1.elf"}
    }
}
```

Jig imports each plugin once per process, under a module name of its own,
and calls its `register(api)` function before the first plan step runs.
What `register` adds through `api` takes effect when it returns. A plugin
file can import its sibling modules relatively (`from . import codec`);
Jig adds nothing to `sys.path`, so two boards' plugins never shadow each
other's modules. Import only `jig.plugin` from Jig: nothing else in the
package is public to plugins, and other modules may change in any release.

## 1. Decode the status record

The example's firmware answers a 1-byte request, `0x01`, on CSP port 10
with 36 big-endian bytes. A codec turns that payload into a record:

```python
"""Board plugin for the ZedBoard example."""

from __future__ import annotations

import struct

from jig.plugin import DslError, PluginApi, Record

JIG_PLUGIN_API = 2

STATUS = struct.Struct(">IBBBBIIIIIII")
STATUS_FIELDS = (
    "magic", "version", "node", "can_tec", "can_rec", "uptime_ms",
    "can_sent", "can_received", "kiss_sent", "kiss_received",
    "routed", "crc_errors",
)
MAGIC = 0x5A454453  # "ZEDS"


def decode_status(payload: bytes) -> Record:
    if len(payload) != STATUS.size:
        raise DslError(f"a status record is 36 bytes, not {len(payload)}")
    fields = dict(zip(STATUS_FIELDS, STATUS.unpack(payload), strict=True))
    if fields["magic"] != MAGIC:
        raise DslError(f"status magic 0x{fields['magic']:08x}")
    return Record(fields)


def status_codec(context):
    """One stream of status records: each decodes on its own."""
    return decode_status


def register(api: PluginApi) -> None:
    api.hk_codec("zed-status", status_codec)
```

A codec is a factory. Jig calls it once per stream of records, a harness
node asking one board or one `bus.hk` call, with the board's name, its
directories, the housekeeping port and any extra keys of the board's
housekeeping map. It calls the decoder the codec returns on each payload,
in order. A format
that sends differences between records keeps its state in the decoder;
the status record needs none, so this codec returns the same function
every time.

`JIG_PLUGIN_API = 2` states the API version the plugin was written for, so
a later Jig can refuse it with a clear message instead of failing inside a
plan. A `DslError` from a decoder means the plan called it wrongly: the
step and its test end with ERROR, and `try` does not catch it. For data
that is wrong, raise `JigError`, which `jig.plugin` also exports: the
step fails, the test ends with FAIL, and a plan's `try` can catch it.

The board file then names the codec, the port and the request in its
device's CSP layer. The request is bytes, hex digits, a byte or a list of
bytes:

```jig
board "zedboard" {
    option node: choice(1, 2)
    device zynq: zynq7000(part: "XC7Z020-CLG484",
                          clocks: {cpu_3x2x: 333.333333 MHz,
                                   cpu_1x: 111.111111 MHz}) {
        firmware: {app: "firmware/build/zed-node-{node}.elf"}
        protocol csp(address: node, housekeeping: {port: 10, request: x"01",
                                                   codec: "zed-status"})
        interface can0: csp over cfp
    }
    external link can: can(bitrate: 1 Mbit/s, members: [zynq.can0])
}
```

`host.hk(b)` in a plan now sends `0x01` to port 10 of zed-b and returns
the decoded record, and `can0.hk(b)` decodes the records zed-b sent across
the bus to anyone.

## 2. Add a plan function

A function the plugin registers is visible to every plan that uses a board
with this plugin. Function names are global to a plan, so two boards whose
plugins define one name can't share a system; two instances of one board,
such as zed-a and zed-b, load the same plugin file, and the same function
registered twice counts once.
Keep functions for helpers that don't belong to one board, like this one,
which computes how many CAN frames CSP's fragmentation protocol uses for a
payload:

```python
import math


def cfp_frames(size: int) -> int:
    """CAN frames for a CSP payload of `size` bytes: the first frame
    carries 2 payload bytes, each later frame up to 8."""
    if size < 0:
        raise DslError(f"cfp_frames: size {size} is negative")
    return 1 + math.ceil(max(size - 2, 0) / 8)
```

Register it inside `register` with `api.function("cfp_frames",
cfp_frames)`. A plan can then check a fragmented ping exactly:

```jig
test "a 200-byte ping takes 26 frames each way" {
    let frames = can0.frames
    host.ping(b, size: 200)
    check can0.frames - frames == 2 * cfp_frames(200)
}
```

Functions take and return numbers, text, bytes, lists and records. A
function that has to wait in simulated time is a coroutine (`async def`)
and runs on the simulation's scheduler.

## 3. Add a board member

A member extends the boards that load this plugin and no others, so two
board types can each define a member with the same name:

```python
import re


async def uptime(board) -> float:
    """Seconds since boot, as the console's uptime command reports them."""
    found = re.search(r"uptime (\d+) ms", await board.cli("uptime"))
    if found is None:
        raise DslError(f"{board.name}: no uptime in the console output")
    return int(found.group(1)) / 1000
```

Register it with `api.member("board", "uptime", uptime)`. The function
receives the board handle first, with the members a plan sees, and plans
call it as `a.uptime()`. With `attribute=True`, a plan reads a member like
a field instead, `a.uptime`, and Jig calls the function with the handle
alone; such a function can't wait, so it must not be a coroutine.

A member's name can't be one the handle already has, such as `reset` or
`ping`, or one an extension registered, such as `address`, `packets`,
`traffic` or `hk`. A board member can't be called `console` either, since
plans reach the board's link of that name there, and a board whose
device, peripheral or link has a member's name fails to load with both
named. A function's name can't be a reserved word, and one that a
built-in function already has (`len`, `crc32`) is an error once a plan
uses the board.

## 4. Test the plugin without Renode

Codecs and functions are plain Python and need no emulator. Load the
plugin file the way a test sees fit and call them:

```python
import importlib.util
import struct
from pathlib import Path

PLUGIN = Path(__file__).parents[1] / "boards" / "zedboard.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("zedboard_plugin", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_status_record_decodes() -> None:
    plugin = load_plugin()
    payload = struct.pack(
        ">IBBBBIIIIIII", 0x5A454453, 1, 2, 0, 0, 1500, 10, 12, 0, 0, 0, 0
    )
    status = plugin.decode_status(payload)
    assert status.node == 2
    assert status.uptime_ms == 1500


def test_cfp_frames_match_the_firmware() -> None:
    plugin = load_plugin()
    assert [plugin.cfp_frames(n) for n in (0, 2, 3, 200, 256)] == [
        1, 1, 2, 26, 33,
    ]
```

For procedures in `.jig` library files, `jig.testing.run_source` runs a
plan that declares no system on simulated time alone and returns its
report ({doc}`../api_reference/testing`).

## What else a plugin can register

```{list-table}
:header-rows: 1
:widths: 35 65

* - Call
  - Adds
* - `api.function(name, fn)`
  - A plan function.
* - `api.member(handle_type, name, fn, attribute=False)`
  - A member of this plugin's `board` handles, or of `node` or `bus`
    handles; a system's CAN links are `bus` handles.
* - `api.hk_codec(name, codec)`
  - A codec that a board's `protocol csp(housekeeping: {codec: ...})` can
    name.
* - `api.node_kind(name, constructor)`
  - A kind of harness node a system declaration can create, next to
    `csp(...)`. The constructor returns a node declaration; when the
    system starts, Jig calls its handle's `start(system, link)` with the
    link the node joined: the link's name and kind, the harness's port on
    it, and the protocol layers under the node.
* - `api.protocol(name, runs_on=..., provides=..., parse=None)`
  - A protocol layer for stacks, such as `interface uart2: mine over kiss`:
    what it runs on and provides (`"byte stream"`, `"packets"`), and a
    function that checks its arguments.
* - `api.bus_observer(name, factory)`
  - Code that runs for every CAN bus of a simulation that includes the
    board.
* - `api.device_family(name, factory)`
  - The driver for `device NAME: name(...)` on this plugin's boards only,
    where it takes the place of a family Jig ships.
* - `api.zynq_hooks(images=..., boot_table=..., qspi_image=...)`
  - The board's own firmware images, boot metadata and Quad-SPI flash
    image, for the Zynq-7000 driver.
* - `api.background(handle_type, name, action, every=None)`
  - A member that starts work beside the plan and returns a handle to stop
    it, such as a heartbeat packet the board's network expects every few
    seconds.
```

`jig.plugin` also re-exports the types plugin code returns, raises or
receives (`Record`, `Regex`, `Profile`, `DslError`, `ToolchainError`,
`HkContext` and others) and `cached()`, for build steps whose output
should be kept between runs. A board handle gives plugin code its
`definition` (the board, with `firmware`, `boot`, `storage` and
`section("csp")` for its one device), `config`, `root`, `workdir`,
`console` and `emit(kind, detail)`, which writes an event into the run's
event log.
{doc}`../api_reference/plugin` lists the whole module.

## The link a node joined

When the system starts, a CSP node's handle keeps the link Jig hands it as
`node.link`, a `NodeLink`: the system link's `name` and `kind` (`"uart"`
or `"can"`), the harness's `port` on it, the `layers` under the node, and
`board_links`, a `BoardLinkRef` for each board's link the system link
joins. A `BoardLinkRef` gives the board's name in the system (`machine`),
the link's name in its board file (`name`) and the device interfaces the
link joins, as the board file writes them (`interfaces`). In the example's
`multiboard.jig`, `host.link` is `ground`, a `uart` link whose one board
link is `a.kiss`, with the interfaces `("zynq.uart0",)`.

A node member that only works on one board's serial link checks the link
before it sends:

```python
def announce(node, board) -> None:
    """A packet to port 20 of the board, from a node on its UART link."""
    link = node.link
    reaches = any(ref.machine == board.name for ref in link.board_links)
    if link.kind != "uart" or not reaches:
        raise DslError(f"announce: {node.name} is not on a UART link of {board.name}")
    node.send(board, 20, b"\x01")
```

Registered with `api.member("node", "announce", announce)`,
`host.announce(a)` sends the packet, and `host.announce(b)` ends the test
with ERROR and that message. A plugin's own node kind gets the same
`NodeLink` in its handle's `start(system, link)`.
