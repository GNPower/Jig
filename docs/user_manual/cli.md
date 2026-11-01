# Command line

`jig` is the one command Jig installs; `python -m jig` runs the same
program. Every subcommand takes `--help`.

## Global options

```{list-table}
:header-rows: 1
:widths: 30 70

* - Option
  - Effect
* - `--version`
  - Print the distribution and version, `jig-dsl 0.1.0`.
* - `-v`, `-vv`
  - `-v` prints each plan step as it runs; `-vv` adds Jig's debug log,
    including the monitor commands it sends to Renode.
* - `-q`
  - Print warnings and errors only.
```

`-v`, `-vv` and `-q` go before or after the subcommand:
`jig -v run plan.jig` and `jig run plan.jig -v` do the same. `--version`
goes before it.

## jig run

```text
jig run PLAN [PLAN ...] [options]
```

Runs each plan in its own emulation and prints a summary per plan.

```{list-table}
:header-rows: 1
:widths: 32 68

* - Option
  - Effect
* - `--board NAME`
  - The board for plans that declare no system.
* - `--boot MODE`
  - Boot mode for every board, over the plan's.
* - `--mips N`
  - Virtual CPU speed for every board.
* - `--option NAME=VALUE`
  - A board option for every board that has it, over the plan's choice;
    repeatable.
* - `-k TEXT`
  - Run only tests whose name contains `TEXT`; repeatable.
* - `--tags TAG[,TAG]`
  - Run only tests with one of the tags; repeatable.
* - `--seed N`
  - The emulation's random seed; 0 by default.
* - `--repeat N`
  - Run each plan `N` times and fail unless every run has the same
    fingerprint.
* - `--html PATH`, `--junit PATH`, `--json PATH`
  - Write an HTML report, JUnit XML or a JSON summary
    ({doc}`results`).
* - `--renode PATH`, `--allow-untested-renode`
  - Use this Renode; accept a version outside the supported range
    ({ref}`renode-options`).
```

## jig list

```text
jig list PLAN [PLAN ...]
```

Prints each plan's tests and their tags without starting Renode.

## jig boot

```text
jig boot BOARD [--mode MODE] [--seconds S] [--mips N]
         [--option NAME=VALUE] [--storage NAME=IMAGE]
         [--renode PATH] [--allow-untested-renode]
```

Builds one board, boots it, copies its console to the terminal for `S`
simulated seconds (5 by default), and prints how fast the emulation ran
against real time. `--storage` starts a storage device from an image
file.

## jig check and jig boards

```text
jig check [BOARD ...] [--option NAME=VALUE] [--contracts]
          [--renode PATH] [--allow-untested-renode]
jig boards
```

`jig check` validates board files, all of them by default, without
Renode: each board file loads, its device's family accepts the device's
arguments, its firmware and XSA files exist, its register maps parse,
every protocol stack fits the interface below it ("csp needs packets and
uart0 gives a byte stream: put kiss between them"), its housekeeping
codecs are registered, and Jig can write its platform description. No
device, peripheral or link may have the name of a board member. Each
board is checked with its options' defaults; `--option NAME=VALUE` sets
an option for every board that has it, and repeats. With `--contracts` it
also builds each board's machine in Renode, without booting it, and
checks that each peripheral a board file setting names implements the C#
contract that setting needs ({doc}`../how_to/write_a_model`). It exits 1
when a board has a problem. `jig boards` lists every board Jig can see
and the root it came from, and under each its description and its options
with their values and defaults.

## jig platform

```text
jig platform BOARD [--option NAME=VALUE] [-o FILE]
```

Prints the Renode platform description Jig writes for the board's device
from its board file ({doc}`board_files`), with the board's option
defaults or the values `--option` gives, or writes it to `FILE` as UTF-8.
`workdir` reads `<workdir>`, since no run builds the machine. A board
author reads it to see what Renode loads; a Renode script can load the
written file to run the board without Jig, as the ZedBoard example's
`renode/two-boards.resc` does.

## jig doctor

```text
jig doctor [--json] [--board BOARD ...]
           [--renode PATH] [--allow-untested-renode]
```

Checks the setup end to end: Python and Jig versions, cache paths, each
Renode discovery source and the winner, the Renode version, a headless
start with the bridge up, a compile of Jig's C# models and of each
`--board`'s models, the .NET SDK (needed only for
`jig models check --sdk`), and free disk space. `--board` takes a board
directory or a board name and repeats. Exits 1 if any check fails.
`--json` prints the result as JSON.

## jig renode

