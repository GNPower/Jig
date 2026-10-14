"""Content-addressed cache for generated artifacts.

Register maps, register programs and flash images are derived from files in
the repository (RTL, XSA, ELFs). Each artifact lives in
``<cache>/artifacts/<kind>-<digest>/``, where the digest covers the contents of every
input file and the build parameters, so a changed input produces a new entry
and an unchanged one is reused. Entries are built in a temporary directory and
renamed into place, so an interrupted build never leaves a half-written entry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from jig import project
from jig.errors import ConfigError

log = logging.getLogger(__name__)

_COMPLETE = ".complete"


def cache_root() -> Path:
    return project.artifacts_dir()


def digest(inputs: Iterable[Path], params: dict[str, Any] | None = None) -> str:
    hasher = hashlib.sha256()
    for path in inputs:
        if not path.is_file():
            raise ConfigError(f"artifact input {path} does not exist")
        hasher.update(path.name.encode())
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(block)
    hasher.update(json.dumps(params or {}, sort_keys=True, default=str).encode())
    return hasher.hexdigest()[:16]


def cached(
    kind: str,
    inputs: Iterable[Path],
    build: Callable[[Path], None],
    params: dict[str, Any] | None = None,
) -> Path:
    """Directory holding the artifact; ``build(directory)`` fills a new one."""
    input_list = list(inputs)
    entry = cache_root() / f"{kind}-{digest(input_list, params)}"
    if (entry / _COMPLETE).exists():
        return entry
    entry.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{kind}-", dir=entry.parent))
    try:
        log.info("building %s", entry.name)
        build(staging)
        (staging / _COMPLETE).write_text(
            json.dumps(
                {"inputs": [str(p) for p in input_list], "params": params or {}},
                default=str,
                indent=2,
            ),
            encoding="utf-8",
        )
        if entry.exists():
            shutil.rmtree(entry)
        staging.rename(entry)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return entry
