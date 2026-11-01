"""Builds the STM32F4 smoke-test firmware into ``<output>/firmware.elf``.

    python build.py <output> [--cc <arm-none-eabi-gcc>]

The stm32 family runs it with ``--cc`` (the device's ``firmware: build(...)``).
Without ``--cc`` the compiler is ``ARM_NONE_EABI_GCC``, else
``arm-none-eabi-gcc`` on PATH. Any compiler or linker warning fails the build.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

COMPILE = [
    "-mcpu=cortex-m4",
    "-mthumb",
    "-mfloat-abi=soft",
    "-O2",
    "-g",
    "-std=c11",
    "-ffreestanding",
    "-fno-common",
    "-ffunction-sections",
    "-fdata-sections",
    "-Wall",
    "-Wextra",
    "-Werror",
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
        str(HERE / "hello.c"),
        *LINK,
        "-T",
        str(HERE / "link.ld"),
        "-o",
        str(args.output / "firmware.elf"),
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
