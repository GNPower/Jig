"""CSP management protocol (CMP, port 0), as libcsp 1.6 speaks it.

Source: libcsp 1.6, ``include/csp/csp_cmp.h`` and the CMP handler in
``src/csp_service_handler.c``. A CMP message is a type byte (request 0x00,
reply 0xFF), a code byte and the code's packed body; a client sends one
request on a connection to port 0 and the node answers with a reply of the
same length. Three codes are covered:

* IDENT (1): the reply carries the node's hostname (20 bytes), model (30),
  revision (20), build date (12) and time (9), NUL padded.
* ROUTE_SET (2): sets the route to ``destination`` through ``interface`` (by
  name, 11 bytes) and the next hop ``via`` (0xFF: none). A node that cannot
  set the route sends no reply.
* CLOCK (6): seconds and nanoseconds, big-endian 32-bit words. Zero seconds
  reads the node's clock; any other value sets it first. The reply carries
  the clock as the node reads it afterwards.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

REQUEST = 0x00
REPLY = 0xFF

IDENT = 1
ROUTE_SET = 2
CLOCK = 6

HOSTNAME_LEN = 20
MODEL_LEN = 30
REVISION_LEN = 20
DATE_LEN = 12
TIME_LEN = 9
INTERFACE_LEN = 11
NO_VIA = 0xFF

_IDENT_BODY = HOSTNAME_LEN + MODEL_LEN + REVISION_LEN + DATE_LEN + TIME_LEN
IDENT_SIZE = 2 + _IDENT_BODY
ROUTE_SET_SIZE = 2 + 2 + INTERFACE_LEN
CLOCK_SIZE = 2 + 8

_NAMES = {IDENT: "IDENT", ROUTE_SET: "ROUTE_SET", CLOCK: "CLOCK"}


class CmpError(ValueError):
    """A CMP reply that does not answer the request."""


@dataclass(frozen=True)
class Ident:
    hostname: str
    model: str
    revision: str
    date: str
    time: str


@dataclass(frozen=True)
class Clock:
    seconds: int
    nanoseconds: int


def _text(field: bytes) -> str:
    return field.split(b"\0", 1)[0].decode("latin-1")


def ident_request() -> bytes:
    return bytes((REQUEST, IDENT)) + bytes(_IDENT_BODY)


def route_set_request(destination: int, interface: str, via: int = NO_VIA) -> bytes:
    name = interface.encode("ascii")
    if not 0 <= destination <= 0xFF or not 0 <= via <= 0xFF:
        raise ValueError(f"route_set: destination {destination} or via {via} outside 0-255")
    if not name or len(name) >= INTERFACE_LEN:
        raise ValueError(
            f"route_set: interface name {interface!r} must have 1 to {INTERFACE_LEN - 1} characters"
        )
    return bytes((REQUEST, ROUTE_SET, destination, via)) + name.ljust(INTERFACE_LEN, b"\0")


def clock_request(seconds: int = 0, nanoseconds: int = 0) -> bytes:
    if not 0 <= seconds <= 0xFFFFFFFF or not 0 <= nanoseconds < 1_000_000_000:
        raise ValueError(f"clock: {seconds} s {nanoseconds} ns is outside the clock's range")
    return bytes((REQUEST, CLOCK)) + struct.pack(">II", seconds, nanoseconds)


def check_reply(reply: bytes, code: int, size: int) -> bytes:
    """The reply's body after checking its type, code and length."""
    name = _NAMES.get(code, str(code))
    if len(reply) < size:
        raise CmpError(f"CMP {name} reply has {len(reply)} bytes, expected {size}")
    if reply[0] != REPLY or reply[1] != code:
        raise CmpError(f"CMP {name} reply starts {reply[:2].hex()}, expected ff{code:02x}")
    return reply[2:size]


def parse_ident(reply: bytes) -> Ident:
    body = check_reply(reply, IDENT, IDENT_SIZE)
    fields = []
    offset = 0
    for length in (HOSTNAME_LEN, MODEL_LEN, REVISION_LEN, DATE_LEN, TIME_LEN):
        fields.append(_text(body[offset : offset + length]))
        offset += length
    return Ident(*fields)


def parse_clock(reply: bytes) -> Clock:
    seconds, nanoseconds = struct.unpack(">II", check_reply(reply, CLOCK, CLOCK_SIZE))
    return Clock(seconds, nanoseconds)
