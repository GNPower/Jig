"""Protocol layers and the stacks they form: ``csp over kiss``.

A layer runs on what the layer below it provides and provides something to the
layer above it. The bottom layer of a stack runs on what an interface gives: a
UART gives a byte stream and a CAN controller gives CAN frames. ``over``
stacks layers top first, so ``csp over kiss`` is CSP packets in KISS frames on
a byte stream.

Jig knows five layers. ``cli`` and ``vt100`` are defined here; the CSP
extension registers ``kiss``, ``cfp`` and ``csp``, and plugins add more with
``api.protocol``:

=========  ===============================  =========================
Layer      Runs on                          Provides
=========  ===============================  =========================
``kiss``   a byte stream                    packets
``cfp``    CAN frames                       packets
``csp``    packets                          CSP services
``vt100``  a byte stream                    terminal text
``cli``    terminal text or a byte stream   commands and replies
=========  ===============================  =========================

A layer named alone takes its defaults: ``kiss`` means ``kiss()``.
``check`` follows a stack from its bottom up and names the first layer that
does not fit, with the layer that would join the two: "csp needs packets and
uart0 gives a byte stream: put kiss between them".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jig.errors import ConfigError

BYTE_STREAM = "byte stream"
FRAMES = "CAN frames"
PACKETS = "packets"
TERMINAL_TEXT = "terminal text"
COMMANDS = "commands and replies"
CSP_SERVICES = "CSP services"

# What a link of each kind carries to the bottom layer of a stack.
LINK_GIVES = {"uart": BYTE_STREAM, "can": FRAMES}

_ARTICLES = {BYTE_STREAM: "a byte stream"}

EOLS = {"\r": "\r", "\n": "\n", "\r\n": "\r\n", "cr": "\r", "lf": "\n", "crlf": "\r\n"}
DEFAULT_EOL = "\r"


def phrase(data: str) -> str:
    """How a kind of data reads in a sentence: "a byte stream", "packets"."""
    return _ARTICLES.get(data, data)


ArgumentParser = Callable[[Mapping[str, Any], str], Any]
"""``parse(arguments, where)``: a layer's arguments, checked, as the settings
the layer keeps (``where`` names the line for error messages)."""


@dataclass(frozen=True)
class LayerKind:
    """A protocol layer Jig knows: what it runs on, what it provides, and how
    it reads its arguments (``parse``; None takes no arguments)."""

    name: str
    runs_on: tuple[str, ...]
    provides: str
    parse: ArgumentParser | None = None

    def settings(self, arguments: Mapping[str, Any], where: str) -> Any:
        if self.parse is not None:
            return self.parse(arguments, where)
        if arguments:
            raise ConfigError(f"{where}: {self.name} takes no arguments")
        return None


@dataclass(frozen=True)
class Layer:
    """One layer of a stack: its kind's name, the arguments it was given and,
    once its kind is known, the settings the kind made of them. ``instance``
    is the device's protocol line it stands for (``protocol csp(...)``)."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    settings: Any = None
    instance: str | None = None

    def __repr__(self) -> str:
        return self.name if not self.arguments else f"{self.name}(...)"


@dataclass(frozen=True)
class Stack:
    """Layers top first, as ``over`` joins them. The top of a node's stack is
    its node declaration."""

    layers: tuple[Any, ...]

    def __repr__(self) -> str:
        return " over ".join(repr(layer) for layer in self.layers)


class LayerConstructor:
    """``kiss``, ``cli(eol: "\\r\\n")``: a layer in a plan's system or a stack."""

    def __init__(self, kind: LayerKind) -> None:
        self.kind = kind
        self.__name__ = kind.name

    def __repr__(self) -> str:
        return f"<layer {self.kind.name}>"

    def __call__(self, **arguments: Any) -> Layer:
        return Layer(self.kind.name, dict(arguments), self.kind.settings(arguments, self.kind.name))


def stack_of(values: Sequence[Any]) -> Stack:
    """``a over b over c`` from the evaluated operands: a layer named alone
    takes its defaults."""
    layers: list[Any] = []
    for index, value in enumerate(values):
        if isinstance(value, LayerConstructor):
            value = value()
        if not isinstance(value, Layer) and not (index == 0 and _is_node(value)):
            raise ConfigError(f"over stacks protocol layers, such as csp over kiss, not {value!r}")
        layers.append(value)
    return Stack(tuple(layers))


