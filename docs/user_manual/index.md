# User Manual

The User Manual is the reference: every statement of the plan language,
every member of the handles a plan works with, every key of the two
configuration files, every command, and what a run leaves behind. Read the
{doc}`../tutorials/index` first for a guided start.

```{toctree}
:maxdepth: 2

language
library
board_yaml
jig_toml
cli
results
time
bridge
renode_defects
```

## Pages

{doc}`language`
: The `.jig` language: file structure, imports, system declarations,
  statements, expressions and operators.

{doc}`library`
: Built-in functions, profiles and calendar time, and the members of
  boards, consoles, buses and CSP nodes.

{doc}`board_yaml`
: board.yaml schema 2, key by key, with the changes from schema 1.

{doc}`jig_toml`
: The project file: board roots, the library path, Renode, defaults and
  local overrides.

{doc}`cli`
: Every `jig` command and option.

{doc}`results`
: Test outcomes, the console summary, HTML, JUnit and JSON reports, the
  run directory, fingerprints and exit codes.

{doc}`time`
: How Jig keeps simulated time, and how it keeps several boards
  repeatable.

{doc}`bridge`
: The localhost bridge Jig controls Renode through, and what it protects
  against.

{doc}`renode_defects`
: The Renode 1.17.0 model defects Jig replaces or patches, and the tests
  that fail once Renode fixes them.
