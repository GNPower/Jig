# jig.contracts

Board IC models implement C# interfaces that Jig defines, so that plans can
set a converter's inputs, attach a flash image, power-cycle a chip or set
a real-time clock ({doc}`../how_to/write_a_model`). This module checks a
board against them before a plan needs them.

`jig check --contracts` calls `verify` on each board.

```{eval-rst}
.. automodule:: jig.contracts
```
