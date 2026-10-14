"""Client of Jig's loopback bridge into Renode (``models/Harness/Bridge.cs``).

Renode compiles the bridge with Jig's other models when it starts, and the
start-up script runs ``emulation StartJigBridge``. The bridge listens on
127.0.0.1 on a port the operating system picks and writes ``bridge.json`` to
the run directory: the port, a per-run random token, Renode's PID and the
protocol version. ``BridgeClient`` connects to it and runs monitor commands
and IronPython over one TCP connection, one request at a time.

A request or a reply is a frame: a 4-byte big-endian length, then a UTF-8
JSON object. Every request carries the token. Renode exits when this
connection closes, so a session cannot leave an emulation running behind it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import struct
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jig.errors import RenodeCommandError, RenodeError

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
"""The bridge protocol this package speaks; ``JigBridge.ProtocolVersion`` in Bridge.cs."""

INFO_FILE = "bridge.json"
ERROR_FILE = "bridge.error"
LOOPBACK = "127.0.0.1"

_HEADER = struct.Struct(">I")
_MAX_REPLY = 256 << 20


@dataclass(frozen=True)
class BridgeInfo:
    """What the bridge wrote to ``bridge.json``."""

    protocol: int
    host: str
    port: int
    token: str
    pid: int
    startup_compile_ms: int = 0

    @classmethod
    def load(cls, path: Path) -> BridgeInfo | None:
        """The info file, or ``None`` while it does not exist yet. The bridge
        writes it to a temporary name and renames it, so a file that exists
        is complete."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:  # Windows: the rename can be in progress
            log.debug("reading %s: %s", path, exc)
            return None
        try:
            data = json.loads(text)
            info = cls(
                protocol=int(data["protocol"]),
                host=str(data["host"]),
                port=int(data["port"]),
                token=str(data["token"]),
                pid=int(data["pid"]),
                startup_compile_ms=int(data.get("startup_compile_ms", 0)),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise RenodeError(f"{path}: not a bridge info file ({exc})") from None
        if info.host != LOOPBACK or not 0 < info.port < 65536:
            raise RenodeError(f"{path}: the bridge must listen on {LOOPBACK}, not {info.host}")
        return info


class BridgeError(RenodeError):
    """The bridge answered a request with an error."""

    def __init__(self, op: str, kind: str, message: str) -> None:
        self.op = op
        self.kind = kind
        self.message = message.strip()
        super().__init__(f"bridge {op} ({kind}): {self.message}")


class BridgeClient:
    """One connection to a Renode's bridge. Calls are serialised by a lock."""

    def __init__(self, info: BridgeInfo, *, timeout: float | None = None) -> None:
        self.info = info
        self.timeout = timeout
        self.address = f"{LOOPBACK}:{info.port}"
        self.hello: dict[str, Any] = {}
        self._socket: socket.socket | None = None
        self._next_id = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ connection

    def connect(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """Connect, greet the bridge and check its protocol version."""
        try:
            sock = socket.create_connection((LOOPBACK, self.info.port), timeout=timeout)
        except OSError as exc:
            raise RenodeError(f"cannot reach the Renode bridge at {self.address}: {exc}") from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self.timeout)
        self._socket = sock
        reply = self.request("hello", protocol=PROTOCOL_VERSION)
        if not isinstance(reply, dict):
            self.close()
            raise RenodeError(f"the Renode bridge answered hello with {reply!r}")
        protocol = reply.get("protocol")
        if protocol != PROTOCOL_VERSION:
            self.close()
            raise RenodeError(
                f"the Renode bridge speaks protocol {protocol}; this Jig speaks "
                f"{PROTOCOL_VERSION}: its models and its Python package do not match"
            )
        self.hello = reply
        return reply

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    # -------------------------------------------------------------- requests

    def request(self, op: str, **fields: Any) -> Any:
        """Send one request and return its ``result``; raise ``BridgeError``
        when the bridge reports a failure and ``RenodeError`` when the
        connection is lost."""
        with self._lock:
            sock = self._socket
            if sock is None:
                raise RenodeError(f"not connected to the Renode bridge at {self.address}")
            self._next_id += 1
            ident = self._next_id
            body = json.dumps(
                {"id": ident, "op": op, "token": self.info.token, **fields}, ensure_ascii=False
            ).encode("utf-8")
            try:
                sock.sendall(_HEADER.pack(len(body)) + body)
                reply = self._receive(sock)
            except (OSError, EOFError) as exc:
                self.close()
                raise RenodeError(
                    f"lost contact with Renode at {self.address} during {op}: {exc}"
                ) from exc
        if not isinstance(reply, dict) or reply.get("id") != ident:
            raise RenodeError(f"the Renode bridge sent an unexpected reply to {op}: {reply!r}")
        if not reply.get("ok"):
            raise BridgeError(op, str(reply.get("kind", "")), str(reply.get("error", "")))
        return reply.get("result")

    @staticmethod
    def _receive(sock: socket.socket) -> Any:
        (length,) = _HEADER.unpack(_receive_exactly(sock, _HEADER.size))
        if length > _MAX_REPLY:
            raise EOFError(f"a reply of {length} bytes exceeds the limit")
        return json.loads(_receive_exactly(sock, length).decode("utf-8"))

    def execute(self, command: str, machine: str | None = None) -> str:
        """Run a monitor command (as the Robot keyword ExecuteCommand) and return its output."""
        log.debug("monitor> %s", command)
        try:
            output = self.request("command", command=command, machine=machine)
        except BridgeError as exc:
            raise RenodeCommandError("ExecuteCommand", (command,), exc.message) from None
        text = "" if output is None else str(output)
        if text.strip():
            log.debug("monitor< %s", text.rstrip())
        return text

    def python(self, code: str, machine: str | None = None) -> Any:
        """Run IronPython in the monitor's scope and return its value (as JSON:
        numbers, strings, booleans, lists and dictionaries; other values as text)."""
        try:
            return self.request("python", code=code, machine=machine)
        except BridgeError as exc:
            raise RenodeCommandError("ExecutePython", (code,), exc.message) from None

    def compile(self, files: Sequence[Path]) -> float:
        """Compile C# files in one include; return the seconds Renode took.
        Compiler errors raise ``BridgeError`` (kind ``compile``)."""
        reply = self.request("compile", files=[str(path) for path in files])
        seconds = reply.get("seconds") if isinstance(reply, dict) else None
        return float(seconds) if isinstance(seconds, int | float) else 0.0

    def quit(self) -> None:
        """Ask Renode to exit; it does once the reply is sent."""
        self.request("quit")


def _receive_exactly(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(min(remaining, 1 << 20))
        if not chunk:
            raise EOFError("Renode closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
