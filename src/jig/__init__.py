"""Jig: a test language and harness for firmware running on Renode.

The package is board-agnostic. Boards are described by data (``board.yaml``),
peripheral models are C# sources Renode loads, and test plans are ``.jig``
scripts.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jig-dsl")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"
