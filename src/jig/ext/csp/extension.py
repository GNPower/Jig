"""Registers the CSP extension, as a board plugin would register its own
members: the ``csp`` board.yaml section, the ``csp(...)`` node kind, the bus
observer behind ``bus.packets``, ``bus.traffic`` and ``bus.hk`` (and the
``<bus>.packets.csv`` file), a board's ``address`` and ``crc32c``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jig.ext.csp import dsl, settings
from jig.ext.csp import protocol as csp
from jig.lang.library import as_bytes

if TYPE_CHECKING:
    from jig.plugin import ExtensionApi


def crc32c(data: Any) -> int:
    """CRC-32C (Castagnoli) of bytes or text, as libcsp computes it."""
    return csp.crc32c(as_bytes(data, "data"))


def register(api: ExtensionApi) -> None:
    api.board_section(settings.SECTION, settings.parse)
    api.node_kind("csp", dsl.csp_node)
    api.bus_observer(dsl.OBSERVER, dsl.observe)
    api.member("board", "address", dsl.csp_address, attribute=True)
    api.member("bus", "packets", dsl.bus_packets)
    api.member("bus", "traffic", dsl.bus_traffic)
    api.member("bus", "hk", dsl.bus_hk)
    api.function("crc32c", crc32c)
