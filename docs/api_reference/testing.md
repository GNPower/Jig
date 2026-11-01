# jig.testing

Test doubles for code built on Jig, for tests that run without Renode or
firmware: a clock the scheduler advances, a CSP board behind an in-process
KISS link, a simulation of fake machines and CAN links, and a runner for
plans. Board repositories use them to test plugins and `.jig` procedure
libraries in their own CI.

```python
from pathlib import Path

from jig.testing import run_source


def test_procedure(tmp_path: Path) -> None:
    report = run_source(
        tmp_path,
        'test "sum" {\n    check 1 + 1 == 2\n}\n',
    )
    assert [case.status for case in report.cases] == ["PASS"]
```

A plan with a system runs on a `FakeSimulation`, whose CAN links are
`FakeCanPort`s in `can_buses`. A fake CAN link acknowledges every frame a
harness node writes, keeps it in `written`, and hands it to the link's bus
observers and nodes at the next scheduler service, as a run's bus does;
`feed(frames)` delivers frames as a board sends them. This test sends a
packet from a CSP node on the CAN link of a board whose device runs
`protocol csp(...)`:

```python
from pathlib import Path

from jig.testing import FakeSimulation, run_source


def test_a_node_sends_on_the_can_link(tmp_path: Path) -> None:
    sim = FakeSimulation(workdir=tmp_path / "out")
    report = run_source(
        tmp_path,
        """
        system {
            board dut: "my-board"
            node gs: csp(address: 10) over cfp
            link can0: can(members: [dut.can, gs])
        }
        test "send" {
            gs.send(dut, 20, x"01")
            wait 10 ms
            check len(can0.packets(src: 10, port: 20)) == 1
        }
        """,
        simulation=sim,
    )
    assert [case.status for case in report.cases] == ["PASS"]
    assert {frame.sender for frame in sim.can_buses["can0"].written} == {"harness"}
```

```{eval-rst}
.. automodule:: jig.testing
```
