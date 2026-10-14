"""Register maps from airhdl-generated SystemVerilog.

airhdl emits, per register file, a package (``*_regs_pkg.sv``) with offsets,
field positions and reset values, and a module (``*_regs.sv``) whose ports (or,
for struct-port register files, the package's ``user2regs_t``/``regs2user_t``
types) say how each register is accessed:

* "strobe ... pulsed when the register is written from the bus" -> software
  writes it (``rw``),
* "strobe ... pulsed when the register is read from the bus" -> hardware drives
  it, software only reads (``ro``),
* "set signal for field 'REG.field'" -> a hardware-set sticky flag that software
  clears by writing 1 (``w1c`` field),
* "read/write address for memory 'name'" -> a memory; it is readable only when a
  "read data for memory" port exists.

``parse`` turns that into a :class:`RegisterMap`, serialisable to the JSON the
C# AirhdlRegisterBlock model loads, so the simulated PL always matches the RTL
revision in the repository.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jig.errors import ConfigError

_OFFSET = re.compile(
    r"localparam\s+logic\s*\[31:0\]\s+(?P<const>\w+)_OFFSET\s*=\s*32'h(?P<hex>[0-9A-Fa-f_]+)\s*;"
    r"\s*//\s*address offset of the '(?P<name>[^']+)' register"
)
_BIT_OFFSET = re.compile(
    r"localparam\s+(?P<const>\w+)_BIT_OFFSET\s*=\s*(?P<value>\d+)\s*;"
    r"\s*//\s*bit offset of the '(?P<field>[^']+)' field"
)
_BIT_WIDTH = re.compile(r"localparam\s+(?P<const>\w+)_BIT_WIDTH\s*=\s*(?P<value>\d+)\s*;")
_RESET = re.compile(
    r"localparam\s+logic\s*\[(?P<msb>\d+):(?P<lsb>\d+)\]\s+(?P<const>\w+)_RESET\s*=\s*"
    r"(?P<width>\d+)'(?P<base>[bhd])(?P<digits>[0-9A-Fa-f_]+)\s*;"
)
_DEPTH = re.compile(
    r"localparam\s+(?P<const>\w+)_DEPTH\s*=\s*(?P<value>\d+)\s*;"
    r"\s*//\s*depth of the '(?P<name>[^']+)' memory"
)
_STROBE = re.compile(
    r"strobe (?:signal|logic) for register '(?P<name>[^']+)' "
    r"\(pulsed when the register is (?P<how>written to|read from|written from|read by)"
)
_SET = re.compile(r"set signal for field '(?P<reg>[^'.]+)\.(?P<field>[^']+)'")
_MEMORY_READ = re.compile(r"read data for memory '(?P<name>[^']+)'")


@dataclass
class Field:
    name: str
    offset: int
    width: int
    reset: int = 0
    kind: str = "value"  # "value" or "w1c" (hardware-set, write-1-to-clear)


@dataclass
class Register:
    name: str
    offset: int
    access: str = "rw"  # "rw", "ro" or "wo"
    fields: list[Field] = field(default_factory=list)

    @property
    def reset(self) -> int:
        value = 0
        for f in self.fields:
            value |= (f.reset & ((1 << f.width) - 1)) << f.offset
        return value


@dataclass
class Memory:
    name: str
    offset: int
    depth: int
    readable: bool = False


@dataclass
class RegisterMap:
    name: str
    registers: list[Register] = field(default_factory=list)
    memories: list[Memory] = field(default_factory=list)
    source: list[str] = field(default_factory=list)

    def register(self, name: str) -> Register:
        for r in self.registers:
            if r.name.lower() == name.lower():
                return r
        raise KeyError(name)

    def to_json(self) -> str:
        data = asdict(self)
        for reg, reg_data in zip(self.registers, data["registers"], strict=True):
            reg_data["reset"] = reg.reset
        return json.dumps(data, indent=2)


def _parse_number(base: str, digits: str) -> int:
    digits = digits.replace("_", "")
    return int(digits, {"b": 2, "h": 16, "d": 10}[base])


def parse(package: Path, module: Path | None = None) -> RegisterMap:
    """Parse one airhdl register file: its package and (if separate from the
    package's struct types) its module."""
    pkg_text = package.read_text(encoding="utf-8", errors="replace")
    access_text = pkg_text + (
        "\n" + module.read_text(encoding="utf-8", errors="replace") if module else ""
    )
    map_name = package.name.removesuffix("_pkg.sv")
    result = RegisterMap(name=map_name, source=[str(package)] + ([str(module)] if module else []))

    # Registers and memories, in file order: every *_OFFSET opens one.
    consts: dict[str, Register | Memory] = {}
    depths = {m.group("name").lower(): int(m.group("value")) for m in _DEPTH.finditer(pkg_text)}
    for m in _OFFSET.finditer(pkg_text):
        name, offset = m.group("name"), int(m.group("hex").replace("_", ""), 16)
        if name.lower() in depths:
            memory = Memory(name=name, offset=offset, depth=depths[name.lower()])
            result.memories.append(memory)
            consts[m.group("const")] = memory
        else:
            register = Register(name=name, offset=offset)
            result.registers.append(register)
            consts[m.group("const")] = register

    # Fields: "<REGCONST>_<FIELDCONST>_BIT_OFFSET", matched to the longest register prefix.
    by_prefix = sorted(consts.items(), key=lambda item: -len(item[0]))
    widths = {m.group("const"): int(m.group("value")) for m in _BIT_WIDTH.finditer(pkg_text)}
    resets = {m.group("const"): m for m in _RESET.finditer(pkg_text)}
    for m in _BIT_OFFSET.finditer(pkg_text):
        const = m.group("const")
        owner = next((r for prefix, r in by_prefix if const.startswith(prefix + "_")), None)
        if owner is None:
            raise ConfigError(f"{package.name}: field constant {const} matches no register")
        if isinstance(owner, Memory):
            continue  # memory word layout: the model treats memory words as whole values
        reset_match = resets.get(const)
        if const in widths:
            width = widths[const]
        elif reset_match is not None:
            width = int(reset_match.group("msb")) - int(reset_match.group("lsb")) + 1
        else:
            raise ConfigError(f"{package.name}: no width for field {const}")
        reset = (
            _parse_number(reset_match.group("base"), reset_match.group("digits"))
            if reset_match
            else 0
        )
        owner.fields.append(
            Field(name=m.group("field"), offset=int(m.group("value")), width=width, reset=reset)
        )

    # Access kinds from strobe / set / memory comments.
    lowered = {r.name.lower(): r for r in result.registers}
    for m in _STROBE.finditer(access_text):
        strobed = lowered.get(m.group("name").lower())
        if strobed is not None and m.group("how").startswith("read"):
            strobed.access = "ro"
    for m in _SET.finditer(access_text):
        flagged = lowered.get(m.group("reg").lower())
        if flagged is None:
            continue
        for f in flagged.fields:
            if f.name.lower() == m.group("field").lower():
                f.kind = "w1c"
    readable = {m.group("name").lower() for m in _MEMORY_READ.finditer(access_text)}
    for memory in result.memories:
        memory.readable = memory.name.lower() in readable

    if not result.registers:
        raise ConfigError(f"{package.name}: no registers found")
    return result


def write_json(register_map: RegisterMap, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(register_map.to_json(), encoding="utf-8", newline="\n")
    return destination
