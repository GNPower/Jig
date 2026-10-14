#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Graham Power
"""Host computer for the ZedBoard example.

Connects to zed-a's KISS link (the TCP socket renode/two-boards.resc opens on
port 4560) and speaks CSP 1.6 over KISS as CSP address 10. zed-a routes
packets for address 2 to zed-b over CAN, and zed-b's answers back.

Without a command it runs the demo and exits 0 when every check passes:

1. ping zed-a (1) and zed-b (2) with 32 bytes;
2. ask both for their status (port 10) and decode the answers;
3. ping zed-b with 200 bytes full of bytes KISS escapes, and check the echo.

Other commands: ``ping ADDR [--size N]``, ``status ADDR``, ``uptime ADDR``.
Standard library only; Python 3.11 or newer.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

# --------------------------------------------------------------- CRC-32C


def _crc32c_table() -> list[int]:
    table = []
    for index in range(256):
        crc = index
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_TABLE = _crc32c_table()


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli), as libcsp computes it; crc32c(b"123456789") == 0xE3069283."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


# ------------------------------------------------------------ CSP header

PORT_PING = 1
PORT_UPTIME = 6
PORT_STATUS = 10
PRIORITY_NORMAL = 2


@dataclass(frozen=True)
class Header:
    """CSP 1.6 header: priority 2 bits, source 5, destination 5,
    destination port 6, source port 6, flags 8; network byte order."""

    src: int
    dst: int
    dport: int
    sport: int
    priority: int = PRIORITY_NORMAL
    flags: int = 0

    def pack(self) -> bytes:
        word = (
            (self.priority & 0x3) << 30
            | (self.src & 0x1F) << 25
            | (self.dst & 0x1F) << 20
            | (self.dport & 0x3F) << 14
            | (self.sport & 0x3F) << 8
            | (self.flags & 0xFF)
        )
        return struct.pack(">I", word)

    @classmethod
    def unpack(cls, data: bytes) -> Header:
        (word,) = struct.unpack(">I", data[:4])
        return cls(
            src=(word >> 25) & 0x1F,
            dst=(word >> 20) & 0x1F,
            dport=(word >> 14) & 0x3F,
            sport=(word >> 8) & 0x3F,
            priority=(word >> 30) & 0x3,
            flags=word & 0xFF,
        )


# ------------------------------------------------------------------ KISS

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
TNC_DATA = 0x00


def kiss_encode(header: Header, payload: bytes) -> bytes:
    """FEND, command 0x00, header, payload, CRC-32C of the payload, FEND (escaped)."""
    body = header.pack() + payload + struct.pack(">I", crc32c(payload))
    out = bytearray((FEND, TNC_DATA))
    for byte in body:
        if byte == FEND:
            out += bytes((FESC, TFEND))
        elif byte == FESC:
            out += bytes((FESC, TFESC))
        else:
            out.append(byte)
    out.append(FEND)
    return bytes(out)


class KissDecoder:
    """Splits a byte stream into (header, payload) pairs; bad CRCs are counted."""

    def __init__(self) -> None:
        self._frame = bytearray()
        self._in_frame = False
        self._escaped = False
        self.crc_errors = 0

    def feed(self, data: bytes) -> list[tuple[Header, bytes]]:
        packets = []
        for byte in data:
            if byte == FEND:
                if self._in_frame and self._frame:
                    packet = self._finish(bytes(self._frame))
                    if packet is not None:
                        packets.append(packet)
                self._frame.clear()
                self._in_frame = True
                self._escaped = False
            elif not self._in_frame:
                continue
            elif self._escaped:
                self._escaped = False
                self._frame.append({TFEND: FEND, TFESC: FESC}.get(byte, byte))
            elif byte == FESC:
                self._escaped = True
            else:
                self._frame.append(byte)
        return packets

    def _finish(self, frame: bytes) -> tuple[Header, bytes] | None:
        if frame[0] != TNC_DATA or len(frame) < 9:
            return None
        body = frame[1:]
        payload, crc = body[4:-4], body[-4:]
        if struct.unpack(">I", crc)[0] != crc32c(payload):
            self.crc_errors += 1
            return None
        return Header.unpack(body), payload


def kiss_escapes(header: Header, payload: bytes) -> int:
    body = header.pack() + payload + struct.pack(">I", crc32c(payload))
    return sum(1 for byte in body if byte in (FEND, FESC))


# --------------------------------------------------------------- link


class LinkError(Exception):
    pass


class HostComputer:
    def __init__(self, host: str, port: int, address: int, timeout: float, verbose: bool) -> None:
        self.host = host
        self.port = port
        self.address = address
        self.timeout = timeout
        self.verbose = verbose
        self.decoder = KissDecoder()
        self.sock: socket.socket | None = None
        self._next_port = 0

    def connect(self, wait: float) -> None:
        deadline = time.monotonic() + wait
        while True:
            try:
                self.sock = socket.create_connection((self.host, self.port), timeout=2.0)
                return
            except OSError as error:
                if time.monotonic() >= deadline:
                    where = f"{self.host}:{self.port}"
                    raise LinkError(f"cannot connect to {where}: {error}") from error
                time.sleep(0.5)

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _source_port(self) -> int:
        self._next_port = (self._next_port + 1) % 16
        return 32 + self._next_port

    def request(
        self, dst: int, dport: int, payload: bytes, attempts: int = 3
    ) -> tuple[Header, bytes, float]:
        """Sends one packet and waits for the answer: (header, payload, seconds)."""
        assert self.sock is not None
        for _ in range(attempts):
            header = Header(src=self.address, dst=dst, dport=dport, sport=self._source_port())
            started = time.monotonic()
            self.sock.sendall(kiss_encode(header, payload))
            deadline = started + self.timeout
            while time.monotonic() < deadline:
                self.sock.settimeout(max(0.01, deadline - time.monotonic()))
                try:
                    data = self.sock.recv(4096)
                except TimeoutError:
                    break
                if not data:
                    raise LinkError("the simulator closed the connection")
                for reply, body in self.decoder.feed(data):
                    if (
                        reply.dst == self.address
                        and reply.src == dst
                        and reply.sport == dport
                        and reply.dport == header.sport
                    ):
                        return reply, body, time.monotonic() - started
                    if self.verbose:
                        print(f"  (ignored packet {reply})")
            if self.verbose:
                print(f"  no answer from {dst} port {dport} in {self.timeout} s, sending again")
        raise LinkError(f"no answer from {dst} port {dport} after {attempts} attempts")


# -------------------------------------------------------------- services

STATUS_REQUEST = b"\x01"
STATUS_MAGIC = 0x5A454453  # "ZEDS"
STATUS = struct.Struct(">IBBBBIIIIIII")  # 36 bytes, see the firmware's services.h
STATUS_FIELDS = (
    "magic",
    "version",
    "node",
    "can_tec",
    "can_rec",
    "uptime_ms",
    "can_tx",
    "can_rx",
    "kiss_tx",
    "kiss_rx",
    "routed",
    "crc_errors",
)


def decode_status(payload: bytes) -> dict[str, int]:
    if len(payload) != STATUS.size:
        raise LinkError(f"status answer has {len(payload)} bytes, expected {STATUS.size}")
    fields = dict(zip(STATUS_FIELDS, STATUS.unpack(payload), strict=True))
    if fields["magic"] != STATUS_MAGIC or fields["version"] != 1:
        found = f"magic 0x{fields['magic']:08X}, version {fields['version']}"
        raise LinkError(f"status answer has {found}")
    return fields


def format_status(fields: dict[str, int]) -> str:
    return (
        f"node {fields['node']}, uptime {fields['uptime_ms'] / 1000:.3f} s, "
        f"CAN sent {fields['can_tx']} received {fields['can_rx']} frames "
        f"(TEC {fields['can_tec']}, REC {fields['can_rec']}), "
        f"KISS sent {fields['kiss_tx']} received {fields['kiss_rx']} frames, "
        f"routed {fields['routed']}, CRC errors {fields['crc_errors']}"
    )


def ping_payload(size: int) -> bytes:
    """Cycles through FEND, FESC, TFEND, TFESC and a counter, so KISS has to
    escape two bytes in every five."""
    return bytes((FEND, FESC, TFEND, TFESC, index & 0xFF)[index % 5] for index in range(size))


def cfp_frames(size: int) -> int:
    return (size + 6 - 1) // 8 + 1


def ping(computer: HostComputer, dst: int, size: int) -> bool:
    payload = ping_payload(size)
    _, body, seconds = computer.request(dst, PORT_PING, payload)
    intact = body == payload
    escapes = kiss_escapes(Header(computer.address, dst, PORT_PING, 0), payload)
    links = f"{escapes} KISS escapes"
    if dst != 1:  # zed-a ends the KISS link; anything else is a CAN hop further
        links += f", {cfp_frames(size)} CAN frames"
    verdict = "payload intact" if intact else "PAYLOAD DIFFERS"
    timing = f"{len(body)} of {size} bytes in {seconds * 1000:.1f} ms"
    print(f"ping {dst}: {timing}, {verdict} ({links} each way)")
    return intact


def status(computer: HostComputer, dst: int) -> bool:
    _, body, _ = computer.request(dst, PORT_STATUS, STATUS_REQUEST)
    fields = decode_status(body)
    print(f"status {dst}: {format_status(fields)}")
    return fields["node"] == dst


def uptime(computer: HostComputer, dst: int) -> bool:
    _, body, _ = computer.request(dst, PORT_UPTIME, b"")
    if len(body) != 4:
        raise LinkError(f"uptime answer has {len(body)} bytes, expected 4")
    print(f"uptime {dst}: {struct.unpack('>I', body)[0]} s")
    return True


def demo(computer: HostComputer) -> bool:
    checks: list[tuple[str, Callable[[], bool]]] = [
        ("ping zed-a", lambda: ping(computer, 1, 32)),
        ("ping zed-b through zed-a", lambda: ping(computer, 2, 32)),
        ("status of zed-a", lambda: status(computer, 1)),
        ("status of zed-b", lambda: status(computer, 2)),
        ("200-byte ping to zed-b", lambda: ping(computer, 2, 200)),
    ]
    passed = 0
    for name, check in checks:
        try:
            ok = check()
        except LinkError as error:
            print(f"{name}: {error}")
            ok = False
        passed += ok
    print(f"{passed} of {len(checks)} checks passed")
    return passed == len(checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4560, help="zed-a's KISS socket (default 4560)")
    parser.add_argument(
        "--address", type=int, default=10, help="this computer's CSP address (default 10)"
    )
    parser.add_argument(
        "--timeout", type=float, default=5.0, help="seconds to wait for each answer"
    )
    parser.add_argument("--wait", type=float, default=30.0, help="seconds to wait for the socket")
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("demo", help="the checks listed above (default)")
    for name in ("ping", "status", "uptime"):
        sub = commands.add_parser(name)
        sub.add_argument("node", type=int, help="CSP address, 0 to 31")
        if name == "ping":
            sub.add_argument("--size", type=int, default=32, help="payload bytes (default 32)")
    args = parser.parse_args(argv)

    computer = HostComputer(args.host, args.port, args.address, args.timeout, args.verbose)
    try:
        computer.connect(args.wait)
        print(f"host computer {args.address} on {args.host}:{args.port}")
        if args.command in (None, "demo"):
            ok = demo(computer)
        elif args.command == "ping":
            ok = ping(computer, args.node, args.size)
        elif args.command == "status":
            ok = status(computer, args.node)
        else:
            ok = uptime(computer, args.node)
    except LinkError as error:
        print(f"host.py: {error}", file=sys.stderr)
        return 1
    finally:
        computer.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
