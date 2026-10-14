"""UART transport: bytes through a Renode ``UartPort``.

A ``UartPort`` (the C# model ``Jig.Harness.UartPort``) sits on the far end of a
modelled UART inside Renode. After every simulation step the harness collects
what the UART transmitted, each byte stamped with its simulated time, and hands
it to the port's listeners. Bytes the harness writes queue on the UART's
receive line and the UART model takes them at its baud rate in simulated time.
Nothing crosses a socket, so there is no race between the host and the
emulation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from jig.errors import RenodeError
from jig.renode.session import RenodeSession

log = logging.getLogger(__name__)

_MAX_WRITE_CHUNK = 4096  # bytes per monitor command
_WRAPPED_DELTA = 1 << 63  # a gap this large is a negative one in unsigned form

Listener = Callable[[bytes, list[int]], None]


def parse_collected(line: str) -> tuple[str, bytes, list[int]]:
    """Parse a ``U <name> <stamps> <hex>`` line into (name, data, stamps in µs)."""
    try:
        _kind, name, stamps_text, hex_data = line.split(" ")
        data = bytes.fromhex(hex_data)
        stamps: list[int] = []
        previous = 0
        for index, item in enumerate(stamps_text.split(",")):
            value = int(item)
            if index > 0 and value >= _WRAPPED_DELTA:
                value = 0  # an unsigned gap that went negative: hold the time
            previous = value if index == 0 else previous + value
            stamps.append(previous)
    except ValueError as exc:
        raise RenodeError(f"malformed UART collection line {line[:80]!r}") from exc
    if len(stamps) != len(data):
        raise RenodeError(
            f"UART collection line for {name}: {len(data)} bytes, {len(stamps)} stamps"
        )
    return name, data, stamps


class UartPort:
    """One UART's byte stream, seen from outside the board."""

    def __init__(self, session: RenodeSession, port: str, label: str) -> None:
        self._session = session
        self.port = port  # the Renode external's name
        self.label = label
        self.received = bytearray()  # everything the board has sent, ever
        self._listeners: list[Listener] = []

    @classmethod
    def attach(cls, session: RenodeSession, machine: str, peripheral: str, label: str) -> UartPort:
        port = f"{machine}_{peripheral}".replace(".", "_")
        session.execute(f'emulation CreateUartPort "{port}"')
        session.select_machine(machine)
        session.execute(f"connector Connect sysbus.{peripheral} {port}")
        return cls(session, port, label)

    def subscribe(self, listener: Listener) -> None:
        """Call ``listener(data, stamps)`` with every chunk the board sends from now on."""
        self._listeners.append(listener)

    def feed(self, data: bytes, stamps: list[int]) -> None:
        """Deliver bytes the harness collected after a step."""
        self.received.extend(data)
        for listener in self._listeners:
            listener(data, stamps)

    def write(self, data: bytes) -> None:
        """Queue bytes for the board to receive (paced by the UART's baud rate)."""
        for start in range(0, len(data), _MAX_WRITE_CHUNK):
            chunk = data[start : start + _MAX_WRITE_CHUNK]
            self._session.execute(f'{self.port} Write "{chunk.hex()}"')
