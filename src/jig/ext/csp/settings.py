"""The arguments of a device's ``csp`` protocol layer, and the housekeeping
codec contract::

    protocol csp(address: 5,                     # the device's CSP address, 0-31
                 housekeeping: {port: 10,        # what node.hk(board) and bus.hk(board) ask
                                request: "01",   # hex digits, a byte or a list of bytes
                                codec: "example-status",  # a codec a plugin registered
                                layout: "status.yaml"},   # any other key: a codec option
                 rdp: {window_size: 4, conn_timeout: 10 s, packet_timeout: 1 s,
                       delayed_acks: true, ack_timeout: 250 ms, ack_delay_count: 2})

RDP options for a connection come from libcsp 1.6's defaults, then the
target device's ``rdp``, then what the plan's ``csp(...)`` declaration sets.

A housekeeping codec is a function a plugin registers under a name
(``api.hk_codec(name, codec)``). Jig calls ``codec(context)`` once per
housekeeping stream (a harness node and the board it asks, or one
``bus.hk`` call) with an ``HkContext``, and calls what it returns with each
reply's payload, in order; the decoder returns the reply's fields as a
mapping. A codec that keeps no state returns the same decoder every time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jig.errors import ConfigError
from jig.ext.csp import protocol as csp

SECTION = "csp"


@dataclass(frozen=True)
class Housekeeping:
    port: int
    request: bytes
    codec: str
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CspSettings:
    address: int | None = None
    housekeeping: Housekeeping | None = None
    rdp: Mapping[str, Any] = field(default_factory=dict)  # RdpOptions fields the device sets

    def problems(self) -> list[str]:
        """What ``jig check`` reports: a codec no loaded plugin registered."""
        from jig.system import registry

        if self.housekeeping is None:
            return []
        try:
            registry.default().codec(self.housekeeping.codec)
        except ConfigError as exc:
            return [f"csp housekeeping: {exc}"]
        return []


@dataclass(frozen=True)
class HkContext:
    """What a housekeeping codec learns about the stream it decodes."""

    board: str  # the board definition's name
    directory: Path  # the directory holding its board file
    root: Path  # the board's root
    port: int  # the housekeeping port
    options: Mapping[str, Any]  # the other keys of the housekeeping map


HkDecoder = Callable[[bytes], Mapping[str, Any]]
HkCodec = Callable[[HkContext], HkDecoder]


def _request(value: Any, where: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bool):
        raise ConfigError(f"{where}: expected hex digits, a byte or a list of bytes")
    if isinstance(value, int):
        if not 0 <= value <= 0xFF:
            raise ConfigError(f"{where}: {value} is not a byte (0-255)")
        return bytes((value,))
    if isinstance(value, str):
        digits = "".join(value.split())
        if digits.lower().startswith("0x"):
            digits = digits[2:]
        try:
            return bytes.fromhex(digits)
        except ValueError:
            raise ConfigError(f'{where}: {value!r} is not hex digits such as "01"') from None
    if isinstance(value, list) and all(
        isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 0xFF for item in value
    ):
        return bytes(value)
    raise ConfigError(f"{where}: expected hex digits, a byte or a list of bytes, not {value!r}")


def _housekeeping(value: Any, where: str) -> Housekeeping:
    if isinstance(value, str):
        raise ConfigError(
            f"{where}: a map {{port, request, codec}}, not a file name "
            "(a codec option can name the file)"
        )
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a map {{port, request, codec}}")
    for key in ("port", "codec"):
        if key not in value:
            raise ConfigError(f"{where}: missing '{key}'")
    port = value["port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= csp.MAX_PORT:
        raise ConfigError(f"{where}.port must be a port number 0-{csp.MAX_PORT}")
    codec = value["codec"]
    if not isinstance(codec, str) or not codec:
        raise ConfigError(f"{where}.codec must name a housekeeping codec")
    request = _request(value["request"], f"{where}.request") if "request" in value else b""
    options = {str(k): v for k, v in value.items() if k not in ("port", "codec", "request")}
    return Housekeeping(port, request, codec, options)


_RDP_TIMES = {
    "conn_timeout": "conn_timeout_ms",
    "packet_timeout": "packet_timeout_ms",
    "ack_timeout": "ack_timeout_ms",
}
_RDP_COUNTS = ("window_size", "ack_delay_count")


def rdp_layer_options(value: Any, where: str) -> dict[str, Any]:
    """RdpOptions fields from ``csp(rdp: {...})``: times with units
    (``ack_timeout: 100 ms``), counts, and ``delayed_acks``."""
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: rdp is a map of RDP options")
    known = [*_RDP_COUNTS, *_RDP_TIMES, "delayed_acks"]
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "delayed_acks":
            if not isinstance(item, bool):
                raise ConfigError(f"{where}: rdp delayed_acks is true or false")
            result[key] = item
        elif key in _RDP_TIMES:
            if not isinstance(item, (int, float)) or isinstance(item, bool) or item <= 0:
                raise ConfigError(f"{where}: rdp {key} is a positive time, such as 100 ms")
            result[_RDP_TIMES[key]] = round(item * 1000)
        elif key in _RDP_COUNTS:
            if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
                raise ConfigError(f"{where}: rdp {key} is a positive whole number")
            result[key] = item
        else:
            raise ConfigError(f"{where}: rdp has no option {key} (known: {', '.join(known)})")
    return result


def parse_layer(arguments: Mapping[str, Any], where: str) -> CspSettings:
    """The ``csp`` protocol layer's arguments: ``address``, ``housekeeping``
    and ``rdp`` (``protocol csp(address: 5, rdp: {ack_timeout: 100 ms})``)."""
    unknown = ", ".join(map(str, sorted(set(arguments) - {"address", "housekeeping", "rdp"})))
    if unknown:
        raise ConfigError(f"{where}: csp takes address, housekeeping and rdp, not {unknown}")
    address = arguments.get("address")
    if address is not None and (
        not isinstance(address, int) or isinstance(address, bool) or not 0 <= address <= 31
    ):
        raise ConfigError(f"{where}: csp address must be an integer 0-31, not {address!r}")
    housekeeping = None
    if arguments.get("housekeeping") is not None:
        housekeeping = _housekeeping(arguments["housekeeping"], f"{where}: csp housekeeping")
    rdp = arguments.get("rdp")
    options = rdp_layer_options(rdp, f"{where}: csp") if rdp is not None else {}
    return CspSettings(address, housekeeping, options)
