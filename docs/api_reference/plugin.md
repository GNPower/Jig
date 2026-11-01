# jig.plugin

The module board plugins import. A plugin is a `.py` file or a package
that a board file names with its `plugin` setting; Jig imports it once per
process and calls its `register(api)` function with the `PluginApi`
documented here.
{doc}`../how_to/write_a_plugin` shows a complete plugin.

Every name below is importable from `jig.plugin`, including the types it
re-exports from Jig's internal modules: values ({doc}`values`), errors
({doc}`errors`), the device family driver protocol ({doc}`socs`), the
Renode session ({doc}`renode`), and the board, node, link and console
handles plan code passes to plugin members.

```{eval-rst}
.. automodule:: jig.plugin
```
