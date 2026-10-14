"""The example status service's reply (port 10, request 0x01): 36 bytes,
big-endian: magic "ZEDS", version, node address, CAN transmit and receive
error counters, uptime in milliseconds, CAN frames sent and received, KISS
frames sent and received, packets routed, CRC errors."""

from __future__ import annotations

import struct

LAYOUT = struct.Struct(">4sBBBBIIIIIII")
FIELDS = (
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
MAGIC = b"ZEDS"


def encode(**fields: int) -> bytes:
    """A reply carrying ``fields`` (every other field 0)."""
    values = [fields.get(name, 0) for name in FIELDS[1:]]
    return LAYOUT.pack(MAGIC, *values)


def decode(payload: bytes) -> dict[str, object]:
    if len(payload) != LAYOUT.size or payload[:4] != MAGIC:
        raise ValueError(f"not a status reply: {payload[:8].hex()} ({len(payload)} bytes)")
    values: dict[str, object] = dict(zip(FIELDS, LAYOUT.unpack(payload), strict=True))
    values["magic"] = MAGIC.decode("ascii")
    return values
