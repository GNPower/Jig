"""Protocol layers: what each runs on and provides, the stack check's
messages, the layers' arguments and the layers plugins add."""

from __future__ import annotations

import pytest

from jig.errors import ConfigError
from jig.ext.csp.settings import CspSettings, Housekeeping
from jig.plugin import PluginApi
from jig.system import protocols
from jig.system.protocols import BYTE_STREAM, FRAMES, Layer, LayerConstructor
from jig.system.registry import Owner, Registry


def test_the_five_layers_stack_as_the_table_says() -> None:
    kinds = protocols.known()
    assert {"kiss", "cfp", "csp", "cli", "vt100"} <= set(kinds)
    assert protocols.check(["csp", "kiss"], BYTE_STREAM, "uart0") == protocols.CSP_SERVICES
    assert protocols.check(["csp", "cfp"], FRAMES, "can0") == protocols.CSP_SERVICES
    assert protocols.check(["cli"], BYTE_STREAM, "uart1") == protocols.COMMANDS
    assert protocols.check(["cli", "vt100"], BYTE_STREAM, "uart1") == protocols.COMMANDS
    assert protocols.check([], FRAMES, "can0") == FRAMES


@pytest.mark.parametrize(
    ("layers", "gives", "message"),
    [
        (["csp"], BYTE_STREAM, "csp needs packets and uart0 gives a byte stream: put kiss between"),
        (["csp"], FRAMES, "csp needs packets and uart0 gives CAN frames: put cfp between them"),
        (["kiss"], FRAMES, "kiss needs a byte stream and uart0 gives CAN frames$"),
        (["cli"], FRAMES, "cli needs terminal text or a byte stream and uart0 gives CAN frames"),
        (["csp", "vt100"], BYTE_STREAM, "csp needs packets and vt100 gives terminal text$"),
        (["morse"], BYTE_STREAM, r"no protocol layer 'morse' \(known: cfp, cli, csp, kiss"),
    ],
)
def test_a_stack_that_does_not_fit_names_the_missing_layer(
    layers: list[str], gives: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        protocols.check(layers, gives, "uart0")


def test_cli_takes_an_end_of_line_and_a_prompt() -> None:
    cli = LayerConstructor(protocols.CLI)
    assert cli().settings == {"eol": "\r", "prompt": None}
    assert cli(eol="\r\n", prompt="zed> ").settings == {"eol": "\r\n", "prompt": "zed> "}
    assert cli(eol="crlf").settings["eol"] == "\r\n"
    with pytest.raises(ConfigError, match=r"cli eol is"):
        cli(eol="tab")
    with pytest.raises(ConfigError, match="cli takes eol and prompt, not speed"):
        cli(speed=3)
    with pytest.raises(ConfigError, match="vt100 takes no arguments"):
        LayerConstructor(protocols.VT100)(rows=24)


def test_a_layer_named_alone_takes_its_defaults() -> None:
    kiss = LayerConstructor(protocols.kind("kiss"))
    stack = protocols.stack_of([LayerConstructor(protocols.CLI), Layer("vt100")])
    assert [layer.name for layer in stack.layers] == ["cli", "vt100"]
    assert repr(protocols.stack_of([kiss])) == "kiss"
    with pytest.raises(ConfigError, match="over stacks protocol layers"):
        protocols.stack_of([kiss, 3])


def test_the_csp_layer_reads_rdp_times_with_units() -> None:
    csp = LayerConstructor(protocols.kind("csp"))
    settings = csp(
        address=5,
        housekeeping={"port": 10, "request": b"\x01", "codec": "status"},
        rdp={"ack_timeout": 0.1, "conn_timeout": 10.0, "window_size": 8, "delayed_acks": False},
    ).settings
    assert settings == CspSettings(
        5,
        Housekeeping(10, b"\x01", "status", {}),
        {"ack_timeout_ms": 100, "conn_timeout_ms": 10_000, "window_size": 8, "delayed_acks": False},
    )
    for arguments, message in (
        ({"address": 40}, "csp address must be an integer 0-31"),
        ({"rdp": {"ack_timeout_ms": 100}}, "rdp has no option ack_timeout_ms"),
        ({"rdp": {"ack_timeout": -1}}, "rdp ack_timeout is a positive time"),
        ({"rdp": {"window_size": 1.5}}, "rdp window_size is a positive whole number"),
        ({"ping": True}, "csp takes address, housekeeping and rdp, not ping"),
    ):
        with pytest.raises(ConfigError, match=message):
            csp(**arguments)


def test_plugins_add_layers(_fresh_registry: Registry) -> None:
    def parse(arguments: object, where: str) -> object:
        return arguments

    api = PluginApi(Owner("board", "x", None))
    api.protocol("pus", runs_on="packets", provides="PUS services", parse=parse)
    _fresh_registry.commit(api.staged)
    pus = protocols.known()["pus"]
    assert (pus.runs_on, pus.provides) == (("packets",), "PUS services")
    assert protocols.check(["pus", "kiss"], BYTE_STREAM, "uart0") == "PUS services"
    other = PluginApi(Owner("board", "y", None))
    other.protocol("pus", runs_on="packets", provides="something else")
    with pytest.raises(ConfigError, match="registers protocol layer 'pus', which board x"):
        _fresh_registry.commit(other.staged)
    with pytest.raises(ConfigError, match="runs_on names what it runs on"):
        PluginApi(Owner("board", "z", None)).protocol("bad", runs_on=(), provides="x")
