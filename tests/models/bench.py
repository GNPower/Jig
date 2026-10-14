"""Benches for the register-level model tests: a machine in a Renode session,
driven through monitor commands with no firmware.

Each test module shares one Renode session (``conftest.renode``); each test
gets its own machine (``Bench``), built from a platform-description snippet
and removed afterwards. Monitor replies are parsed here: numbers print in hex,
booleans as True or False.
"""

from __future__ import annotations

import contextlib
import importlib
import itertools
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jig.errors import JigError
from jig.renode.session import RenodeSession

FIXTURE_MODELS = Path(__file__).resolve().parents[1] / "fixtures" / "models"

# Wall-clock limit for one simulation step: a hung step fails the test instead
# of the run.
STEP_WALL_LIMIT = 120.0

_HEX_BYTE = re.compile(r"0x([0-9A-Fa-f]{2})\b")


def open_session(workdir: Path) -> RenodeSession:
    """A session (not started) on the Renode the environment names.

    Shim for the Renode-layer rework (J1b-beta): a ``jig.renode.discover``
    module means sessions take ``install=find_renode(...)``; before it lands
    they take ``executable=`` from ``jig.renode.toolchain``."""
    kwargs: dict[str, Any] = {"workdir": workdir, "step_wall_limit": STEP_WALL_LIMIT}
    try:
        discover = importlib.import_module("jig.renode.discover")
    except ModuleNotFoundError:
        toolchain = importlib.import_module("jig.renode.toolchain")
        kwargs["executable"] = toolchain.renode_executable()
    else:
        kwargs["install"] = discover.find_renode(explicit=None, config=None)
    return RenodeSession(**kwargs)


def monitor_path(path: Path) -> str:
    """A path as a monitor path token (``@`` and the path, spaces escaped)."""
    return "@" + path.as_posix().replace(" ", "\\ ")


def include(session: RenodeSession, paths: Iterable[Path]) -> None:
    """Compile C# files together into the session (the monitor's ``include``)."""
    session.execute("include " + " ".join(f'"{path.as_posix()}"' for path in paths))


def fixture_models() -> list[Path]:
    """The C# fixture models under ``tests/fixtures/models``."""
    return sorted(FIXTURE_MODELS.glob("*.cs"))


def first_line(reply: str) -> str:
    for line in reply.splitlines():
        if line.strip():
            return line.strip()
    return ""


def parse_bytes(reply: str) -> bytes:
    """The bytes in a ``sysbus ReadBytes`` reply ("[0x00, 0x01, ...]")."""
    return bytes(int(value, 16) for value in _HEX_BYTE.findall(reply))


class Bench:
    """One machine in a Renode session. Every command runs with this machine
    selected; ``remove`` takes it out of the emulation."""

    _numbers = itertools.count(1)

    def __init__(self, session: RenodeSession, workdir: Path) -> None:
        self.session = session
        self.workdir = workdir
        self.name = f"bench{next(Bench._numbers)}"
        workdir.mkdir(parents=True, exist_ok=True)
        session.create_machine(self.name)

    # ------------------------------------------------------------- building

    def load(self, repl: str) -> None:
        """Adds a platform-description snippet to the machine."""
        path = self.workdir / f"{self.name}-{next(Bench._numbers)}.repl"
        path.write_text(repl, encoding="utf-8", newline="\n")
        self.execute(f"machine LoadPlatformDescription {monitor_path(path)}")

    def remove(self) -> None:
        self.session.execute(f'mach rem "{self.name}"')

    # ------------------------------------------------------------- monitor

    def execute(self, command: str) -> str:
        self.session.select_machine(self.name)
        return self.session.execute(command)

    def text(self, command: str) -> str:
        """The first line a command prints."""
        return first_line(self.execute(command))

    def number(self, command: str) -> int:
        """A command's integer reply."""
        reply = self.text(command)
        try:
            return int(reply, 0)
        except ValueError:
            raise AssertionError(f"{command!r} printed {reply!r}, not a number") from None

    def flag(self, command: str) -> bool:
        """A command's boolean reply."""
        reply = self.text(command)
        if reply not in ("True", "False"):
            raise AssertionError(f"{command!r} printed {reply!r}, not True or False")
        return reply == "True"

    # ----------------------------------------------------------- system bus

    def read(self, address: int, *, cpu: str | None = None) -> int:
        return self.number(f"sysbus ReadDoubleWord {address:#x}{_context(cpu)}")

    def write(self, address: int, value: int, *, cpu: str | None = None) -> None:
        self.execute(f"sysbus WriteDoubleWord {address:#x} {value:#x}{_context(cpu)}")

    def read_word(self, address: int, *, cpu: str | None = None) -> int:
        return self.number(f"sysbus ReadWord {address:#x}{_context(cpu)}")

    def read_byte(self, address: int, *, cpu: str | None = None) -> int:
        return self.number(f"sysbus ReadByte {address:#x}{_context(cpu)}")

    def write_byte(self, address: int, value: int, *, cpu: str | None = None) -> None:
        self.execute(f"sysbus WriteByte {address:#x} {value:#x}{_context(cpu)}")

    def read_bytes(self, address: int, count: int) -> bytes:
        data = parse_bytes(self.execute(f"sysbus ReadBytes {address:#x} {count}"))
        if len(data) != count:
            raise AssertionError(f"ReadBytes {address:#x} {count} gave {len(data)} bytes")
        return data

    def write_bytes(self, address: int, data: bytes) -> None:
        """Loads ``data`` at ``address`` through a file (``sysbus LoadBinary``)."""
        path = self.workdir / f"{self.name}-{next(Bench._numbers)}.bin"
        path.write_bytes(data)
        self.execute(f"sysbus LoadBinary {monitor_path(path)} {address:#x}")

    # ----------------------------------------------------------------- time

    def run_ns(self, nanoseconds: int) -> None:
        self.session.run_for_ns(nanoseconds)

    def run_us(self, microseconds: float) -> None:
        self.session.run_for_ns(round(microseconds * 1000))

    def run_ms(self, milliseconds: float) -> None:
        self.session.run_for_ns(round(milliseconds * 1_000_000))

    # ------------------------------------------------------------- harness

    def uart_port(self, peripheral: str) -> str:
        """A harness UART port on ``peripheral`` (``sysbus.<peripheral>``); returns its name."""
        name = f"{self.name}_{peripheral}"
        self.session.execute(f'emulation CreateUartPort "{name}"')
        self.execute(f"connector Connect sysbus.{peripheral} {name}")
        return name

    def can_bus(self, *peripherals: str, bitrate: int = 1_000_000) -> str:
        """A Jig CanBus joining ``peripherals``; returns its name."""
        name = f"{self.name}_can"
        self.session.execute(f'emulation CreateCanBus "{name}" {bitrate}')
        for peripheral in peripherals:
            self.execute(f"connector Connect sysbus.{peripheral} {name}")
        return name


def _context(cpu: str | None) -> str:
    return f" {cpu}" if cpu else ""


def remove_quietly(bench: Bench) -> None:
    """Removes the bench's machine; a session that already died is left alone."""
    with contextlib.suppress(JigError):
        bench.remove()
