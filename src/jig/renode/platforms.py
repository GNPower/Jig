"""Platform descriptions Jig ships, by name.

A device family names the platform its devices build on as
``<vendor>/<family>``, the path of the ``.repl`` file below ``jig/platforms``
without its suffix: ``xilinx/zynq7000``. The platform description Jig writes
for a board starts by using that file (``jig.system.generate``), so it holds
only what the board adds.
"""

from __future__ import annotations

import re
from pathlib import Path

from jig import project
from jig.errors import ConfigError

SUFFIX = ".repl"
_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*)*")


def root() -> Path:
    """The directory holding the platform descriptions."""
    return project.package_file("platforms")


def available() -> list[str]:
    """Every platform name Jig ships, sorted."""
    base = root()
    return sorted(
        path.relative_to(base).with_suffix("").as_posix() for path in base.rglob("*.repl")
    )


def path(name: str) -> Path:
    """The platform description called ``name`` (``xilinx/zynq7000``)."""
    if not _NAME.fullmatch(name) or ".." in name.split("/"):
        raise ConfigError(f"{name!r} is not a platform name such as 'xilinx/zynq7000'")
    found = root().joinpath(*f"{name}{SUFFIX}".split("/"))
    if not found.is_file():
        known = ", ".join(available()) or "none"
        raise ConfigError(f"Jig ships no platform {name!r} (has: {known})")
    return found
