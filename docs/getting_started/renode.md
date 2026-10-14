# Renode setup

Jig drives [Renode](https://renode.io), Antmicro's open-source emulator,
as a separate process. It doesn't bundle Renode: it finds an installed copy,
or downloads a release into its cache once you agree to it.

## Supported versions

Jig accepts Renode `>=1.17.0,<1.18` and is tested on the 1.17.0 release
builds; another 1.17 build runs with a warning. Renode 1.16 compiles C#
models with Mono's compiler, and 1.17.0 removed members that older models
called. Jig checks the version before a run starts, so a mismatch fails
there instead of part-way through a plan. `--allow-untested-renode` lets
a run use another version anyway, and `version` in the `[renode]` table of
`jig.toml` narrows the range for a project.

Renode 1.17.0 ships portable packages for Windows on x64, Linux on x64 and
arm64, and macOS on Apple silicon. It has no package for Intel Macs.

## Install Renode with Jig

```bash
jig renode install
```

Without a version the command installs the one Jig is tested on, for the
host it runs on. Before it downloads anything it shows the URL, the size,
the destination and the license terms, and waits for you to agree. The
download comes only from Renode's GitHub releases
(`https://github.com/renode/renode/releases/download/v<version>/<asset>`),
and Jig checks its byte count and SHA-256 digest against a table in its own
source before unpacking it.

```{list-table}
:header-rows: 1
:widths: 30 70

* - Option
  - Effect
* - `VERSION`
  - A version from Jig's table, such as `1.17.0`.
* - `--platform P`
  - Install for another platform: `win-x64`, `linux-x64`, `linux-arm64`
    or `osx-arm64`.
* - `--yes`
  - Agree without a prompt. A run with no terminal needs it, or
    `JIG_ASSUME_YES` set to `1`, `yes` or `true`.
* - `--force`
  - Reinstall over an existing copy.
* - `--from FILE`
  - Install from an archive already on disk, for a machine without
    internet access. Jig checks it against the same table.
```

Installs go to `renode/<version>-<platform>` in Jig's cache directory:
`~/.cache/jig` on Linux, `~/Library/Caches/jig` on macOS and
`%LOCALAPPDATA%\jig\Cache` on Windows. `JIG_CACHE_DIR` moves the cache.
The downloaded archive stays in `renode/downloads` beside the install.
`jig renode list` shows the managed installs, `jig renode path` prints the
one Jig would use, and `jig renode remove` deletes one with its archive.

Renode is MIT-licensed, and its packages include components under the LGPL:
tlib under LGPL-2.1, NetMQ and Packet.Net under LGPL-3.0. Each install keeps
the license files Renode ships.

## How Jig finds Renode

Jig looks in these places, in order:

1. the `--renode PATH` option;
2. the `JIG_RENODE` environment variable;
3. `path` in the `[renode]` table of the project's `jig.toml`
   ({doc}`../user_manual/jig_toml`);
4. `path` in the `[renode]` table of the user configuration file,
   `config.toml` in Jig's configuration directory: `~/.config/jig` on
   Linux, `~/Library/Application Support/jig` on macOS and
   `%LOCALAPPDATA%\jig` on Windows (`JIG_CONFIG_DIR` moves it);
5. the installs `jig renode install` made, newest first;
6. every `renode` on `PATH`, in `PATH` order;
7. the standard install locations of Renode's own packages:
   `%ProgramFiles%\Renode` on Windows, `/Applications/Renode.app` (or the
   same under your home directory) on macOS and `/opt/renode` on Linux;
8. the `RENODE_ROOT` environment variable.

The first four name Renode explicitly: Jig uses the first of them that is
set, and if its path is missing or holds an unsupported version, Jig stops
with an error instead of looking further. From the fifth on, Jig takes the
first install whose `renode --version` falls in the supported range, or in
the range `version` in `jig.toml` narrows it to. A path can name the
executable or the directory that holds it. `jig doctor` lists every source
with what it found.

## Check the setup

`jig doctor` checks everything a run needs and exits non-zero if any check
fails:

```bash
jig doctor
```

It prints the Python and Jig versions and Jig's cache paths, every
discovery source with what it found and which one won, and the Renode
version. Then it starts Renode without a window, brings up the bridge Jig
controls it through, compiles Jig's C# models in it, and reports the time
each step took. `jig doctor --json` writes the same report as JSON for CI
logs.