```text
jig renode install [VERSION] [--platform P] [--yes] [--force]
                   [--from FILE]
jig renode list [--json]
jig renode path [--renode PATH] [--allow-untested-renode]
jig renode remove VERSION [--platform P]
```

Installs, lists, locates and removes the Renode copies Jig manages
({doc}`../getting_started/renode`). `path` prints the executable Jig
would run on standard output, and its version and discovery source on
standard error; it exits 3 when it finds none. `remove` deletes the
install and its downloaded archive, and exits 1 when that version is not
installed.

## jig models

```text
jig models check [--board BOARD ...] [--sdk]
                 [--renode PATH] [--allow-untested-renode]
jig models ide [--board BOARD] [-o FILE]
               [--renode PATH] [--allow-untested-renode]
```

`check` compiles Jig's C# models and then each `--board`'s model set (a
board directory or name, repeatable) in a throwaway Renode, and prints
each set's compile time. With `--sdk` it builds them with the .NET SDK
instead, warnings as errors, against the Renode assemblies Jig found. It
exits 1 on a compile error; `jig run` exits 3 for the same error.

`ide` writes a C# project for an editor: by default
`<model root>/<Name>.Models.csproj` (`-o` names another file), linking
the board's model sources and Jig's, with references to the Renode
assemblies Jig found. Its build output goes to `models/ide` in Jig's cache
directory.

## jig cosim

```text
jig cosim build OUTPUT --top TOP --sources FILE [options]
```

Builds a Verilated RTL model into a library Renode loads
({doc}`../how_to/cosimulate_rtl`). Linux only.

(renode-options)=

## Choosing Renode

Every command that starts or finds Renode (`run`, `boot`, `check`,
`doctor`, `renode path`, `models` and `cosim`) takes two options:

```{list-table}
:header-rows: 1
:widths: 32 68

* - Option
  - Effect
* - `--renode PATH`
  - Use the Renode at `PATH`, an executable or the directory holding it,
    instead of the discovery order {doc}`../getting_started/renode`
    describes. Jig stops if it is missing or outside the supported range.
* - `--allow-untested-renode`
  - Accept a Renode outside the supported range, `>=1.17.0,<1.18`. A
    `version` requirement in `jig.toml` still applies.
```

`jig renode install`, `list` and `remove` act on Jig's own installs and
take neither option.

## Environment variables

```{list-table}
:header-rows: 1
:widths: 30 70

* - Variable
  - Effect
* - `JIG_PROJECT`
  - The project directory or its `jig.toml`.
* - `JIG_BOARD_PATH`
  - Extra board roots, separated by the platform's path separator.
* - `JIG_OUTPUT`
  - Where run directories go, instead of the project's `out`.
* - `JIG_KEEP_RUNS`
  - How many run directories to keep; 20 by default, and 0 keeps all.
    A starting run deletes the oldest beyond that, except any written to
    in the last 10 minutes.
* - `JIG_CACHE_DIR`
  - Jig's cache: Renode installs, downloads, built artifacts.
* - `JIG_CONFIG_DIR`
  - The directory holding the user configuration file, `config.toml`,
    instead of Jig's configuration directory
    ({doc}`../getting_started/renode`).
* - `JIG_RENODE`
  - The Renode executable or its directory: the second place discovery
    looks, after `--renode`.
* - `RENODE_ROOT`
  - A Renode installation directory: the last place discovery looks.
* - `JIG_ASSUME_YES`
  - `1`, `yes` or `true` answers yes to `jig renode install`'s prompt.
* - `ARM_NONE_EABI_GCC`
  - The compiler for STM32 firmware Jig builds from source, instead of
    `arm-none-eabi-gcc` on `PATH`.
* - `RENODE_INTEGRATION_LIBRARY`
  - Renode's IntegrationLibrary sources for `jig cosim build`, instead of
    `plugins/IntegrationLibrary` in the Renode install.
```

## Exit codes

```{list-table}
:header-rows: 1
:widths: 15 85

* - Code
  - Meaning
* - 0
  - Every selected test passed, and every repeated run matched.
* - 1
  - A test failed, a teardown failed, or repeated runs differed.
* - 2
  - A usage or configuration error: a bad option, a plan that does not
    parse, a board file or `jig.toml` that is wrong, or a file a
    board needs that is missing, which Jig finds before Renode starts.
* - 3
  - An infrastructure failure: Renode missing or failing, a model that
    does not compile, a firmware build that fails.
* - 130
  - Interrupted with Ctrl+C.
```

`jig doctor`, `jig check` and `jig models check` exit with 1 when a check
finds a problem. A download nobody agreed to, at the prompt, with `--yes`
or through `JIG_ASSUME_YES`, counts as a usage error (2).
