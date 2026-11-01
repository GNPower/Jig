# Renode sessions and discovery

A device family's driver talks to Renode through a session: one Renode process per
plan run, reached through the localhost bridge
({doc}`../user_manual/bridge`). Discovery finds the Renode a session runs,
and model sets carry the C# models each session compiles.

## Sessions

`jig.plugin` re-exports `RenodeSession`; import it from there.

```{eval-rst}
.. autoclass:: jig.renode.session.RenodeSession
   :members:
   :no-index:
```

## Model sets

```{eval-rst}
.. autoclass:: jig.renode.models.ModelSet
   :members:
```

## Discovery

```{eval-rst}
.. automodule:: jig.renode.discover
```
