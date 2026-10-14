"""The plugin API, version 1: everything a board plugin may use.

A board names its plugin in its board.yaml: ``plugin: plugin.py`` (a file) or
``plugin: myboard`` (a package directory with ``__init__.py``), relative to
the board.yaml. Jig imports it the first time a simulation uses the board,
under a module name of its own (a file plugin's sibling modules and a package
plugin's submodules import relatively, ``from . import codec``; nothing joins
``sys.path``), and calls its ``register(api)`` once per board definition::

    from jig.plugin import HkContext, PluginApi, Record

    def status(context: HkContext):
        def decode(payload: bytes) -> dict[str, object]:
            return {"uptime_ms": int.from_bytes(payload[8:12], "big")}
        return decode

    async def banner(board, within=5.0):
        found = await board.expect("ready", within)
        return found.text

    def register(api: PluginApi) -> None:
        api.hk_codec("example-status", status)
        api.member("board", "banner", banner)       # dut.banner() in a plan

What a plugin registers:

``api.function(name, fn)``
    A plan function. Two boards in one system whose plugins define one name
    are an error naming both; prefer a board member for anything board-specific.
``api.member(handle_type, name, fn, *, attribute=False)``
    A member of plan handles: ``"board"``, ``"node"`` or ``"bus"``. ``fn``
    takes the handle first. A board member exists only on boards of this
    plugin's board (``dut.board_file(path)``), so two board types never
    collide; a node or bus member goes to the plugin of the board its first
    argument names (``gs.upload(dut, data)``), or of the one board in the
    system that registered it. With ``attribute=True`` a plan reads the
    member like a field and Jig calls ``fn(handle)``.
``api.hk_codec(name, codec)``
    A housekeeping codec a board.yaml names in ``csp.housekeeping.codec``:
    ``codec(context: HkContext)`` returns a decoder, called with each reply's
    payload in order, returning the reply's fields. ``node.hk(board)``,
    ``node.hk_published(board)`` and ``bus.hk(board)`` dispatch through the
    target board's codec and port.
``api.node_kind(name, constructor)``
    A ``node`` kind for system declarations: ``constructor(...)`` returns a
    ``NodeDeclaration``.
``api.bus_observer(name, factory)``
    ``factory(context: BusContext)`` runs for every CAN bus of a simulation
    that includes the board; plugin members reach the result through
    ``bus.observer(name)``.
``api.soc_driver(name, factory)``
    The SoC driver for ``soc: name`` on this plugin's board.
``api.zynq_hooks(images=, boot_table=, qspi_image=)``
    How the Zynq-7000 driver gets this board's firmware images, boot metadata
    and Quad-SPI flash image (each a function of a ``MachineConfig``).
``api.background(handle_type, name, action, *, every=None)``
    A member that starts work beside the plan: ``gs.heartbeat(dut)`` calls
    ``action(handle, ...)`` every ``every`` seconds (or once, as a coroutine,
    without ``every``) until the plan stops it or the block that started it
    ends, and returns a background handle (``.stop()``, ``.running``).

The ``api`` also tells the plugin which board it serves: ``api.board`` (the
parsed board.yaml), ``api.name``, ``api.root`` and ``api.directory``. A board
handle gives plugin code ``definition``, ``config``, ``root``, ``workdir``,
``console`` and ``emit(kind, detail)``.

Nothing outside this module is public to plugins: every other module of the
``jig`` package is internal and may change in any release. This module
follows semantic versioning through ``API_VERSION``; a plugin may declare the
version it was written for as ``JIG_PLUGIN_API = 1``, and Jig refuses to load
it under another. Board repositories pin ``jig-dsl>=0.1,<0.2``.
"""

from __future__ import annotations

import contextlib
import inspect
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jig.artifacts import cached
from jig.errors import ConfigError, JigError, ToolchainError
from jig.ext.csp import protocol as _csp
from jig.ext.csp.dsl import ConnectionHandle, NodeHandle
from jig.ext.csp.node import Connection, CspError, CspNode, Received
from jig.ext.csp.protocol import CspId, Packet, crc32c
from jig.ext.csp.rdp import RdpOptions
from jig.ext.csp.settings import HkCodec, HkContext, HkDecoder
from jig.ext.zynq7000.driver import BootTableHook, ImagesHook, QspiImageHook, ZynqHooks
from jig.lang.library import (
    BackgroundHandle,
    BoardHandle,
    BusHandle,
    ConsoleHandle,
    DslError,
    InterfaceRef,
    Match,
    NodeDeclaration,
    SystemContext,
    periodic,
)
from jig.lang.parser import KEYWORDS
from jig.lang.values import Record, Regex
from jig.renode.session import RenodeSession
from jig.runtime.environment import Background, Profile
from jig.system.boards import Board as BoardDefinition
from jig.system.boards import SectionParser
from jig.system.registry import HANDLE_TYPES, Member, Owner, Staged
from jig.system.socs import MachineConfig, SocDriver
from jig.system.system import BusContext
from jig.transports.can import CanBusPort, CanFrame
from jig.transports.uart import UartPort

