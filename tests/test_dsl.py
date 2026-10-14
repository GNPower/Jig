"""The plan language on a fake clock: parsing, evaluation, control flow, reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jig.lang import Interpreter, PlanError, RunOptions, load
from jig.lang.values import parse_quantity
from jig.report.report import SuiteReport
from jig.testing import FakeSimulation, run_source


def run(tmp_path: Path, source: str, **files: str) -> SuiteReport:
    return run_source(tmp_path, source, files=files)


def statuses(report: SuiteReport) -> dict[str, str]:
    return {case.name: case.status for case in report.cases}


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("5", 5),
        ("100 ms", 0.1),
        ("2 s", 2),
        ("1 Mbit/s", 1_000_000),
        ("1.5 V", 1.5),
        ("0x1F", 31),
        ("0b101", 5),
        ("1_000", 1000),
        ("3 min", 180),
        ("2 KiB", 2048),
        ("250 us", 0.00025),
    ],
)
def test_quantities(text: str, value: float) -> None:
    assert parse_quantity(text) == pytest.approx(value)


def test_expressions_and_values(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "arithmetic" {
            check 1 + 2 * 3 == 7
            check (1 + 2) * 3 == 9
            check 7 / 2 == 3.5
            check 7 % 4 == 3
            check -(2 - 5) == 3
            check 100 ms + 0.9 s == 1 s
        }
        test "text" {
            let name = "NODE"
            check "id {name}-{1 + 2}" == "id NODE-3"
            check "{3.14159:.2f}" == "3.14"
            check "literal {{braces}}" == "literal \\{braces\\}"
            check "abc" + "def" == "abcdef"
            check "pump state IDLE" contains "IDLE"
            check "Pump State: IDLE (0x3)" matches /State: (\\w+)/
            check "x".upper() == "X"
        }
        test "collections" {
            let items = [1, 2, 3]
            items.append(4)
            check len(items) == 4
            check 3 in items and 9 not in items
            check items[0] == 1
            let table = {a: 1, "b c": 2}
            check table.a == 1 and table["b c"] == 2
            check 5 in 1 .. 5 and not (6 in 1 .. 5)
            check x"01 0A ff" == from_hex("010aff")
            check len(random_bytes(16, seed: 3)) == 16
        }
        test "regex captures" {
            let found = match(/count: (\\d+)/, "frame count: 42")
            check found.number(1) == 42
            check found[1] == "42"
            check match(/absent/, "text") == none
        }
    """,
    )
    assert statuses(report) == {name: "PASS" for name in statuses(report)}, [
        c.detail for c in report.cases
    ]


def test_a_failing_check_reports_both_sides(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "compare" {
            let code = 1550
            check code == 1552, "supply_mv"
        }
    """,
    )
    case = report.cases[0]
    assert case.status == "FAIL"
    assert "plan.jig:4:13" in case.detail
    assert "1550 == 1552" in case.detail
    assert "(supply_mv)" in case.detail
    assert case.steps[-1].outcome == "fail"


def test_procedures_imports_and_defaults(tmp_path: Path) -> None:
    library = """
        const OFFSET = 10
        procedure add(a, b = OFFSET) { return a + b }
        procedure twice(x) { return add(x, b: x) }
    """
    report = run(
        tmp_path,
        """
        import "lib.jig"
        import "lib.jig" as lib
        test "calls" {
            check add(1) == 11
            check add(1, 2) == 3
            check twice(4) == 8
            check lib.add(5) == 15
            check lib.OFFSET == 10
        }
    """,
        **{"lib.jig": library},
    )
    assert statuses(report) == {"calls": "PASS"}, report.cases[0].detail


def test_loops(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "loops" {
            let total = 0
            repeat 4 times as i { total += i }
            check total == 6
            let n = 0
            while true { n += 1  if n == 3 { break } }
            check n == 3
            let odd = []
            for value in 1 .. 7 {
                if value % 2 == 0 { continue }
                odd.append(value)
            }
            check odd == [1, 3, 5, 7]
        }
    """,
    )
    assert statuses(report) == {"loops": "PASS"}, report.cases[0].detail


def test_every_keeps_its_period_in_simulated_time(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "every" {
            let stamps = []
            every 250 ms for 1 s { stamps.append(now()) }
            check stamps == [0, 0.25, 0.5, 0.75]
            let count = 0
            every 100 ms times 3 { count += 1 }
            check count == 3
        }
    """,
    )
    assert statuses(report) == {"every": "PASS"}, report.cases[0].detail


def test_parallel_tasks_share_time_and_variables(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "parallel" {
            let done = []
            parallel {
                task slow { wait 2 s  done.append("slow") }
                task fast { wait 500 ms  done.append("fast") }
            }
            check done == ["fast", "slow"]
            check now() == 2
        }
        test "race" {
            let start = now()
            race {
                task never { wait until false within 10 s }
                task quick { wait 300 ms }
            }
            check abs(now() - start - 0.3) < 1e-9
        }
    """,
    )
    assert statuses(report) == {"parallel": "PASS", "race": "PASS"}, [
        c.detail for c in report.cases
    ]
    tasks = {step.task for step in report.cases[0].steps}
    assert {"slow", "fast", "main"} <= tasks