def _is_node(value: Any) -> bool:
    from jig.lang.library import NodeDeclaration

    return isinstance(value, NodeDeclaration)


# ------------------------------------------------------------------ layers


def _cli(arguments: Mapping[str, Any], where: str) -> dict[str, Any]:
    unknown = sorted(set(arguments) - {"eol", "prompt"})
    if unknown:
        raise ConfigError(f"{where}: cli takes eol and prompt, not {', '.join(unknown)}")
    eol = arguments.get("eol", DEFAULT_EOL)
    if not isinstance(eol, str) or eol.lower() not in EOLS:
        raise ConfigError(f'{where}: cli eol is "\\r", "\\n" or "\\r\\n", not {eol!r}')
    prompt = arguments.get("prompt")
    if prompt is not None and not isinstance(prompt, str) and not hasattr(prompt, "pattern"):
        raise ConfigError(f"{where}: cli prompt is text or a /regex/, not {prompt!r}")
    return {"eol": EOLS[eol.lower()], "prompt": prompt}


CLI = LayerKind("cli", (TERMINAL_TEXT, BYTE_STREAM), COMMANDS, _cli)
VT100 = LayerKind("vt100", (BYTE_STREAM,), TERMINAL_TEXT)

BUILT_IN: dict[str, LayerKind] = {kind.name: kind for kind in (CLI, VT100)}


def known() -> dict[str, LayerKind]:
    """Every layer kind: the built-in ones, then what extensions and the
    plugins loaded so far registered (``api.protocol``)."""
    from jig.system import registry

    plugins = registry.default()
    plugins.ensure_extensions()
    kinds = dict(BUILT_IN)
    for name, entry in plugins.protocols_for(None).items():
        kinds.setdefault(name, entry.value)
    return kinds


def kind(name: str, kinds: Mapping[str, LayerKind] | None = None) -> LayerKind:
    found = (kinds if kinds is not None else known()).get(name)
    if found is None:
        listed = ", ".join(sorted(kinds if kinds is not None else known()))
        raise ConfigError(f"no protocol layer {name!r} (known: {listed})")
    return found


def constructors(kinds: Mapping[str, LayerKind] | None = None) -> dict[str, LayerConstructor]:
    """A constructor per layer kind, for a scope that builds stacks."""
    return {name: LayerConstructor(item) for name, item in (kinds or known()).items()}


def check(
    layers: Iterable[str], gives: str, below: str, kinds: Mapping[str, LayerKind] | None = None
) -> str:
    """Check layers (named top first) on something that gives ``gives`` (an
    interface or link called ``below``). Returns what the top layer provides;
    a layer that does not fit raises a ConfigError naming the layer that would
    join it to what is below it."""
    table = kinds if kinds is not None else known()
    for name in reversed(list(layers)):
        layer = kind(name, table)
        if gives not in layer.runs_on:
            raise ConfigError(_misfit(layer, gives, below, table))
        gives, below = layer.provides, layer.name
    return gives


def fits(runs_on: Sequence[str], gives: str, below: str, what: str) -> None:
    """A node (``what``) that runs on ``runs_on`` on top of ``gives``."""
    if gives in runs_on:
        return
    table = known()
    bridge = _bridge(runs_on, gives, table)
    needs = " or ".join(phrase(item) for item in runs_on)
    tail = f": put {bridge} between them" if bridge else ""
    raise ConfigError(f"{what} needs {needs} and {below} gives {phrase(gives)}{tail}")


def _misfit(layer: LayerKind, gives: str, below: str, table: Mapping[str, LayerKind]) -> str:
    needs = " or ".join(phrase(item) for item in layer.runs_on)
    bridge = _bridge(layer.runs_on, gives, table)
    tail = f": put {bridge} between them" if bridge else ""
    return f"{layer.name} needs {needs} and {below} gives {phrase(gives)}{tail}"


def _bridge(runs_on: Sequence[str], gives: str, table: Mapping[str, LayerKind]) -> str | None:
    for candidate in table.values():
        if gives in candidate.runs_on and candidate.provides in runs_on:
            return candidate.name
    return None
