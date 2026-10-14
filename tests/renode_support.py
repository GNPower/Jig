"""Test helpers for Renode: fake installs for discovery, the real one for the
``renode`` tier.

A fake install is a launcher script that answers ``--version`` like Renode
(``renode.cmd`` on Windows, an executable ``renode`` shell script elsewhere)
beside an empty ``Renode.dll``; each call appends a line to ``calls.txt`` so
tests can see whether discovery ran it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jig.errors import ToolchainError
from jig.renode.discover import RenodeInstall, find_renode, load_project_config

WINDOWS = os.name == "nt"
LAUNCHER = "renode.cmd" if WINDOWS else "renode"


def version_text(version: str, build: str | None = None, runtime: str = ".NET 8.0.10") -> str:
    return (
        f"Renode v{version}\n"
        f"  build: {build or version + '+20260906gitf1dd1b4af'}\n"
        "  build type: Release\n"
        f"  runtime: {runtime}\n"
    )


def fake_renode(
    directory: Path,
    version: str = "1.17.0",
    *,
    build: str | None = None,
    name: str = LAUNCHER,
    exit_code: int = 0,
) -> Path:
    """A launcher in ``directory`` that prints Renode's ``--version`` text."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = version_text(version, build).splitlines()
    launcher = directory / name
    if WINDOWS:
        body = ["@echo off", 'echo x>>"%~dp0calls.txt"']
        body += [f"echo {line}" for line in lines]
        body.append(f"exit /b {exit_code}")
        launcher.write_text("\r\n".join(body) + "\r\n", encoding="ascii")
    else:
        # Shell built-ins only: tests run it with an empty PATH.
        body = ["#!/bin/sh", 'echo x >> "${0%/*}/calls.txt"']
        body += [f"echo '{line}'" for line in lines]
        body.append(f"exit {exit_code}")
        launcher.write_text("\n".join(body) + "\n", encoding="ascii")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (directory / "Renode.dll").write_bytes(b"")
    (directory / "Infrastructure.dll").write_bytes(b"")
    return launcher


def calls(launcher: Path) -> int:
    """How often the fake ``launcher`` ran."""
    record = launcher.parent / "calls.txt"
    return len(record.read_text(encoding="ascii").split()) if record.exists() else 0


def real_renode() -> RenodeInstall:
    """The Renode the ``renode`` tier runs on (``JIG_RENODE`` or discovery), or a skip."""
    try:
        return find_renode(explicit=None, config=load_project_config())
    except ToolchainError as exc:
        pytest.skip(f"no Renode: {exc}")


SMALLEST_ZYNQ_OVERLAY = """\
globalTimer:
    frequency: 325000000

privateTimer:
    frequency: 325000000

swdt:
    frequency: 108333333
"""
"""The properties zynq7000.repl leaves without a default: the clocks of the
Cortex-A9 timers (CPU_3x2x) and of the system watchdog (CPU_1x), here for a
650 MHz CPU clock with the 6:2:1 ratio."""
