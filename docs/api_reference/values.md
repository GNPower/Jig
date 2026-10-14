# Values

Plans and plugin code exchange plain Python values: numbers, text, bytes,
lists and mappings. Three kinds of value need classes of their own:
records, regular expressions and profiles. `jig.plugin` re-exports all
three.

The modules that define them are internal: import the classes from
`jig.plugin`.

## Records and regular expressions

```{eval-rst}
.. autoclass:: jig.lang.values.Record
   :members: plain
   :no-index:

.. autoclass:: jig.lang.values.Regex
   :members: search
   :no-index:
```

## Profiles

```{eval-rst}
.. autoclass:: jig.runtime.environment.Profile
   :members: at
   :no-index:
```
