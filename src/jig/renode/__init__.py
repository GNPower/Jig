"""Renode: discovery and installation, the process and its loopback bridge,
sessions in simulated time, and the C# model sets Renode compiles.

* ``jig.renode.discover``: ``find_renode``, ``RenodeConfig``, ``RenodeInstall``;
* ``jig.renode.install``: managed installs (``jig renode install``);
* ``jig.renode.process`` and ``jig.renode.bridge``: a headless Renode and the
  client of its bridge;
* ``jig.renode.session``: ``RenodeSession``;
* ``jig.renode.models``: ``ModelSet``, compile diagnostics, SDK projects;
* ``jig.renode.commands``: ``jig doctor``, ``jig renode``, ``jig models``,
  ``jig cosim``.
"""

from jig.renode.discover import RenodeConfig, RenodeInstall, find_renode
from jig.renode.models import ModelSet

__all__ = ["ModelSet", "RenodeConfig", "RenodeInstall", "find_renode"]
