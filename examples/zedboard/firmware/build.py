#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Graham Power
"""Build the ZedBoard example firmware.

Compiles the sources in src/ once per node and writes build/zed-node-1.elf
(zed-a, CSP address 1) and build/zed-node-2.elf (zed-b, CSP address 2), then
prints arm-none-eabi-size for each.

The compiler is the first of: --cc, the ARM_NONE_EABI_GCC environment variable,
arm-none-eabi-gcc on PATH. The other binutils (size) are looked up next to it.
Needs Python 3.11 or newer and nothing outside the standard library.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NODES = (1, 2)

CPU_FLAGS = ["-mcpu=cortex-a9", "-marm", "-mfloat-abi=soft", "-mno-unaligned-access"]
C_FLAGS = [
    "-std=c11",
    "-O2",
    "-g",
    "-ffreestanding",
    "-fno-common",
    # Keeps GCC from turning the byte loops in lib.c into calls to themselves.
    "-fno-tree-loop-distribute-patterns",
    "-ffunction-sections",
    "-fdata-sections",
    "-Wall",
    "-Wextra",
    "-Werror",
]
# The Vitis toolchain links with --build-id by default; Ubuntu's does not.
LINK_FLAGS = ["-nostdlib", "-nostartfiles", "-static", "-Wl,--gc-sections", "-Wl,--build-id=none"]


class BuildError(Exception):
    pass


def find_compiler(option: str | None) -> Path:
    for candidate in (option, os.environ.get("ARM_NONE_EABI_GCC")):
        if candidate:
            path = Path(candidate)
            if path.is_file():
                return path
            found = shutil.which(candidate)
            if found:
                return Path(found)
            raise BuildError(f"compiler not found: {candidate}")
    found = shutil.which("arm-none-eabi-gcc")
    if found:
        return Path(found)
    raise BuildError(
        "no compiler: pass --cc, set ARM_NONE_EABI_GCC or put arm-none-eabi-gcc on PATH"
    )


def sibling_tool(gcc: Path, tool: str) -> Path:
    """arm-none-eabi-gcc[.exe] -> arm-none-eabi-<tool>[.exe] in the same directory."""
    name = gcc.name
    index = name.rfind("gcc")
    if index < 0:
        raise BuildError(f"cannot derive {tool} from {gcc}")
    path = gcc.with_name(name[:index] + tool + name[index + 3 :])
    if not path.is_file():
        raise BuildError(f"{tool} not found next to the compiler: {path}")
    return path


def run(command: list[str], verbose: bool) -> str:
    if verbose:
        print(" ".join(command))
    result = subprocess.run(command, cwd=HERE, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise BuildError(f"{Path(command[0]).name} failed:\n{output}")
    if output:
        # A warning on a successful run: show it and fail, the build must be clean.
        raise BuildError(f"{Path(command[0]).name} reported:\n{output}")
    return result.stdout


def build_node(gcc: Path, node: int, out: Path, verbose: bool) -> Path:
    objects_dir = out / f"node-{node}"
    objects_dir.mkdir(parents=True, exist_ok=True)
    sources = [*sorted((HERE / "src").glob("*.c")), HERE / "src" / "start.S"]
    defines = [f"-DNODE_ADDRESS={node}", f"-ffile-prefix-map={HERE}=."]
    objects = []
    for source in sources:
        obj = objects_dir / (source.stem + ".o")
        relative = source.relative_to(HERE).as_posix()
        run([str(gcc), *CPU_FLAGS, *C_FLAGS, *defines, "-c", relative, "-o", str(obj)], verbose)
        objects.append(str(obj))
    elf = out / f"zed-node-{node}.elf"
    link = [
        str(gcc),
        *CPU_FLAGS,
        *LINK_FLAGS,
        "-T",
        "link.ld",
        f"-Wl,-Map={out / f'zed-node-{node}.map'}",
        *objects,
        "-lgcc",
        "-o",
        str(elf),
    ]
    run(link, verbose)
    return elf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cc", help="path or name of arm-none-eabi-gcc")
    parser.add_argument("--out", type=Path, default=HERE / "build", help="output directory")
    parser.add_argument(
        "--node", type=int, choices=NODES, action="append", help="build one node (repeatable)"
    )
    parser.add_argument("--clean", action="store_true", help="remove the output directory first")
    parser.add_argument("-v", "--verbose", action="store_true", help="print each command")
    args = parser.parse_args(argv)

    try:
        gcc = find_compiler(args.cc)
        size = sibling_tool(gcc, "size")
        version = subprocess.run(
            [str(gcc), "--version"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
        print(f"compiler: {gcc}\n          {version}")
        out = args.out.resolve()
        if args.clean and out.exists():
            shutil.rmtree(out)
        elves = [build_node(gcc, node, out, args.verbose) for node in (args.node or NODES)]
        print(
            subprocess.run(
                [str(size), *[str(elf) for elf in elves]],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.rstrip()
        )
    except BuildError as error:
        print(f"build.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
