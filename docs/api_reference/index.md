# API Reference

Most users never write Python for Jig: plans are `.jig` files, and boards
are YAML. Python enters when a board needs a plugin, a test double or a
driver for a new SoC family. This section documents the modules those
authors meet; the plan language is in the {doc}`../user_manual/index`.

**Public surface.** Board plugins import only `jig.plugin`, whose API
follows semantic versioning from 0.1.0 with its own version number,
currently 1. `jig.testing` is public for tests. Every other module is
internal: it can change in any release, and the pages here document it
only where a plugin or driver author needs to read its types.

```{toctree}
:maxdepth: 1

plugin
testing
values
errors
socs
renode
contracts
```

## Pages

{doc}`plugin`
: `jig.plugin`: what a board plugin's `register(api)` receives, and the
  types it re-exports.

{doc}`testing`
: `jig.testing`: a fake clock, a fake CSP board behind a KISS link, and a
  runner for plans without a system, for tests that need no Renode.

{doc}`values`
: The values plans pass to plugin code: records, regular expressions and
  profiles.

{doc}`errors`
: The exception hierarchy under `JigError`.

{doc}`socs`
: The SoC driver protocol and the per-machine configuration a driver
  receives.

{doc}`renode`
: The Renode session a driver talks to, model sets and Renode discovery.

{doc}`contracts`
: `jig.contracts`: checking that board peripherals implement the C#
  contracts their board.yaml keys need.
