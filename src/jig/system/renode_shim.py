"""The Renode layer, as the system model sees it.

Modules outside ``jig.renode`` import these names from here, so the system
model, the CLI and their tests depend on one module rather than on the Renode
layer's internal layout.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from jig.renode.commands import add_commands, run
from jig.renode.discover import RenodeConfig, RenodeInstall, find_renode
from jig.renode.models import ModelSet
from jig.renode.session import RenodeSession

__all__ = [
    "ModelSet",
    "RenodeConfig",
    "RenodeInstall",
    "RenodeSession",
    "add_commands",
    "find_renode",
    "open_session",
    "run",
]


def open_session(
    *,
    workdir: Path,
    install: RenodeInstall | None,
    model_sets: Sequence[ModelSet] = (),
    seed: int = 0,
    step_wall_limit: float | None = None,
) -> RenodeSession:
    """A session on ``install`` with Jig's models and then ``model_sets`` compiled in."""
    return RenodeSession(
        workdir=workdir,
        install=install,
        model_sets=model_sets,
        seed=seed,
        step_wall_limit=step_wall_limit,
    )
