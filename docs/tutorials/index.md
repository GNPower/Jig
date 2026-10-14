# Tutorials

The tutorials build plans for the ZedBoard example, from one board on its
own to two boards on a bus with a host computer, then to plans that run
work on a schedule. Each one needs the example's firmware built
({doc}`../getting_started/first_run`), and the plan each one builds is in
the repository under `examples/zedboard/plans/`, so you can run the
finished version next to your own.

**Recommended order.** Read them in sequence: each tutorial uses the plan
and the terms of the one before.

```{list-table}
:header-rows: 1
:widths: 5 25 20 50

* - #
  - Title
  - Plan
  - What you will learn
* - 1
  - {doc}`one_zedboard`
  - `one_zedboard.jig`
  - System declarations, setup and test blocks, console commands and
    expectations, procedures, checks, a CSP node on a serial link, reading
    the report
* - 2
  - {doc}`two_zedboards`
  - `two_zedboards.jig`
  - Two boards on a CAN bus, imported procedures, parallel tasks, routed
    CSP traffic, the bus observer, decoded status records, `try` and
    `catch`
* - 3
  - {doc}`profiles_timelines`
  - `profiles_timelines.jig`
  - `every` loops, recorded values and their plots, profiles, timelines,
    cutting and restoring a link
```

`examples/zedboard/plans/multiboard.jig`, the plan
{doc}`../getting_started/first_run` runs, uses the system of tutorial 2
and goes further: it checks the boards' frame counters against what the
bus carried, pings across a link outage, and power-cycles zed-b.

```{toctree}
:hidden:

one_zedboard
two_zedboards
profiles_timelines
```
