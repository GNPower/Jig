# Results, fingerprints and exit codes

Every run produces a verdict per test, a step log in simulated time, a
fingerprint, and a run directory full of raw data. This page describes
each, and the files `jig run` writes on request.

## Outcomes

```{list-table}
:header-rows: 1
:widths: 12 88

* - Outcome
  - When
* - PASS
  - The block ran to its end.
* - FAIL
  - A check was false, `fail` ran, or a step the system had to satisfy
    did not happen: a console pattern, a reply, a connection.
* - ERROR
  - The plan is wrong (an unknown name, a bad argument), or the emulation
    stopped, for example when a machine aborted. An emulation that stops
    also ends the plan.
* - SKIP
  - The test did not run. A skipped test does not fail the run.
```

The setup and the teardown get outcomes too. A setup that does not pass
stops the plan before its first test; a teardown that fails fails the run.

## Steps

Every check, call, wait, log line and record becomes a step, stamped with
the simulated time it finished at, the task it ran in, its kind and its
place in the plan as `file:line:column`. A failing step carries the detail:
for a comparison, the values on both sides.

## The summary on the terminal

`jig run` prints, per plan: the plan's path; one line per board, bus and
node in the system, with each board's options; one line per test with its
outcome and simulated seconds, followed for a test that did not pass by
its failure and the steps before it; and a final line with the counts, the
simulated and wall seconds, and the first 16 hex digits of the
fingerprint. With `--repeat`, it states whether the runs gave one
fingerprint.

## Files on request

`--html PATH`
: One self-contained page per invocation, with every plan run: the system,
  the Renode that ran it, each test's steps, a plot of each numeric series
  a plan recorded, the events counted by source and kind, and relative
  links to the run directory's transcripts and recordings, so the page and
  the run directories can move together as a CI artifact.

`--junit PATH`
: JUnit XML: a test suite per plan run and a test case per test, plus the
  setup and the teardown. Each case carries its wall time, its file and
  line, its simulated seconds as a property, and the step log as its
  output. Each suite carries the run directory, the seed, the fingerprint
  and the Renode details below as properties.

`--json PATH`
: A summary per plan run, for scripts: outcomes, counts, times, the
  fingerprint and the Renode details below.

## The Renode behind a run

Every report records the Renode a run started, because a result depends
on it: the version, the build and the .NET runtime that
`renode --version` printed, whether Jig was tested on that build, how
long Renode took to start, and how long each set of C# models took to
compile, Jig's own first. In the JSON summary these sit under `renode`
(`null` when the plan stopped before Jig found a Renode). The JUnit
properties are `renode_version`, `renode_build`, `renode_runtime`,
`renode_startup_seconds` and `model_set.<name>.compile_seconds`. The HTML
page gives them a line under each plan run's source and seed. The run
directory holds the same record in `renode/session.json`. None of
it enters the fingerprint, which describes the run and not the host.

## The run directory

Each run writes `out/runs/<UTC time>-<plan>/` under the project (or under
`JIG_OUTPUT`): a transcript per console, `<board>/<interface>.log`;
Renode's log; and the recorder's files:

```{list-table}
:header-rows: 1
:widths: 30 70

* - File
  - Contents
* - `events.jsonl`
  - One JSON line per event, with `t`, `source`, `kind` and `detail`:
    boots, power changes, resets, sensor settings, clock settings, link
    changes.
* - `steps.jsonl`
  - Every step of every block, as in the report.
* - `telemetry.csv`
  - Every `record`: `t`, `block`, `name`, `value`.
* - `<bus>.packets.csv`
  - Every CSP packet on the bus: `t`, `src`, `dst`, `sport`, `dport`,
    `priority`, `flags`, `length`, `crc_ok`.
```

The recorder writes each file from its first line on and flushes them
every simulated second, so a long run can be followed while it runs, and
a run that stops part-way leaves its data behind. Storage images live in
the run directory too, which is why Jig keeps only the newest 20 run
directories unless `JIG_KEEP_RUNS` says otherwise.

## Fingerprints

A fingerprint is a SHA-256 digest of what a run decided and measured in
simulated time: each block's outcome and failure text, each step's time,
task, kind, text, outcome and detail, each recorded value with its time,
and the simulated length of the run. Wall time, host paths and anything
else that depends on the host stay out of it, and paths inside failure
details are written relative to the project before they are hashed. Two
runs of one plan on the same firmware and inputs have the same
fingerprint, on any host; `jig run --repeat N` checks that.

## Exit codes

`jig run` exits with 0 when every test passed and repeated runs matched,
1 when a test or a teardown failed or repeated runs differed, 2 for a
usage or configuration error or a missing file, 3 when Renode, a model
compile or a firmware build failed, and 130 when interrupted. {doc}`cli`
lists them with the other commands.
