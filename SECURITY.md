# Security policy

## Supported versions

Jig is in its 0.x stage. Security fixes are put into the latest minor release
and into `main`. Older releases get no security patches.

| Version         | Fixes |
| --------------- | ----- |
| latest 0.x      | yes   |
| older 0.x       | no    |

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open the repository's
**Security** tab and choose **Report a vulnerability**, or go to
<https://github.com/GNPower/Jig/security/advisories/new>. Please don't
open a public issue for one.

Include the Jig, Renode, Python and OS versions, what an attacker needs
(network access, a local account, a file they can write), and the steps or
a plan that shows the problem. We will provide a response in the advisory post.

Problems in Renode itself belong to the Renode project:
<https://github.com/renode/renode>.

## Threat model

Jig runs plans, board definitions, plugins, C# models and firmware that
its user chooses, so it treats all of them as code the user trusts, as a
compiler treats its sources. A plan can send any Renode monitor command,
execute any Python that Jig imports, and board definitions name build
commands that run on the host. Running a plan from an untrusted repository
is the same as running a program from it.

What Jig does control:

- **Control of Renode.** Jig talks to Renode through a bridge that listens
  on 127.0.0.1 only, on a port the operating system picks, and accepts one
  client that presents a 256-bit token made for that run. The token file
  in the run directory is readable by its owner only on Linux and macOS.
  Another machine can't reach the bridge, and another local user can't
  use it without reading that file. Jig doesn't start Renode's Robot
  Framework server, which listens beyond the loopback interface.
- **Renode downloads.** `jig renode install` downloads only from Renode's
  GitHub releases, after a prompt or `--yes`, and checks the archive's
  size and SHA-256 digest against a table in Jig's source before
  unpacking it. It rejects archive members that would unzip outside the
  install directory.

A report that explpoits one of these controls is in scope.

The full model, with what each kind of local user can do, is in the
documentation: <https://jig.readthedocs.io/en/latest/user_manual/bridge.html>.
