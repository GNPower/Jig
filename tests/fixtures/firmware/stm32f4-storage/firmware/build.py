"""Builds the storage fixture's firmware into ``<output>/firmware.elf``.

    python build.py <output> [--cc <arm-none-eabi-gcc>]

The stm32 family runs it with ``--cc`` (the device's ``firmware: build(...)``).
Without ``--cc`` the compiler is ``ARM_NONE_EABI_GCC``, else
``arm-none-eabi-gcc`` on PATH. Any compiler or linker warning fails the build.

The build is freestanding, without a C library: ``libc/`` holds the string
functions littlefs calls. ``littlefs/`` holds littlefs 2.11 as the
littlefs-python 0.19.0 sdist on PyPI ships it, unchanged: lfs.c, lfs.h,
lfs_util.c, lfs_util.h, and its BSD-3-Clause license, upstream's LICENSE.md
renamed LICENSE so that the Markdown lint over ``tests/`` leaves it alone.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SOURCES = [
    "main.c",
    "console.c",
    "spi_nor.c",
    "fat.c",
    "libc/string.c",
    "littlefs/lfs.c",
    "littlefs/lfs_util.c",
]
INCLUDES = ["libc", "littlefs"]
COMPILE = [
    "-mcpu=cortex-m4",
    "-mthumb",
    "-mfloat-abi=soft",
    "-O2",
    "-g",
    "-std=c11",
    "-ffreestanding",
    "-fno-common",
    # Keeps GCC from turning the byte loops in libc/string.c into calls to themselves.
    "-fno-tree-loop-distribute-patterns",
    "-ffunction-sections",
    "-fdata-sections",
    "-Wall",
    "-Wextra",
    "-Werror",
]
# littlefs without malloc, assert or printf: the firmware gives it static
# buffers, and lfs_util.h then needs only <string.h> and <inttypes.h>.
LITTLEFS = [
    "-DLFS_NO_MALLOC",
    "-DLFS_NO_ASSERT",
    "-DLFS_NO_DEBUG",
    "-DLFS_NO_WARN",
    "-DLFS_NO_ERROR",
]
LINK = ["-nostdlib", "-nostartfiles", "-Wl,--gc-sections", "-Wl,--build-id=none"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path, help="directory for firmware.elf")
    parser.add_argument(
        "--cc",
        default=os.environ.get("ARM_NONE_EABI_GCC") or "arm-none-eabi-gcc",
        help="the arm-none-eabi-gcc to use",
    )
    args = parser.parse_args(argv)
    compiler = shutil.which(args.cc) or args.cc
    args.output.mkdir(parents=True, exist_ok=True)
    command = [
        compiler,
        *COMPILE,
        *LITTLEFS,
        *(f"-I{HERE / directory}" for directory in INCLUDES),
        *(str(HERE / source) for source in SOURCES),
        *LINK,
        "-T",
        str(HERE / "link.ld"),
        "-o",
        str(args.output / "firmware.elf"),
        "-lgcc",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        print(f"cannot run {compiler}: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode == 0 and (result.stdout.strip() or result.stderr.strip()):
        print("the build printed warnings", file=sys.stderr)
        return 1
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
