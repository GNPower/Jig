# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Graham Power
"""Board plugin for the ZedBoard example, shared by zed-a and zed-b.

Registers:

* the housekeeping codec ``zed-status``: the 36-byte status record the
  firmware sends from port 10 in answer to the 1-byte request 0x01, decoded
  into a record (``ground.hk(a)``, ``can0.hk(b)``);
* the plan function ``cfp_frames(size)``: how many CAN frames CSP's CAN
  fragmentation protocol uses for a payload of ``size`` bytes;
* the board member ``uptime()``: seconds since boot, from the console's
  ``uptime`` command (``a.uptime()``).

The status layout and the fragmentation rule are the firmware's; the
example's README.md describes both.
"""

from __future__ import annotations

import math
import re
import struct

from jig.plugin import BoardHandle, DslError, HkContext, HkDecoder, PluginApi, Record

JIG_PLUGIN_API = 1

STATUS = struct.Struct(">IBBBBIIIIIII")  # 36 bytes, big-endian
STATUS_FIELDS = (
    "magic",
    "version",
    "node",
    "can_tec",
    "can_rec",
    "uptime_ms",
    "can_sent",
    "can_received",
    "kiss_sent",
    "kiss_received",
    "routed",
    "crc_errors",
)
MAGIC = 0x5A454453  # "ZEDS"
VERSION = 1

FIRST_FRAME_BYTES = 2  # the first CFP frame: CSP header, length, 2 payload bytes
FRAME_BYTES = 8  # every later frame


def decode_status(payload: bytes) -> Record:
    """One status record, by field name; a payload of another length, magic
    number or layout version fails the plan step that asked for it."""
    if len(payload) != STATUS.size:
        raise DslError(f"a status record is {STATUS.size} bytes, not {len(payload)}")
    fields = dict(zip(STATUS_FIELDS, STATUS.unpack(payload), strict=True))
    if fields["magic"] != MAGIC:
        raise DslError(f"status magic 0x{fields['magic']:08x}, expected 0x{MAGIC:08x}")
    if fields["version"] != VERSION:
        raise DslError(f"status layout version {fields['version']}, expected {VERSION}")
    return Record(fields)


def status_codec(context: HkContext) -> HkDecoder:
    """The ``zed-status`` codec: each record decodes on its own, so every
    stream gets the same decoder."""
    return decode_status


def cfp_frames(size: int) -> int:
    """CAN frames for a CSP payload of ``size`` bytes: the first frame carries
    2 payload bytes, each later frame up to 8."""
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise DslError(f"cfp_frames: size must be a whole number of bytes, not {size!r}")
    return 1 + math.ceil(max(size - FIRST_FRAME_BYTES, 0) / FRAME_BYTES)


async def uptime(board: BoardHandle) -> float:
    """Seconds since boot, as the console's ``uptime`` command reports them."""
    found = re.search(r"uptime (\d+) ms", await board.cli("uptime"))
    if found is None:
        raise DslError(f"{board.name}: no uptime in the console output")
    return int(found.group(1)) / 1000


def register(api: PluginApi) -> None:
    api.hk_codec("zed-status", status_codec)
    api.function("cfp_frames", cfp_frames)
    api.member("board", "uptime", uptime)
