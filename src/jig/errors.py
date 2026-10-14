"""Exception hierarchy. Every error raised by Jig derives from JigError,
so callers can separate framework failures from test verdicts."""

from __future__ import annotations


class JigError(Exception):
    """Base class for all Jig errors."""

    __test__ = False  # keep pytest from collecting this as a test class


class ToolchainError(JigError):
    """A required tool is missing, corrupt, or failed verification."""


class RenodeError(JigError):
    """Renode could not be started, reached, or stopped."""


class RenodeCommandError(RenodeError):
    """A Renode keyword or monitor command reported failure."""

    def __init__(self, keyword: str, args: tuple[object, ...], message: str) -> None:
        self.keyword = keyword
        self.args_used = args
        self.message = message.strip()
        shown = ", ".join(repr(a) for a in args)
        super().__init__(f"{keyword}({shown}) failed: {self.message}")


class ConfigError(JigError):
    """A board, system, or plan definition is invalid."""


class SimulationError(JigError):
    """The simulated system can no longer run (a machine aborted, Renode died)."""


class MachineAbortedError(SimulationError):
    """Renode aborted a machine (e.g. a CPU fetched code outside memory)."""

    def __init__(self, machine: str, detail: str, simulated_time: float) -> None:
        self.machine = machine
        self.detail = detail.strip()
        self.simulated_time = simulated_time
        super().__init__(f"machine {machine!r} aborted at t={simulated_time:.6f} s: {self.detail}")


class ExpectTimeoutError(JigError):
    """An awaited output did not appear within its simulated-time limit."""

    def __init__(self, interface: str, pattern: str, waited: float, tail: str) -> None:
        self.interface = interface
        self.pattern = pattern
        self.waited = waited
        self.tail = tail
        super().__init__(
            f"{interface}: {pattern!r} not seen within {waited:g} s (simulated); "
            f"last output: {tail[-400:]!r}"
        )
