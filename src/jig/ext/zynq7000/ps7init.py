"""Translate Vivado's ``ps7_init.tcl`` into a register program.

A debugger initialises a Zynq-7000 for JTAG boot by running ``ps7_init`` (MIO,
PLLs, clocks, DDR, peripherals, chosen by silicon revision) and later
``ps7_post_config`` (PS-PL level shifters, FPGA resets). This module flattens
the same procedures into the line format that the C# ``RunRegisterProgram``
executes:

    W <addr> <value>          32-bit write   (``mwr``)
    M <addr> <mask> <value>   masked write   (``mask_write``)
    P <addr> <mask>           poll until any masked bit is set (``mask_poll``)

``mask_delay`` waits on the global timer and changes no register, so it is kept
as a comment.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from jig.errors import ConfigError

# ps7_init.tcl: PCW_SILICON_VER_1_0 = 0x0, _2_0 = 0x1, _3_0 = 0x2; anything else
# takes the 3.0 branch.
_VERSION_SUFFIX = {0x0: "1_0", 0x1: "2_0"}
_INIT_STAGES = ("mio", "pll", "clock", "ddr", "peripherals")

_PROC = re.compile(r"^proc\s+(\w+)\s*\{[^}]*\}\s*\{", re.MULTILINE)
_HEX = r"(0[xX][0-9A-Fa-f]+)"
_MASK_WRITE = re.compile(rf"^\s*mask_write\s+{_HEX}\s+{_HEX}\s+{_HEX}\s*$")
_MWR = re.compile(rf"^\s*mwr(?:\s+-force)?\s+{_HEX}\s+{_HEX}\s*$")
_MASK_POLL = re.compile(rf"^\s*mask_poll\s+{_HEX}\s+{_HEX}\s*$")
_MASK_DELAY = re.compile(rf"^\s*mask_delay\s+{_HEX}\s+(\S+)\s*$")


@dataclass(frozen=True)
class Operation:
    kind: str  # "W", "M", "P", or "#" (comment)
    address: int = 0
    mask: int = 0
    value: int = 0
    text: str = ""

    def render(self) -> str:
        if self.kind == "W":
            return f"W 0x{self.address:08X} 0x{self.value:08X}"
        if self.kind == "M":
            return f"M 0x{self.address:08X} 0x{self.mask:08X} 0x{self.value:08X}"
        if self.kind == "P":
            return f"P 0x{self.address:08X} 0x{self.mask:08X}"
        return f"# {self.text}"


def _procs(text: str) -> dict[str, str]:
    """Bodies of the script's ``proc`` definitions, braces balanced."""
    bodies: dict[str, str] = {}
    for match in _PROC.finditer(text):
        depth, start = 1, match.end()
        index = start
        while index < len(text) and depth:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        if depth:
            raise ConfigError(f"ps7_init.tcl: unbalanced braces in proc {match.group(1)}")
        bodies[match.group(1)] = text[start : index - 1]
    return bodies


def _operations(name: str, body: str) -> list[Operation]:
    ops = [Operation("#", text=name)]
    for line in body.splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if m := _MASK_WRITE.match(line):
            ops.append(Operation("M", int(m[1], 16), int(m[2], 16), int(m[3], 16)))
        elif m := _MWR.match(line):
            ops.append(Operation("W", int(m[1], 16), value=int(m[2], 16)))
        elif m := _MASK_POLL.match(line):
            ops.append(Operation("P", int(m[1], 16), int(m[2], 16)))
        elif m := _MASK_DELAY.match(line):
            ops.append(
                Operation("#", text=f"mask_delay {m[1]} {m[2]} (time only, no register effect)")
            )
        else:
            raise ConfigError(f"ps7_init.tcl: unsupported statement in {name}: {line.strip()!r}")
    return ops


@dataclass(frozen=True)
class Ps7Init:
    """``ps7_init`` and ``ps7_post_config`` for one silicon revision."""

    init: list[Operation]
    post_config: list[Operation]

    @staticmethod
    def from_tcl(text: str, ps_version: int) -> Ps7Init:
        procs = _procs(text)
        suffix = _VERSION_SUFFIX.get(ps_version, "3_0")
        init: list[Operation] = []
        for stage in _INIT_STAGES:
            name = f"ps7_{stage}_init_data_{suffix}"
            if name not in procs:
                raise ConfigError(f"ps7_init.tcl has no {name}")
            init.extend(_operations(name, procs[name]))
        post_name = f"ps7_post_config_{suffix}"
        if post_name not in procs:
            raise ConfigError(f"ps7_init.tcl has no {post_name}")
        return Ps7Init(init=init, post_config=_operations(post_name, procs[post_name]))

    @staticmethod
    def from_xsa(xsa: Path, ps_version: int) -> Ps7Init:
        with zipfile.ZipFile(xsa) as archive:
            names = [n for n in archive.namelist() if n.endswith("ps7_init.tcl")]
            if not names:
                raise ConfigError(f"{xsa} contains no ps7_init.tcl")
            text = archive.read(names[0]).decode("utf-8", errors="replace")
        return Ps7Init.from_tcl(text, ps_version)


def write_program(operations: list[Operation], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "\n".join(op.render() for op in operations) + "\n", encoding="utf-8", newline="\n"
    )
    return destination
