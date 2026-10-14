# jig.errors

Every error Jig raises derives from `JigError`, so code that calls Jig can
tell a framework failure from a test verdict. A plugin raises `DslError`
for a plan that called it wrongly and `ToolchainError` for a tool that is
missing or failed; `jig.plugin` re-exports both.

```{eval-rst}
.. automodule:: jig.errors
   :no-index:
```
