"""The example-package fixture's plugin: a package whose modules import each
other relatively, including a subpackage."""

from __future__ import annotations

from typing import Any

from jig.plugin import BoardHandle, NodeHandle, PluginApi

from . import helpers
from .codec import uptime_codec
from .services import uptime


def origin(board: BoardHandle) -> str:
    return helpers.NAME


def describe(node: NodeHandle, target: Any) -> str:
    return f"{helpers.NAME} describes {getattr(target, 'name', target)}"


def register(api: PluginApi) -> None:
    api.hk_codec("example-uptime", uptime_codec)
    api.member("board", "origin", origin)
    api.member("node", "status_of", uptime.status_of)
    api.member("node", "describe", describe)
    api.function("package_name", lambda: helpers.NAME)
