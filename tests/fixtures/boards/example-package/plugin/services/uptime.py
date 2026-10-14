"""A node member asking the uptime service (port 6)."""

from __future__ import annotations

from typing import Any

from jig.plugin import NodeHandle, Record

from ..codec import decode


async def status_of(node: NodeHandle, target: Any, timeout: float = 2.0) -> Record:
    raw = await node.request(target, 6, b"", timeout)
    return Record(decode(raw))
