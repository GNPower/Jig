# SoC drivers

An SoC driver turns a board definition into a booted Renode machine for
one SoC family. Jig ships the `zynq7000` and `stm32` drivers; a board
names another in board.yaml (`soc_driver: module:Class`), registers one
from its plugin, or installs one under the `jig.socs` entry-point group.
{doc}`../how_to/add_a_board` describes when a board needs one.

The module is internal: driver authors read its types here and import
them from `jig.plugin`.

```{eval-rst}
.. automodule:: jig.system.socs
   :no-index:
```