API_VERSION = 1

PRIORITY_CRITICAL = _csp.PRIORITY_CRITICAL
PRIORITY_HIGH = _csp.PRIORITY_HIGH
PRIORITY_NORMAL = _csp.PRIORITY_NORMAL
PRIORITY_LOW = _csp.PRIORITY_LOW
BROADCAST = _csp.BROADCAST

__all__ = [
    "API_VERSION",
    "BROADCAST",
    "PRIORITY_CRITICAL",
    "PRIORITY_HIGH",
    "PRIORITY_LOW",
    "PRIORITY_NORMAL",
    "BackgroundHandle",
    "BoardDefinition",
    "BoardHandle",
    "BootTableHook",
    "BusContext",
    "BusHandle",
    "CanBusPort",
    "CanFrame",
    "ConfigError",
    "Connection",
    "ConnectionHandle",
    "ConsoleHandle",
    "CspError",
    "CspId",
    "CspNode",
    "DslError",
    "HkCodec",
    "HkContext",
    "HkDecoder",
    "ImagesHook",
    "InterfaceRef",
    "JigError",
    "MachineConfig",
    "Match",
    "NodeDeclaration",
    "NodeHandle",
    "Packet",
    "PluginApi",
    "Profile",
    "QspiImageHook",
    "RdpOptions",
    "Received",
    "Record",
    "Regex",
    "RenodeSession",
    "SocDriver",
    "SystemContext",
    "ToolchainError",
    "UartPort",
    "ZynqHooks",
    "cached",
    "crc32c",
]

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_FAMILY = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_BUILT_IN = {
    "board": BoardHandle.dsl_members,
    "node": NodeHandle.dsl_members,
    "bus": BusHandle.dsl_members,
}


