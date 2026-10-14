"""Firmware build script of the example-stm32 fixture.

The STM32 driver runs ``python build.py <output-dir> --cc <gcc>``; the unit
tests check only that this script and its sources exist.
"""

import sys

if __name__ == "__main__":
    sys.exit("the example-stm32 fixture builds no firmware")
