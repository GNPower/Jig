# jig.toml reference

A directory that holds `jig.toml` is a Jig project. The file tells Jig
where the project's boards and plan libraries are, which Renode to use,
and what a plan gets when it leaves something out.

## Finding the project

Jig takes the project from `JIG_PROJECT` (the directory or the file
itself), else from the nearest `jig.toml` at or above the plan being run,
else from the nearest one at or above the working directory. Without any,
the working directory is a project with default settings.

## Tables and keys

```toml
[project]
name = "zedboard-example"     # default: the directory's name

[paths]
boards = ["boards"]           # board roots, searched in this order
lib = ["lib"]                 # directories plan imports search

[renode]
path = "../tools/renode"      # a Renode executable or its directory
version = ">=1.17.0,<1.18"    # the Renode versions the project accepts

[defaults]
board = "zed-a"               # the board of a plan that declares no system
options = { pl = "model" }    # board options, for every board that has them
```

Every table and key is optional, and an unknown one is an error. Paths are
relative to the file that names them.

```{list-table}
:header-rows: 1
:widths: 28 72

* - Key
  - Meaning
* - `project.name`
  - The project's name in reports.
* - `paths.boards`
  - Board roots, searched first and in order. Without the key, the
    project's `boards` directory, when there is one.
    `JIG_BOARD_PATH` and the `jig.boards` entry points come after. Each
    entry must be an existing directory.
* - `paths.lib`
  - Directories that `import` searches for paths that are not relative
    (`./`, `../`); each must exist.
* - `renode.path`
  - The Renode to use, which stops discovery from looking further; only
    `--renode` and `JIG_RENODE` come before it
    ({doc}`../getting_started/renode`).
* - `renode.version`
  - A PEP 440 version specifier that narrows the Renode versions Jig
    accepts for this project.
* - `defaults.board`
  - The board a plan without a system runs on, unless `--board` names
    one.
* - `defaults.options`
  - Board options applied to every board in a plan that has them, under
    what the plan and `--option` choose. `jig boot` and
    `jig check --contracts` don't apply them.
```

## Local overrides

A `jig.local.toml` beside `jig.toml` holds one developer's settings, such
as a local checkout of another repository as a board root, or a Renode
build of their own. Keep it out of version control. It merges on top of
`jig.toml`: tables merge key by key, and any other value, lists included,
replaces the one in `jig.toml`.

```toml
[paths]
boards = ["boards", "../other-repo/boards"]

[renode]
path = "C:/tools/renode-1.17.0"
```

## Where runs and caches go

Each run writes a run directory, `runs/<UTC time>-<plan>`, under
`JIG_OUTPUT`, else under the project's `out` directory; reports go where
`--html`, `--junit` and `--json` say. A starting run deletes all but the
newest 20 run directories, sparing any written to in the last 10 minutes;
`JIG_KEEP_RUNS` changes the count, and 0 keeps them all. Renode installs,
downloads and built artifacts go to Jig's per-user cache, which
`JIG_CACHE_DIR` moves ({doc}`../getting_started/renode`).
