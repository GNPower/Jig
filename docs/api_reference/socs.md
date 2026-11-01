# Device families

A device family's driver turns a board file's device into a booted Renode
machine: it checks the device's arguments, writes the family's part of the
platform description, and boots, resets and power-cycles the machine. Jig
ships the `zynq7000` and `stm32` families under the `jig.device_families`
entry-point group, where other distributions can add theirs; a board's
plugin registers one with `api.device_family`, and a device can name its
own driver with `driver: "module:Class"`.
{doc}`../how_to/add_a_board` describes when a board needs one.

The module is internal: driver authors read its types here and import
them from `jig.plugin`.

```{eval-rst}
.. automodule:: jig.system.socs
   :no-index:
```