def test_a_failure_in_one_task_fails_the_test(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "task fails" {
            parallel {
                task a { wait 1 s  fail "a broke" }
                task b { wait 10 s }
            }
        }
    """,
    )
    assert report.cases[0].status == "FAIL"
    assert "a broke" in report.cases[0].detail


def test_timeline_entries_start_at_their_offsets(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        const PASS = 2 s
        test "timeline" {
            wait 1 s
            let started = []
            timeline {
                at 3 s as late { started.append(["late", now()]) }
                at 0 s {
                    started.append(["first", now()])
                    wait 2.5 s
                    started.append(["end", now()])
                }
                at PASS + 500 ms as pass { started.append(["pass", now()]) }
            }
            # Entries due at the same moment run in the order they are written.
            check started == [["first", 1], ["end", 3.5], ["pass", 3.5], ["late", 4]]
            check now() == 4
        }
    """,
    )
    assert statuses(report) == {"timeline": "PASS"}, report.cases[0].detail
    tasks = {step.task for step in report.cases[0].steps}
    assert {"late", "at 0 s", "pass"} <= tasks


def test_profiles_are_plan_values(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "profiles" {
            let rate = table([[0 s, 10], [10 s, 30]]) + 5
            check rate.at(5 s) == 25
            let wave = sine(mean: 1 V, amplitude: 0.5 V, period: 4 s)
            check wave.at(1 s) == 1.5
            check (wave + ramp(0, 2, over: 2 s)).at(1 s) == 2.5
            record shape = wave
        }
        test "a profile adds only to numbers and profiles" {
            let bad = sine(mean: 1, amplitude: 1, period: 1 s) + [1]
        }
    """,
    )
    assert statuses(report) == {
        "profiles": "PASS",
        "a profile adds only to numbers and profiles": "ERROR",
    }, report.cases[0].detail
    assert "not list" in report.cases[1].detail


def test_records_in_a_time_window(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "during" {
            let packets = [{time: 1, n: 1}, {time: 2.5, n: 2}, {time: 3, n: 3}]
            check len(during(packets, 1 s, 3 s)) == 2
            check during(packets, 2.5 s, 10 s)[0].n == 2
            check floor(2.7) == 2 and ceil(2.1) == 3
            let epoch = "2027-03-21T06:00:00"
            check seconds_between(epoch, time_after(epoch, 90 s)) == 90
        }
        test "during needs times" {
            let bad = during([{n: 1}], 0, 1)
        }
    """,
    )
    assert statuses(report) == {"during": "PASS", "during needs times": "ERROR"}, report.cases[
        0
    ].detail
    assert "every record needs a time" in report.cases[1].detail


def test_timeline_failure_cancels_the_other_entries(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "fails" {
            timeline {
                at 1 s as broken { fail "entry broke" }
                at 30 s as never { log "unreachable" }
            }
        }
        test "names" {
            timeline {
                at 1 s { log "a" }
                at 1 s { log "b" }
            }
        }
        test "negative" {
            timeline { at -1 s { log "before" } }
        }
    """,
    )
    assert statuses(report) == {"fails": "FAIL", "names": "ERROR", "negative": "ERROR"}
    assert "entry broke" in report.cases[0].detail
    assert report.cases[0].finished == pytest.approx(1.0)
    assert "different names" in report.cases[1].detail
    assert "needs a duration" in report.cases[2].detail


def test_wait_until_times_out_as_a_failure(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "timeout" { wait until now() > 5 within 1 s }
        test "succeeds" { wait until now() > 1.5 within 5 s  check now() >= 1.5 }
    """,
    )
    assert statuses(report) == {"timeout": "FAIL", "succeeds": "PASS"}
    assert "not within 1 s" in report.cases[0].detail


def test_plan_errors_carry_their_location(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "unknown name" {
            log "fine"
            check missing == 1
        }
        test "next test still runs" { check true }
    """,
    )
    assert statuses(report) == {"unknown name": "ERROR", "next test still runs": "PASS"}
    assert "plan.jig:4:13" in report.cases[0].detail
    assert "unknown name 'missing'" in report.cases[0].detail


def test_syntax_errors_point_at_the_file(tmp_path: Path) -> None:
    plan = tmp_path / "broken.jig"
    plan.write_text('test "x" {\n    check 1 ==\n}\n', encoding="utf-8", newline="\n")
    with pytest.raises(PlanError, match=r"broken.jig:3:1"):
        load(plan)


def test_records_and_fingerprints(tmp_path: Path) -> None:
    source = """
        test "records" {
            repeat 3 times as i { record sample = i * 1.5  wait 100 ms }
            log "done at {now()}"
        }
    """
    first = run(tmp_path, source)
    second = run(tmp_path, source)
    case = first.cases[0]
    assert case.records["sample"] == [(0, 0), (0.1, 1.5), (0.2, 3.0)]
    assert first.fingerprint() == second.fingerprint()
    changed = run(tmp_path, source.replace("1.5", "2.5"))
    assert changed.fingerprint() != first.fingerprint()


def test_members_outside_the_library_are_refused(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        test "private" { let x = "abc"  check x.__class__ == none }
    """,
    )
    assert report.cases[0].status == "ERROR"
    assert "not a member a plan can use" in report.cases[0].detail


def test_setup_failure_aborts_the_plan(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        """
        setup { fail "no boot" }
        test "never runs" { check true }
    """,
    )
    assert report.aborted.startswith("setup FAIL")
    assert report.cases == []


def describe(value: Any) -> str:  # keeps mypy quiet about unused helpers
    return repr(value)


def test_a_plan_without_a_system_needs_a_board(tmp_path: Path) -> None:
    plan = tmp_path / "bare.jig"
    plan.write_text('test "t" { check true }\n', encoding="utf-8", newline="\n")
    interpreter = Interpreter(load(plan), FakeSimulation(), RunOptions())  # type: ignore[arg-type]
    with pytest.raises(PlanError, match="declares no system; name its board with --board"):
        interpreter.build_system()
