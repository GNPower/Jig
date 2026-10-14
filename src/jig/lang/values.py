"""Values a plan computes with, and how they print.

Numbers carry no units at run time: a quantity literal converts to SI base
units when it is parsed (``100 ms`` is 0.1, ``1 Mbit/s`` is 1e6, ``1.2 V`` is
1.2), so every duration a library function takes is in seconds.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator, Mapping
from typing import Any

UNITS: dict[str, float] = {
    "ns": 1e-9,
    "us": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "min": 60.0,
    "h": 3600.0,
    "mV": 1e-3,
    "V": 1.0,
    "uA": 1e-6,
    "mA": 1e-3,
    "A": 1.0,
    "Hz": 1.0,
    "kHz": 1e3,
    "MHz": 1e6,
    "GHz": 1e9,
    "B": 1.0,
    "KiB": 1024.0,
    "MiB": 1024.0 * 1024.0,
    "bit/s": 1.0,
    "kbit/s": 1e3,
    "Mbit/s": 1e6,
}

_QUANTITY = re.compile(
    r"(?P<number>0x[0-9A-Fa-f_]+|0b[01_]+|\d[\d_]*(\.\d+)?([eE][+-]?\d+)?)"
    r"([ \t]*(?P<unit>[A-Za-z/]+))?$"
)


def parse_quantity(text: str) -> int | float:
    """A number literal with an optional unit, in SI base units."""
    match = _QUANTITY.match(text.strip())
    if match is None:
        raise ValueError(f"not a number: {text!r}")
    number = match.group("number").replace("_", "")
    value: int | float
    if number.startswith(("0x", "0X")):
        value = int(number, 16)
    elif number.startswith(("0b", "0B")):
        value = int(number, 2)
    elif any(c in number for c in ".eE"):
        value = float(number)
    else:
        value = int(number)
    unit = match.group("unit")
    if unit is None:
        return value
    if unit not in UNITS:
        raise ValueError(f"unknown unit {unit!r}")
    scaled = value * UNITS[unit]
    if isinstance(scaled, float) and scaled.is_integer() and UNITS[unit] >= 1:
        return int(scaled)
    return scaled


class Record(Mapping[str, Any]):
    """A read-only mapping whose keys are also attributes (``reply.header.length``)."""

    def __init__(self, values: Mapping[str, Any] | None = None, **more: Any) -> None:
        data = dict(values or {})
        data.update(more)
        self._data = {key: _wrap(value) for key, value in data.items()}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError:
            known = ", ".join(sorted(self._data)) or "none"
            raise AttributeError(f"no field {name!r} (fields: {known})") from None

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        inner = ", ".join(f"{key}: {show(value)}" for key, value in self._data.items())
        return "{" + inner + "}"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self._data) == dict(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(tuple(sorted(self._data.items(), key=lambda item: item[0])))

    def plain(self) -> dict[str, Any]:
        return {
            key: value.plain() if isinstance(value, Record) else value
            for key, value in self._data.items()
        }


def _wrap(value: Any) -> Any:
    if isinstance(value, Record):
        return value
    if isinstance(value, dict):
        return Record(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return record_of(value)
    return value


def record_of(instance: Any) -> Record:
    """A dataclass instance as a record: its fields and its public properties."""
    values: dict[str, Any] = {}
    for item in dataclasses.fields(instance):
        if not item.name.startswith("_"):
            values[item.name] = getattr(instance, item.name)
    for name in dir(type(instance)):
        if not name.startswith("_") and isinstance(getattr(type(instance), name, None), property):
            values[name] = getattr(instance, name)
    return Record(values)


class Regex:
    """A ``/pattern/flags`` literal."""

    def __init__(self, literal: str) -> None:
        body, _, flags = literal[1:].rpartition("/")
        bits = 0
        for flag in flags:
            bits |= {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}[flag]
        self.source = body.replace("\\/", "/")
        self.pattern = re.compile(self.source, bits)

    def search(self, text: str) -> re.Match[str] | None:
        return self.pattern.search(text)

    def __repr__(self) -> str:
        return f"/{self.source}/"


def as_pattern(value: Any) -> str:
    """The regex source for a Regex or a plain string (matched literally)."""
    if isinstance(value, Regex):
        return value.source
    if isinstance(value, str):
        return re.escape(value)
    raise TypeError(f"expected a /regex/ or a string, not {type_name(value)}")


def type_name(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, bytes):
        return "bytes"
    if isinstance(value, Regex):
        return "regex"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, Mapping):
        return "record"
    return type(value).__name__


def show(value: Any, limit: int = 120) -> str:
    """How a value appears in logs and failure messages."""
    if value is None:
        text = "none"
    elif value is True:
        text = "true"
    elif value is False:
        text = "false"
    elif isinstance(value, float):
        text = f"{value:.9g}"
    elif isinstance(value, str):
        text = repr(value)
    elif isinstance(value, bytes):
        text = (
            'x"' + value[:48].hex() + (".." if len(value) > 48 else "") + f'" ({len(value)} bytes)'
        )
    elif isinstance(value, re.Match):
        text = f"match {value.group(0)!r}"
    elif isinstance(value, (list, tuple)):
        text = "[" + ", ".join(show(item, limit) for item in value) + "]"
    else:
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def truthy(value: Any) -> bool:
    if isinstance(value, re.Match):
        return True
    return bool(value)
