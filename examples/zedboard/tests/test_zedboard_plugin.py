# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Graham Power
"""The ZedBoard example's board plugin and board definitions, without Renode.

Run with ``python -m pytest examples/zedboard/tests`` from the repository root.
"""

from __future__ import annotations

import asyncio
import importlib.util
import struct
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

from jig.plugin import DslError

EXAMPLE = Path(__file__).resolve().parents[1]
PLUGIN = EXAMPLE / "boards" / "zedboard.py"


def load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location("zedboard_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = load_plugin()


def status_payload(**fields: int) -> bytes:
    values = {
        "magic": 0x5A454453,
        "version": 1,
        "node": 2,
        "can_tec": 0,
        "can_rec": 0,
        "uptime_ms": 1500,
        "can_sent": 10,
        "can_received": 12,
        "kiss_sent": 0,
        "kiss_received": 0,
        "routed": 0,
        "crc_errors": 0,
    }
    values.update(fields)
    return struct.pack(">IBBBBIIIIIII", *values.values())


# ------------------------------------------------------------------ the codec


def test_a_status_record_decodes_by_field_name() -> None:
    status = plugin.decode_status(status_payload(routed=4, crc_errors=1))
    assert list(status) == list(plugin.STATUS_FIELDS)
    assert status.node == 2
    assert status.uptime_ms == 1500
    assert status.can_received == 12
    assert status.routed == 4
    assert status.crc_errors == 1


def test_the_codec_gives_every_stream_the_same_decoder() -> None:
    context = SimpleNamespace(board="zed-b", port=10, options={})
    assert plugin.status_codec(context) is plugin.decode_status


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (status_payload()[:35], "36 bytes, not 35"),
        (status_payload() + b"\0", "36 bytes, not 37"),
        (status_payload(magic=0x12345678), "magic 0x12345678"),
        (status_payload(version=2), "layout version 2"),
    ],
)
def test_a_record_that_is_not_a_status_record_fails(payload: bytes, message: str) -> None:
    with pytest.raises(DslError, match=message):
        plugin.decode_status(payload)


# -------------------------------------------------------- the plan function


def test_cfp_frames_follows_the_fragmentation_rule() -> None:
    # The firmware's own counts: a 200-byte ping took 26 frames each way and
    # the largest payload, 256 bytes, 33.
    sizes = (0, 1, 2, 3, 10, 11, 200, 256)
    assert [plugin.cfp_frames(n) for n in sizes] == [1, 1, 1, 2, 2, 3, 26, 33]


@pytest.mark.parametrize("size", [-1, 2.5, True, "8"])
def test_cfp_frames_takes_whole_byte_counts(size: Any) -> None:
    with pytest.raises(DslError, match="whole number of bytes"):
        plugin.cfp_frames(size)


# ------------------------------------------------------------ board members


class FakeBoard:
    """What the member uses of a board handle."""

    def __init__(self) -> None:
        self.name = "b"
        self.commands: list[str] = []
        self.console_output = "uptime\r\nuptime 2310 ms\r\nzed> "

    async def cli(self, command: str) -> str:
        self.commands.append(command)
        return self.console_output


def test_uptime_reads_the_console_in_seconds() -> None:
    board = FakeBoard()
    assert asyncio.run(plugin.uptime(board)) == 2.31
    assert board.commands == ["uptime"]


def test_uptime_without_an_answer_fails() -> None:
    board = FakeBoard()
    board.console_output = "unknown command 'uptime', type help\r\nzed> "
    with pytest.raises(DslError, match="no uptime"):
        asyncio.run(plugin.uptime(board))


# --------------------------------------------------------------- registering


class RecordingApi:
    """The part of jig.plugin.PluginApi the plugin calls."""

    def __init__(self) -> None:
        self.codecs: dict[str, Any] = {}
        self.functions: dict[str, Any] = {}
        self.members: dict[tuple[str, str], Any] = {}

    def hk_codec(self, name: str, codec: Any) -> None:
        self.codecs[name] = codec

    def function(self, name: str, fn: Any) -> None:
        self.functions[name] = fn

    def member(self, handle_type: str, name: str, fn: Any, *, attribute: bool = False) -> None:
        self.members[(handle_type, name)] = fn


def test_register_adds_the_codec_the_function_and_the_board_member() -> None:
    api = RecordingApi()
    plugin.register(api)
    assert api.codecs == {"zed-status": plugin.status_codec}
    assert api.functions == {"cfp_frames": plugin.cfp_frames}
    # Rerunning a board's boot flow after a power cycle is Jig's board.boot().
    assert api.members == {("board", "uptime"): plugin.uptime}
    assert plugin.JIG_PLUGIN_API == 1


# ------------------------------------------------------- board definitions


@pytest.mark.parametrize(("board", "address"), [("zed-a", 1), ("zed-b", 2)])
def test_each_board_names_the_plugin_its_codec_and_its_firmware(board: str, address: int) -> None:
    path = EXAMPLE / "boards" / board / "board.yaml"
    definition = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert definition["schema"] == 2
    assert definition["name"] == board
    assert (path.parent / definition["plugin"]).resolve() == PLUGIN
    assert (path.parent / definition["platform"]).resolve() == EXAMPLE / "zedboard.repl"
    assert (path.parent / definition["root"]).resolve() == EXAMPLE
    assert definition["firmware"]["app"] == f"firmware/build/zed-node-{address}.elf"
    assert definition["csp"] == {
        "address": address,
        "housekeeping": {"port": 10, "request": "01", "codec": "zed-status"},
    }
    console = definition["interfaces"]["console"]
    assert (console["peripheral"], console["eol"]) == ("uart1", "cr")
    assert ("kiss" in definition["interfaces"]) == (board == "zed-a")