class PluginApi:
    """What a board plugin's ``register(api)`` receives (see the module
    documentation for each method)."""

    version = API_VERSION

    def __init__(self, owner: Owner, board: BoardDefinition | None = None) -> None:
        self._owner = owner
        self._board = board
        self.staged = Staged(owner)

    def __repr__(self) -> str:
        return f"<plugin api v{API_VERSION} for {self._owner}>"

    # ------------------------------------------------------- the board served

    @property
    def board(self) -> BoardDefinition:
        """The board definition (board.yaml) the plugin is registering for."""
        if self._board is None:
            raise ConfigError(f"{self._owner} serves no single board")
        return self._board

    @property
    def name(self) -> str:
        return self.board.name

    @property
    def root(self) -> Path:
        """The board's root (board.yaml ``root``): where its repository paths start."""
        return self.board.root

    @property
    def directory(self) -> Path:
        """The directory holding the board's board.yaml."""
        return self.board.directory

    # ------------------------------------------------------------ registering

    def _fail(self, message: str) -> ConfigError:
        return ConfigError(f"{self._owner}: {message}")

    def _stage(self, table: dict[Any, Any], key: Any, value: Any, what: str) -> None:
        earlier = table.get(key)
        if earlier is not None and earlier is not value:
            raise self._fail(f"registers {what} twice")
        table[key] = value

    def _identifier(self, name: str, what: str) -> str:
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name) or name in KEYWORDS:
            raise self._fail(f"{what} {name!r} is not a name a plan can use")
        return name

    @staticmethod
    def _callable(value: Any, what: str, owner: Owner) -> None:
        if not callable(value):
            raise ConfigError(f"{owner}: {what} must be callable, not {value!r}")

    def function(self, name: str, fn: Callable[..., Any]) -> None:
        """A plan function ``name(...)``."""
        self._identifier(name, "function")
        self._callable(fn, f"function {name}", self._owner)
        self._stage(self.staged.functions, name, fn, f"function {name!r}")

    def member(
        self, handle_type: str, name: str, fn: Callable[..., Any], *, attribute: bool = False
    ) -> None:
        """A member of board, node or bus handles; ``fn`` takes the handle first."""
        if handle_type not in HANDLE_TYPES:
            raise self._fail(f"handle type {handle_type!r} is not one of {', '.join(HANDLE_TYPES)}")
        self._identifier(name, "member")
        if name in _BUILT_IN[handle_type]:
            raise self._fail(f"{handle_type} member {name!r} is built in")
        self._callable(fn, f"member {name}", self._owner)
        self._stage(
            self.staged.members,
            (handle_type, name),
            Member(fn, attribute),
            f"{handle_type} member {name!r}",
        )

    def hk_codec(self, name: str, codec: HkCodec) -> None:
        """A housekeeping codec (``csp.housekeeping.codec: name``)."""
        if not isinstance(name, str) or not _FAMILY.fullmatch(name):
            raise self._fail(f"codec name {name!r} is not lower case letters, digits, '.-_'")
        self._callable(codec, f"codec {name}", self._owner)
        self._stage(self.staged.codecs, name, codec, f"codec {name!r}")

    def node_kind(self, name: str, constructor: Callable[..., NodeDeclaration]) -> None:
        """A node kind: ``node gs: name(...)`` in a system declaration."""
        self._identifier(name, "node kind")
        self._callable(constructor, f"node kind {name}", self._owner)
        self._stage(self.staged.node_kinds, name, constructor, f"node kind {name!r}")

    def bus_observer(self, name: str, factory: Callable[[BusContext], Any]) -> None:
        """An observer made for every CAN bus (``bus.observer(name)``)."""
        self._identifier(name, "bus observer")
        self._callable(factory, f"bus observer {name}", self._owner)
        self._stage(self.staged.bus_observers, name, factory, f"bus observer {name!r}")

    def soc_driver(self, name: str, factory: Callable[[], SocDriver]) -> None:
        """The driver class for ``soc: name`` on this plugin's board."""
        if not isinstance(name, str) or not _FAMILY.fullmatch(name):
            raise self._fail(f"SoC family {name!r} is not lower case letters, digits, '.-_'")
        self._callable(factory, f"SoC driver {name}", self._owner)
        self._stage(self.staged.soc_drivers, name, factory, f"SoC driver {name!r}")

    def zynq_hooks(
        self,
        *,
        images: ImagesHook | None = None,
        boot_table: BootTableHook | None = None,
        qspi_image: QspiImageHook | None = None,
    ) -> None:
        """The board's Zynq-7000 image hooks; each takes the ``MachineConfig``."""
        hooks = {"images": images, "boot_table": boot_table, "qspi_image": qspi_image}
        if all(hook is None for hook in hooks.values()):
            raise self._fail("zynq_hooks needs at least one of images, boot_table, qspi_image")
        for key, hook in hooks.items():
            if hook is not None:
                self._callable(hook, f"zynq_hooks {key}", self._owner)
        self._stage(
            self.staged.hooks,
            "zynq7000",
            ZynqHooks(qspi_image=qspi_image, images=images, boot_table=boot_table),
            "zynq_hooks",
        )

    def background(
        self,
        handle_type: str,
        name: str,
        action: Callable[..., Any],
        *,
        every: float | None = None,
    ) -> None:
        """A member that starts ``action(handle, ...)`` beside the plan."""
        self._callable(action, f"background {name}", self._owner)
        if every is not None and (
            not isinstance(every, (int, float)) or isinstance(every, bool) or every <= 0
        ):
            raise self._fail(f"background {name}: every must be a positive number of seconds")

        def start(handle: Any, *args: Any, **kwargs: Any) -> BackgroundHandle:
            label = f"{getattr(handle, 'name', handle_type)}.{name}"
            if every is not None:
                body = periodic(handle.scheduler, lambda: action(handle, *args, **kwargs), every)
            else:

                async def body(entry: Background) -> None:
                    result = action(handle, *args, **kwargs)
                    if inspect.isawaitable(result):
                        await result

            started: BackgroundHandle = handle.start_background(label, body)
            return started

        with contextlib.suppress(TypeError, ValueError):  # a builtin without a signature
            start.__signature__ = inspect.signature(action)  # type: ignore[attr-defined]
        start.__name__ = name
        start.__qualname__ = getattr(action, "__qualname__", name)
        start.__module__ = getattr(action, "__module__", __name__)
        self.member(handle_type, name, start)


class ExtensionApi(PluginApi):
    """The API the extensions bundled with Jig register through: a plugin's,
    plus board.yaml sections. Board plugins never receive it."""

    def board_section(self, name: str, parse: SectionParser) -> None:
        """A top-level board.yaml key: ``parse(value, context)`` (the context
        names the file, the board's directory and root) returns what boards
        hold under ``board.section(name)``."""
        self._identifier(name, "section")
        self._callable(parse, f"section {name}", self._owner)
        self._stage(self.staged.sections, name, parse, f"section {name!r}")
