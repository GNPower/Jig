"""The example-node fixture's plugin: a single file that imports the modules
beside it relatively (``from . import status``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jig.plugin import (
    BoardHandle,
    HkContext,
    HkDecoder,
    MachineConfig,
    NodeHandle,
    PluginApi,
    Record,
)

from . import helpers, status

JIG_PLUGIN_API = 2


def status_codec(context: HkContext) -> HkDecoder:
    """Decodes status replies; ``reply`` counts the replies of one stream."""
    replies = 0

    def decode(payload: bytes) -> dict[str, object]:
        nonlocal replies
        replies += 1
        values = status.decode(payload)
        values["reply"] = replies
        values["label"] = context.options.get("label", "")
        return values

    return decode


def origin(board: BoardHandle) -> str:
    return helpers.NAME


def where(board: BoardHandle) -> Record:
    return Record(root=board.root.name, workdir=board.workdir.name, board=board.definition.name)


async def status_of(node: NodeHandle, target: Any, timeout: float = 2.0) -> Record:
    """The status reply of ``target`` (a node member, dispatched on its target)."""
    raw = await node.request(target, 10, b"\x01", timeout)
    return Record(status.decode(raw))


def beat(node: NodeHandle, target: Any) -> None:
    """One heartbeat: a packet without a connection to port 20."""
    node.send(target, 20, b"\x82", sport=20, priority=2)


def describe(node: NodeHandle, target: Any) -> str:
    """Which plugin answered (a node member both fixture plugins define)."""
    return f"{helpers.NAME} describes {getattr(target, 'name', target)}"


def images(config: MachineConfig) -> dict[str, Path]:
    return {"app": config.board.firmware_file("app")}


def register(api: PluginApi) -> None:
    api.hk_codec("example-status", status_codec)
    api.member("board", "origin", origin)
    api.member("board", "where", where, attribute=True)
    api.member("node", "status_of", status_of)
    api.member("node", "describe", describe)
    api.background("node", "heartbeat", beat, every=1.0)
    api.function("status_size", lambda: status.LAYOUT.size)
    api.zynq_hooks(images=images)
