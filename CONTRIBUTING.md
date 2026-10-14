# Contributing to Jig

Bug reports, fixes, board examples and documentation are welcome. The
requirements for pull requests to this repo are listed here.

## Development setup

### 1. Clone and create a virtual environment

Jig needs Python 3.11 or newer.

```bash
git clone https://github.com/GNPower/Jig.git
cd Jig
python -m venv .venv
```

Activate it with `. .venv/bin/activate` on Linux and macOS, or
`.venv\Scripts\Activate.ps1` in PowerShell on Windows.

### 2. Install Jig in editable mode

```bash
python -m pip install -e ".[dev,docs]"
```

The `dev` addon installs the CI tools: pytest, ruff, PyMarkdown, mypy,
nox, build, twine and bump-my-version. `docs` installs Sphinx, the Read the
Docs theme and MyST.

### 3. Set up Renode

Unit tests run without Renode. Tests marked `renode` need Renode 1.17.0:

```bash
jig renode install
jig doctor
```

Or point `JIG_RENODE` at an existing Renode executable or its directory.

## Running checks locally

Every CI job runs one [nox](https://nox.thea.codes) session from
`noxfile.py`, so the following command checks everything works:

```bash
nox                  # lint, typecheck, tests on each Python found, docs, hygiene
nox -s tests-3.13    # unit tests on Python, with coverage
nox -s renode        # the tests that need Renode
nox -s dist          # build, twine check, and the wheel from a clean venv
```

- `lint`: `ruff check`, `ruff format --check`, PyMarkdown with the rules
  in `.markdownlint.json` (Markdown lines stay within 80 columns), and
  zizmor's security audit of the workflows.
- `typecheck`: `mypy --strict` over the package, the model tests,
  `noxfile.py` and `docs/conf.py`.
- `tests-3.11` to `tests-3.14`: the unit tests with line and branch
  coverage, which must stay at or above the floor in `noxfile.py`.
- `renode`: `jig doctor`, `jig models check` (and `--sdk` when `dotnet`
  is installed), then every test marked `renode` or `slow`.
- `cosim`: the co-simulation test, on Linux with Verilator, `make` and
  `g++`.
- `docs`: `sphinx-build -W --keep-going -E`
- `dist`: the sdist and wheel, `twine check --strict`, and the wheel
  installed in a new venv outside the checkout, where it runs
  `jig --version`, `jig doctor --json` and a plan on Renode.
- `hygiene`: the test that no file names a private project.
- `audit`: pip-audit over the runtime dependencies.

The Renode sessions use `JIG_RENODE`, or the Renode that discovery finds.
If neither are available, they run `jig renode install`, which asks before it
downloads Renode. `pytest` on its own leaves out the tests marked `renode`,
`firmware` and `slow`. Without network access, build the documentation
with `JIG_DOCS_OFFLINE=1`, which drops the links into Python's
documentation instead of fetching their index.

## Project layout

```text
/
├── src/jig/              the package
│   ├── lang/             grammar, parser, interpreter, built-in functions
│   ├── runtime/          scheduler, profiles, recorder
│   ├── system/           board definitions, simulations, device drivers
│   ├── renode/           discovery, installer, session, model compile
│   ├── ext/              CSP, Zynq-7000, airhdl and co-simulation extensions
│   ├── models/           C# models Renode compiles
│   ├── platforms/        Renode platform descriptions Jig ships
│   ├── report/           HTML and JUnit writers
│   └── testing/          test doubles
├── tests/                unit tests and Renode tests
├── docs/                 this documentation
└── examples/zedboard/    the public example
```

## Pull request process

1. Fork the repository and branch from `main`.
2. Add tests for what you change, a fix must come with a test that fails
   without it.
3. Run the checks above.
4. Add a line under `[Unreleased]` in `CHANGELOG.md`.
5. Open a pull request against `main`, CI must pass before review.

## Signed commits

Every commit on `main` must carry a verified signature. Configure git to
sign with your GPG key:

```bash
git config user.signingkey <KEY-ID>
git config commit.gpgsign true
```

and upload the public key to your GitHub account so GitHub can verify the
signatures.

## Releases

Jig follows semantic versioning. The version lives in `pyproject.toml`.
`jig --version` and the documentation read it from the installed
package. bump-my-version changes it without committing or tagging:

```bash
bump-my-version bump pre_l      # 0.1.0.dev0 -> 0.1.0a1, 0.1.0a2 -> 0.1.0b1
bump-my-version bump pre_n      # 0.1.0a1 -> 0.1.0a2
bump-my-version bump patch      # 0.1.0 -> 0.1.1.dev1
bump-my-version bump --new-version 0.2.0   # any other step
```

A release then takes these steps:

1. Move the `[Unreleased]` entries in `CHANGELOG.md` under
   `## [X.Y.Z] - YYYY-MM-DD`, and commit the version and the changelog,
   signed, through a pull request.
2. Rehearse on TestPyPI: run the Release workflow by hand (Actions,
   Release, Run workflow). It publishes a development release of the
   version, `0.1.0a1.dev7` for `0.1.0a1` on run 7, and installs it back
   from TestPyPI.
3. Sign an annotated tag `vX.Y.Z` with the message `Jig X.Y.Z`, push it,
   and publish a GitHub release from it with the changelog entry as its
   notes. Mark it a pre-release for `a`, `b` and `rc` versions.
4. The Release workflow checks that the tag names the version, builds and
   checks the distribution, publishes a pre-release to TestPyPI first,
   publishes to PyPI with attestations, and installs the result back from
   the index.

## Running workflows locally with act

[act](https://nektosact.com) runs the Linux jobs of `.github/workflows` in
containers. Jig's workflows use actions that run on Node 24, which act
supports from version 0.2.81. act copies the working tree into the
container, uncommitted and untracked files included:

```bash
git clone -c core.autocrlf=input https://github.com/GNPower/Jig.git jig-act
cd jig-act
```

Then run one job at a time, with an event file holding
`{"act": true}` so that jobs which only work on GitHub skip
themselves:

```bash
echo '{"act": true}' > /tmp/act-event.json
act pull_request -W .github/workflows/ci.yml -j lint \
  -P ubuntu-24.04=ghcr.io/catthehacker/ubuntu:act-24.04 \
  --container-daemon-socket - -e /tmp/act-event.json
```

## Code style

- Python: ruff's rules and formatter at 100 columns, type hints that pass
  `mypy --strict`, and docstrings that say what a function does and what
  its numbers mean, with units.
- C# models: a header that names the part and cites the sections of its
  public documentation each behaviour comes from.
- Markdown: lines up to 80 columns, checked by `nox -s lint` with the
  rules in `.markdownlint.json`, which markdownlint in an editor reads
  too.

## Reporting bugs

Open an issue with the bug form. It asks for the Jig, Renode, Python and
OS versions and the plan that shows the problem. Report security
problems privately, as
[SECURITY.md](https://github.com/GNPower/Jig/blob/main/SECURITY.md)
describes.
