"""Reading property values out of Renode platform descriptions (``.repl``).

Only what checks need: the literal values a file assigns to peripheral
properties, from both full entries (``name: Type @ ...``) and update entries
(``name:``). Later assignments win, as they do when Renode merges entries.
Values that are not plain numbers or strings are kept as their source text.
"""

from __future__ import annotations

import re
from pathlib import Path

_ENTRY = re.compile(r"^(?P<name>[A-Za-z_][\w.]*)\s*:(?P<rest>.*)$")
_PROPERTY = re.compile(r"^\s+(?P<key>[A-Za-z_]\w*)\s*:\s*(?P<value>[^\n]*?)\s*$")


def _strip_comment(line: str) -> str:
    in_string = False
    for index in range(len(line) - 1):
        if line[index] == '"':
            in_string = not in_string
        if not in_string and line[index : index + 2] == "//":
            return line[:index]
    return line


def _value(text: str) -> int | float | str:
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    try:
        return int(text, 0)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def properties(path: Path) -> dict[str, dict[str, int | float | str]]:
    """``{peripheral: {property: value}}`` for one file (``using`` is not followed)."""
    return properties_of(path.read_text(encoding="utf-8"))


def properties_of(text: str) -> dict[str, dict[str, int | float | str]]:
    """``{peripheral: {property: value}}`` for a description's text."""
    result: dict[str, dict[str, int | float | str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        if not line[0].isspace():
            match = _ENTRY.match(line)
            current = match.group("name") if match and not line.startswith("using") else None
            continue
        if current is None:
            continue
        match = _PROPERTY.match(line)
        if match is None or match.group("value") == "" or "->" in line:
            continue
        result.setdefault(current, {})[match.group("key")] = _value(match.group("value"))
    return result
