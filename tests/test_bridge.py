"""The bridge client against an in-process stand-in for Renode's bridge."""

from __future__ import annotations

import json
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from jig.errors import RenodeCommandError, RenodeError
from jig.renode.bridge import PROTOCOL_VERSION, BridgeClient, BridgeError, BridgeInfo

TOKEN = "ab" * 32

Handler = Callable[[dict[str, Any]], dict[str, Any] | None]


class FakeBridge:
    """Answers framed JSON requests with ``handler``; ``None`` closes the connection."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[dict[str, Any]] = []
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def info(self, protocol: int = PROTOCOL_VERSION) -> BridgeInfo:
        return BridgeInfo(protocol, "127.0.0.1", self.port, TOKEN, 1234)

    def _serve(self) -> None:
        connection, _ = self.server.accept()
        with connection:
            while True:
                header = connection.recv(4)
                if len(header) < 4:
                    return
                (length,) = struct.unpack(">I", header)
                body = b""
                while len(body) < length:
                    body += connection.recv(length - len(body))
                request = json.loads(body)
                self.requests.append(request)
                reply = self.handler(request)
                if reply is None:
                    return
                data = json.dumps({"id": request["id"], **reply}).encode()
                connection.sendall(struct.pack(">I", len(data)) + data)

    def close(self) -> None:
        self.server.close()


def _renode_like(request: dict[str, Any]) -> dict[str, Any] | None:
    op = request["op"]
    if op == "hello":
        return {"ok": True, "result": {"protocol": PROTOCOL_VERSION, "version": "Renode"}}
    if op == "command" and request["command"] == "version":
        return {"ok": True, "result": "Renode, version 1.17.0\r\n"}
    if op == "command":
        error = f"KeywordException: Could not execute command '{request['command']}': no\n"
        return {"ok": False, "kind": "command", "error": error}
    if op == "python":
        return {"ok": True, "result": [1, "two", None]}
    if op == "compile":
        return {"ok": False, "kind": "compile", "error": "X.cs(1,2): error CS1002: ; expected"}
    if op == "drop":
        return None
    return {"ok": True, "result": None}


@pytest.fixture
def fake() -> Iterator[FakeBridge]:
    bridge = FakeBridge(_renode_like)
    yield bridge
    bridge.close()


def test_requests_carry_the_token_and_replies_come_back(fake: FakeBridge) -> None:
    client = BridgeClient(fake.info())
    hello = client.connect()
    assert hello["version"] == "Renode"
    assert client.execute("version") == "Renode, version 1.17.0\r\n"
    assert client.python("[1, 'two', None]") == [1, "two", None]
    assert [r["op"] for r in fake.requests] == ["hello", "command", "python"]
    assert all(r["token"] == TOKEN for r in fake.requests)
    assert fake.requests[0]["protocol"] == PROTOCOL_VERSION
    assert [r["id"] for r in fake.requests] == [1, 2, 3]
    client.close()


def test_failures_read_as_they_did_through_the_robot_server(fake: FakeBridge) -> None:
    client = BridgeClient(fake.info())
    client.connect()
    with pytest.raises(RenodeCommandError) as caught:
        client.execute("bogus")
    assert caught.value.keyword == "ExecuteCommand"
    assert caught.value.message == "KeywordException: Could not execute command 'bogus': no"
    with pytest.raises(BridgeError) as compile_error:
        client.compile([Path("X.cs")])
    assert compile_error.value.kind == "compile"
    assert "CS1002" in compile_error.value.message
    assert fake.requests[-1]["files"] == ["X.cs"]
    client.close()


def test_a_lost_connection_is_reported(fake: FakeBridge) -> None:
    client = BridgeClient(fake.info())
    client.connect()
    lost = r"lost contact with Renode at 127\.0\.0\.1:\d+ during drop"
    with pytest.raises(RenodeError, match=lost):
        client.request("drop")
    assert not client.connected
    with pytest.raises(RenodeError, match="not connected"):
        client.execute("version")


def test_another_protocol_version_is_refused() -> None:
    def old(request: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "result": {"protocol": PROTOCOL_VERSION + 1}}

    bridge = FakeBridge(old)
    try:
        client = BridgeClient(bridge.info())
        with pytest.raises(RenodeError, match="speaks protocol 2; this Jig speaks 1"):
            client.connect()
    finally:
        bridge.close()


def test_the_info_file(tmp_path: Path) -> None:
    path = tmp_path / "bridge.json"
    assert BridgeInfo.load(path) is None
    info = {"protocol": 1, "host": "127.0.0.1", "port": 50123, "token": TOKEN, "pid": 7}
    path.write_text(json.dumps(info), encoding="utf-8")
    assert BridgeInfo.load(path) == BridgeInfo(1, "127.0.0.1", 50123, TOKEN, 7, 0)
    path.write_text(json.dumps({**info, "host": "0.0.0.0"}), encoding="utf-8")
    with pytest.raises(RenodeError, match=r"must listen on 127\.0\.0\.1"):
        BridgeInfo.load(path)
    path.write_text(json.dumps({"protocol": 1}), encoding="utf-8")
    with pytest.raises(RenodeError, match="not a bridge info file"):
        BridgeInfo.load(path)
