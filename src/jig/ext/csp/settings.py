"""The ``csp`` section of a board.yaml, and the housekeeping codec contract::

    csp:
      address: 5                 # the board's CSP address, 0-31
      housekeeping:              # what node.hk(board) and bus.hk(board) use
        port: 10                 # the board's housekeeping port, 0-63
        request: "01"            # the request payload: hex digits, a byte or a list of bytes
        codec: example-status    # a codec a plugin registered (api.hk_codec)
        layout: status.yaml      # any other key goes to the codec as an option
      rdp:                       # RDP options for connections to this board
        window_size: 4
        conn_timeout_ms: 10000
        packet_timeout_ms: 1000
        delayed_acks: true
        ack_timeout_ms: 250
        ack_delay_count: 2

RDP options for a connection come from libcsp 1.6's defaults, then the
target board's ``csp.rdp``, then what the plan's ``csp(...)`` declaration
sets.

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
from jig.ext.csp.rdp import RdpOptions
from jig.system.boards import SectionContext

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
    rdp: Mapping[str, Any] = field(default_factory=dict)  # RdpOptions fields the board sets

    def problems(self) -> list[str]:
        """What ``jig check`` reports: a codec no loaded plugin registered."""
        from jig.system import registry

        if self.housekeeping is None:
            return []
        try:
            registry.default().codec(self.housekeeping.codec)
        except ConfigError as exc:
            return [f"csp.housekeeping: {exc}"]
        return []


@dataclass(frozen=True)
class HkContext:
    """What a housekeeping codec learns about the stream it decodes."""

    board: str  # the board definition's name
    directory: Path  # the directory holding its board.yaml
    root: Path  # the board's root
    port: int  # the housekeeping port
    options: Mapping[str, Any]  # the other keys of csp.housekeeping


HkDecoder = Callable[[bytes], Mapping[str, Any]]
HkCodec = Callable[[HkContext], HkDecoder]


def _request(value: Any, where: str) -> bytes:
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
            f"{where}: a table {{port, request, codec}} since board.yaml schema 2, "
            "not a file name (a codec option can name the file)"
        )
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a table {{port, request, codec}}")
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


def rdp_overrides(value: Any, where: str) -> dict[str, Any]:
    """RdpOptions fields from a table, checked."""
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a table of RDP options")
    known = RdpOptions.field_names()
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key not in known:
            raise ConfigError(f"{where}: unknown option {key} (known: {', '.join(known)})")
        if key == "delayed_acks":
            if not isinstance(item, bool):
                raise ConfigError(f"{where}.delayed_acks must be true or false")
        elif not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            raise ConfigError(f"{where}.{key} must be a positive integer")
        result[str(key)] = item
    return result


def parse(value: Any, context: SectionContext) -> CspSettings:
    """The board.yaml section parser the CSP extension registers."""
    where = context.where
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping")
    unknown = ", ".join(map(str, sorted(set(value) - {"address", "housekeeping", "rdp"})))
    if unknown:
        raise ConfigError(f"{where}: unknown key {unknown} (known: address, housekeeping, rdp)")
    address = value.get("address")
    if address is not None and (
        not isinstance(address, int) or isinstance(address, bool) or not 0 <= address <= 31
    ):
        raise ConfigError(f"{where}.address must be an integer 0-31")
    housekeeping = None
    if value.get("housekeeping") is not None:
        housekeeping = _housekeeping(value["housekeeping"], f"{where}.housekeeping")
    rdp = rdp_overrides(value["rdp"], f"{where}.rdp") if value.get("rdp") is not None else {}
    return CspSettings(address, housekeeping, rdp)
