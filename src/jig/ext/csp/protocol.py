"""CubeSat Space Protocol as libcsp 1.6 speaks it.

Wire formats, from libcsp 1.6's sources:

* Header: one 32-bit word, network byte order: priority [31:30], source
  [29:25], destination [24:20], destination port [19:14], source port [13:8],
  flags [7:0] (HMAC 0x08, XTEA 0x04, RDP 0x02, CRC32 0x01).
* KISS (``csp_if_kiss.c``): FEND 0xC0, TNC_DATA 0x00, then the header, the
  payload and a CRC-32C of the payload alone (big-endian), FEND/FESC escaped,
  closed by FEND. Every KISS packet carries the CRC whatever the header flags
  say (``csp_crc32_append(packet, false)`` in ``csp_kiss_tx``).
* CAN fragmentation protocol (``csp_if_can.c``, ``csp_if_can.h``): 29-bit
  extended identifiers holding source [28:24], destination [23:19], type [18]
  (0 BEGIN, 1 MORE), frames remaining [17:10] and a 10-bit transfer identifier
  [9:0]. The BEGIN frame carries the header, the payload length (16-bit,
  big-endian) and the first 2 payload bytes; MORE frames carry 8 bytes each.

* CRC32 option (header flag 0x01): ``csp_send_direct`` appends a CRC-32C of
  the payload, big-endian, before the packet reaches an interface; on KISS the
  interface's own CRC follows it. An RDP connection (flag 0x02) puts its 5-byte
  trailer (``jig.ext.csp.rdp``) before that CRC.

CRC-32C is the Castagnoli polynomial (reflected 0x82F63B78) as
``csp_crc32_memory`` computes it.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

# Priorities and flags (csp_types.h).
PRIORITY_CRITICAL = 0
PRIORITY_HIGH = 1
PRIORITY_NORMAL = 2
PRIORITY_LOW = 3

FLAG_HMAC = 0x08
FLAG_XTEA = 0x04
FLAG_RDP = 0x02
FLAG_CRC32 = 0x01

# Standard service ports (csp_types.h CSP_CMP .. CSP_UPTIME).
PORT_CMP = 0
PORT_PING = 1
PORT_PS = 2
PORT_MEMFREE = 3
PORT_REBOOT = 4
PORT_BUF_FREE = 5
PORT_UPTIME = 6

MAX_ADDRESS = 31
MAX_PORT = 63
BROADCAST = 31  # the broadcast address (csp_types.h CSP_BROADCAST_ADDR)


def _crc32c_table() -> list[int]:
    table = []
    for index in range(256):
        crc = index
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC32C = _crc32c_table()


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


@dataclass(frozen=True)
class CspId:
    src: int
    dst: int
    dport: int
    sport: int
    priority: int = PRIORITY_NORMAL
    flags: int = 0

    def __post_init__(self) -> None:
        for name, value, limit in (
            ("src", self.src, MAX_ADDRESS),
            ("dst", self.dst, MAX_ADDRESS),
            ("dport", self.dport, MAX_PORT),
            ("sport", self.sport, MAX_PORT),
            ("priority", self.priority, 3),
            ("flags", self.flags, 0xFF),
        ):
            if not 0 <= value <= limit:
                raise ValueError(f"CSP {name} {value} outside 0-{limit}")

    def pack(self) -> bytes:
        word = (
            (self.priority << 30)
            | (self.src << 25)
            | (self.dst << 20)
            | (self.dport << 14)
            | (self.sport << 8)
            | self.flags
        )
        return struct.pack(">I", word)

    @classmethod
    def unpack(cls, data: bytes) -> CspId:
        (word,) = struct.unpack(">I", data[:4])
        return cls(
            src=(word >> 25) & 0x1F,
            dst=(word >> 20) & 0x1F,
            dport=(word >> 14) & 0x3F,
            sport=(word >> 8) & 0x3F,
            priority=(word >> 30) & 0x3,
            flags=word & 0xFF,
        )

    def reply(self, flags: int | None = None) -> CspId:
        """The identifier of an answer to this packet."""
        return CspId(
            src=self.dst,
            dst=self.src,
            dport=self.sport,
            sport=self.dport,
            priority=self.priority,
            flags=self.flags if flags is None else flags,
        )


@dataclass(frozen=True)
class Packet:
    id: CspId
    data: bytes


# ------------------------------------------------------------------- KISS

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD
TNC_DATA = 0x00


def crc32_append(data: bytes) -> bytes:
    """``data`` with its CRC-32C appended big-endian (csp_crc32_append, header excluded)."""
    return data + struct.pack(">I", crc32c(data))


def crc32_strip(data: bytes) -> bytes | None:
    """``data`` without its trailing CRC-32C, or None when the CRC does not match."""
    if len(data) < 4:
        return None
    body, crc = data[:-4], data[-4:]
    return body if struct.unpack(">I", crc)[0] == crc32c(body) else None


def kiss_encode(packet: Packet) -> bytes:
    """A complete KISS frame for one CSP packet (CRC-32C appended)."""
    body = packet.id.pack() + packet.data + struct.pack(">I", crc32c(packet.data))
    escaped = bytearray()
    for byte in body:
        if byte == FEND:
            escaped += bytes((FESC, TFEND))
        elif byte == FESC:
            escaped += bytes((FESC, TFESC))
        else:
            escaped.append(byte)
    return bytes((FEND, TNC_DATA)) + bytes(escaped) + bytes((FEND,))


class KissDecoder:
    """Turns a byte stream into CSP packets; frames with a bad CRC are counted and dropped."""

    def __init__(self) -> None:
        self._frame = bytearray()
        self._in_frame = False
        self._escape = False
        self.crc_errors = 0
        self.short_frames = 0

    def feed(self, data: bytes) -> Iterator[Packet]:
        for packet, _index in self._feed(data):
            yield packet

    def feed_timed(self, data: bytes, stamps: list[int]) -> Iterator[tuple[Packet, int]]:
        """Like ``feed``; each packet comes with the stamp of the byte that closed it."""
        for packet, index in self._feed(data):
            yield packet, stamps[index]

    def _feed(self, data: bytes) -> Iterator[tuple[Packet, int]]:
        for index, byte in enumerate(data):
            if byte == FEND:
                packet = self._finish() if self._in_frame and self._frame else None
                # Reset before yielding: the consumer may feed more data first.
                self._frame.clear()
                self._in_frame = True
                self._escape = False
                if packet is not None:
                    yield packet, index
                continue
            if not self._in_frame:
                continue
            if self._escape:
                self._escape = False
                if byte == TFEND:
                    self._frame.append(FEND)
                elif byte == TFESC:
                    self._frame.append(FESC)
                else:
                    self._frame.append(byte)
                continue
            if byte == FESC:
                self._escape = True
                continue
            self._frame.append(byte)

    def _finish(self) -> Packet | None:
        frame = bytes(self._frame)
        if frame[:1] != bytes((TNC_DATA,)):
            return None
        body = frame[1:]
        if len(body) < 8:
            self.short_frames += 1
            return None
        payload, crc = body[4:-4], body[-4:]
        if struct.unpack(">I", crc)[0] != crc32c(payload):
            self.crc_errors += 1
            return None
        return Packet(CspId.unpack(body[:4]), payload)


# -------------------------------------------------------------------- CAN

CFP_BEGIN = 0
CFP_MORE = 1
CFP_OVERHEAD = 6  # header + 16-bit length
CAN_FRAME_BYTES = 8
CFP_MAX_PAYLOAD = (1 << 8) * CAN_FRAME_BYTES - CFP_OVERHEAD


def cfp_id(src: int, dst: int, frame_type: int, remain: int, ident: int) -> int:
    return (
        ((src & 0x1F) << 24)
        | ((dst & 0x1F) << 19)
        | ((frame_type & 0x1) << 18)
        | ((remain & 0xFF) << 10)
        | (ident & 0x3FF)
    )


def cfp_fields(can_id: int) -> tuple[int, int, int, int, int]:
    """(source, destination, type, remain, identifier) of a CFP frame."""
    return (
        (can_id >> 24) & 0x1F,
        (can_id >> 19) & 0x1F,
        (can_id >> 18) & 0x1,
        (can_id >> 10) & 0xFF,
        can_id & 0x3FF,
    )


def cfp_fragments(packet: Packet, ident: int, via: int | None = None) -> list[tuple[int, bytes]]:
    """CAN frames (identifier, data) for one packet, as ``csp_can_tx`` sends them."""
    length = len(packet.data)
    if length > CFP_MAX_PAYLOAD:
        raise ValueError(f"a CFP packet carries at most {CFP_MAX_PAYLOAD} bytes, not {length}")
    dst = packet.id.dst if via is None else via
    first = min(length, CAN_FRAME_BYTES - CFP_OVERHEAD)
    remain = (length + CFP_OVERHEAD - 1) // CAN_FRAME_BYTES
    frames = [
        (
            cfp_id(packet.id.src, dst, CFP_BEGIN, remain, ident),
            packet.id.pack() + struct.pack(">H", length) + packet.data[:first],
        )
    ]
    sent = first
    while sent < length:
        count = min(CAN_FRAME_BYTES, length - sent)
        remain = (length - sent - count + CAN_FRAME_BYTES - 1) // CAN_FRAME_BYTES
        frames.append(
            (cfp_id(packet.id.src, dst, CFP_MORE, remain, ident), packet.data[sent : sent + count])
        )
        sent += count
    return frames


class CfpReassembler:
    """Rebuilds packets from CFP frames, keyed by source and transfer identifier."""

    def __init__(self) -> None:
        self._pending: dict[tuple[int, int], tuple[CspId, int, bytearray, int]] = {}
        self.dropped = 0

    def feed(self, can_id: int, data: bytes) -> Packet | None:
        src, _dst, frame_type, remain, ident = cfp_fields(can_id)
        key = (src, ident)
        if frame_type == CFP_BEGIN:
            if len(data) < CFP_OVERHEAD:
                self.dropped += 1
                return None
            header = CspId.unpack(data[:4])
            (length,) = struct.unpack(">H", data[4:6])
            buffer = bytearray(data[CFP_OVERHEAD:])
            if remain == 0 or len(buffer) >= length:
                self._pending.pop(key, None)
                return Packet(header, bytes(buffer[:length]))
            self._pending[key] = (header, length, buffer, remain)
            return None
        entry = self._pending.get(key)
        if entry is None:
            self.dropped += 1
            return None
        header, length, buffer, expected_remain = entry
        if remain != expected_remain - 1:
            self._pending.pop(key, None)
            self.dropped += 1
            return None
        buffer += data
        if remain == 0 or len(buffer) >= length:
            self._pending.pop(key, None)
            return Packet(header, bytes(buffer[:length]))
        self._pending[key] = (header, length, buffer, remain)
        return None
