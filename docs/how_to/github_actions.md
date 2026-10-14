# Run plans in GitHub Actions

This guide adds a workflow to a repository that holds a Jig project: on
every push it installs Jig and Renode, builds the firmware, runs the plans
and keeps the reports, whether the plans pass or not.

## Pin Jig

Pin the version in a requirements file at the project root, so every run
and every developer uses one release:

```text
jig-dsl==0.1.0
```

Jig's plugin API follows semantic versioning, so a board repository can
also accept patch releases with `jig-dsl>=0.1,<0.2`.

## The workflow

Save this as `.github/workflows/simulation.yml`:

```yaml
name: Simulation

on:
  push:
  pull_request:

jobs:
  plans:
    runs-on: ubuntu-24.04
    env:
      JIG_CACHE_DIR: ${{ github.workspace }}/.jig-cache
    steps:
      - uses: actions/checkout@v7

      - uses: actions/setup-python@v7
        with:
          python-version: "3.13"
          cache: pip

      - name: Install Jig
        run: python -m pip install -r requirements.txt

      - name: Restore Renode
        uses: actions/cache@v6
        with:
          path: .jig-cache/renode
          key: renode-1.17.0-${{ runner.os }}-${{ runner.arch }}

      - name: Install Renode
        run: jig renode install 1.17.0 --yes

      - name: Check the setup
        run: jig doctor

      - name: Build the firmware
        run: |
          sudo apt-get update
          sudo apt-get install -y gcc-arm-none-eabi
          python firmware/build.py

      - name: Run the plans
        run: >-
          jig run plans/*.jig
          --junit out/junit.xml --html out/report.html --json out/summary.json

      - name: Keep the reports
        if: always()
        uses: actions/upload-artifact@v7
        with:
          name: jig-reports
          path: out/
```

`JIG_CACHE_DIR` puts Jig's cache inside the workspace, where
`actions/cache` can save the Renode install between runs; the key names the
Renode version, so changing the version installs the new one. On a cache
hit, `jig renode install` finds the install, whose marker file records the
SHA-256 digest the archive had, compares that with its pinned table and
skips the download; it does not hash the unpacked files again. Add
`.jig-cache/` to the repository's `.gitignore`. `if: always()` keeps the
reports and the run directories under `out/` when a plan fails, which is
when you need them.

## Read the result

`jig run` exits with 1 when a test failed; with 2 when a plan, a board
definition or a file one names is wrong or missing, such as an ELF the
build step did not write; and with 3 when Renode, a model compile or a
firmware build that Jig runs itself failed. The step's log tells broken
firmware from a broken runner. The JUnit file has one case per test, plus
the setup and the teardown, each with its wall time, its file and line, and
its simulated seconds as a property; each plan's suite also names the
Renode version, build and runtime and each model set's compile time. The
HTML report in the artifact links each test's steps to the console
transcripts beside it.

## Keep long plans off the push path

A plan that simulates minutes of board time can take longer than a push
deserves. Tag those tests and split the runs:

```bash
jig run plans/*.jig --tags smoke
jig run plans/*.jig --tags soak --repeat 2
```

Run the first on every push and the second on a `schedule:` trigger.
`--repeat 2` on the long run also checks that each plan gives the same
fingerprint twice.

## Run the workflow locally

[act](https://nektosact.com) runs Linux jobs like this one in a container
on your machine. Version 0.2.81 or newer is needed for the `@v7` actions,
which run on Node 24:

```bash
act push -W .github/workflows/simulation.yml
```
