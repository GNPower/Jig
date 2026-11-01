"""The plan language: imports through the library path, the units table,
the plan stdlib and what the interpreter reports per case."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jig.lang import PlanError, RunOptions, load, parser
from jig.lang.values import UNITS, parse_quantity
from jig.report.report import SuiteReport
from jig.testing import FakeConsole, FakeMachine, FakeSimulation, run_source

from .conftest import FIXTURES


def statuses(report: SuiteReport) -> dict[str, str]:
    return {case.name: case.status for case in report.all_cases()}


def details(report: SuiteReport) -> list[str]:
    return [case.detail for case in report.all_cases() if case.status not in ("PASS", "SKIP")]


# ------------------------------------------------------------------- imports


def test_imports_reach_the_library_path_boards_and_the_stdlib(tmp_path: Path) -> None:
    report = run_source(
        tmp_path,
        """
        import "common.jig"
        import "example-node:node.jig" as node
        import "std:text.jig"
        import "./local.jig"
        test "imports" {
            check LIBRARY == "common" and twice(21) == 42
            check node.NODE_PORT == 10 and node.greeting("x") == "hello x"
            check number_in("count: 42", /count: (\\d+)/) == 42
            check LOCAL == 1
        }
        """,
        files={"local.jig": "const LOCAL = 1\n"},
    )
    assert statuses(report) == {"imports": "PASS"}, details(report)


def test_a_file_beside_the_plan_wins_over_the_library_path(tmp_path: Path) -> None:
    report = run_source(
        tmp_path,
        'import "common.jig"\ntest "t" { check LIBRARY == "beside" }\n',
        files={"common.jig": 'const LIBRARY = "beside"\n'},
    )
    assert statuses(report) == {"t": "PASS"}, details(report)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('import "absent.jig"', r"import 'absent.jig': no such file \(looked at .*absent.jig, "),
        ('import "no-such-board:x.jig"', "no board named 'no-such-board'"),
        ('import "example-node:../../example-zynq/board.jig"', "reaches outside"),
        ('import "example-node:../plugin.py"', "reaches outside"),
        ('import "std:absent.jig"', r"import 'std:absent.jig': no such file"),
        ('import "notes.txt"', "imports name .jig files, not notes.txt"),
    ],
)
def test_import_errors(tmp_path: Path, line: str, message: str) -> None:
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    plan = tmp_path / "plan.jig"
    plan.write_text(line + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(PlanError, match=message) as error:
        load(plan)
    assert "plan.jig:1:1" in str(error.value)


def test_library_references_keep_drive_letters_as_paths(tmp_path: Path) -> None:
    library = tmp_path / "lib.jig"
    library.write_text("const X = 1\n", encoding="utf-8")
    plan = tmp_path / "plan.jig"
    plan.write_text(f'import "{library.as_posix()}"\n', encoding="utf-8", newline="\n")
    assert parser.resolve_import(library.as_posix(), plan) == library.resolve()
    assert load(plan).main.imports[0][0].path == library.resolve()


# --------------------------------------------------------------------- units


def test_the_grammar_takes_its_units_from_the_units_table() -> None:
    alternation = parser.unit_alternation()
    assert sorted(alternation.replace("\\/", "/").split("|")) == sorted(UNITS)
    assert alternation.index("Mbit\\/s") < alternation.index("ms")  # longest first
    assert "@UNITS@" not in parser.grammar_text()
    quantity = re.search(r"Quantity:\s*value=/(.*)/\s*;", parser.grammar_text())
    assert quantity is not None and alternation in quantity.group(1)


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_every_unit_parses_in_a_plan(tmp_path: Path, unit: str) -> None:
    report = run_source(
        tmp_path, f'test "u" {{ check 3 {unit} == {parse_quantity("3 " + unit)!r} }}\n'
    )
    assert statuses(report) == {"u": "PASS"}, details(report)


def test_keywords_come_from_the_grammar() -> None:
    assert {"check", "wait", "timeline", "none", "over"} <= parser.KEYWORDS
    # Declaration words are matched only where a declaration starts.
    assert not {"board", "link", "device", "option", "peripheral", "sensor"} & parser.KEYWORDS


# ---------------------------------------------------------- grammar version 3


def test_over_is_reserved_but_argument_names_are_not(tmp_path: Path) -> None:
    report = run_source(
        tmp_path,
        'test "t" {\n'
        "    let shape = ramp(16, 256, over: 10 s)\n"
        "    check round(shape.at(5)) == 136\n"
        '    let table = {0: "cs0", 0x40: "pmon", 0b11: 3, name: 4}\n'
        '    check table[0x40] == "pmon" and table[3] == 3 and table.name == 4\n'
        '    let device = "a name that board files reserve"\n'
        "    check len(device) > 0\n"
        "}\n",
    )
    assert statuses(report) == {"t": "PASS"}, details(report)
    with pytest.raises(PlanError, match="syntax error"):
        run_source(tmp_path, 'test "t" { let x = over }\n', name="reserved")


def test_a_plan_holds_no_board_definition(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match=r"plan\.jig:2:1: a plan file holds no board definition"):
        run_source(tmp_path, '# a board in a plan\nboard "x" { description: "no" }\n')


def test_a_bus_declaration_points_at_links(tmp_path: Path) -> None:
    source = 'system {\n    board a: "example-zynq"\n    bus can0: can(members: [a.can])\n}\n'
    with pytest.raises(PlanError, match=r"3:\d+: syntax error: .*link NAME: can\(\.\.\.\)"):
        run_source(tmp_path, source)


def test_board_files_hold_consts_and_one_board(tmp_path: Path) -> None:
    good = tmp_path / "good.jig"
    good.write_text('const X = 1\nboard "b" { description: "{X}" }\n', encoding="utf-8")
    program = parser.load_board_file(good)
    assert [type(item).__name__ for item in program.main.model.items] == [
        "Const",
        "BoardDefinition",
    ]
    for text, message in (
        ('board "b" { }\ntest "t" { }\n', "not a test"),
        ('import "x.jig"\nboard "b" { }\n', "not an import"),
        ('board "b" { }\nboard "c" { }\n', "holds one board definition"),
        ('board "b" { }\nconst X = 1\n', "const lines come before the board definition"),
        ("const X = 1\n", "holds one board definition"),
    ):
        bad = tmp_path / "bad.jig"
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(PlanError, match=message):
            parser.load_board_file(bad)


# -------------------------------------------------------------------- stdlib


def test_stdlib_text_and_records(tmp_path: Path) -> None:
    report = run_source(
        tmp_path,
        """
        import "std:text.jig"
        import "std:records.jig"
        test "text" {
            check number_in("rate: 0x1F per s", /rate: (\\S+)/) == 31
            let dump = "vfs cat -h /f\\n00 01 ff\\n[LOG] noise\\n10 20\\n> "
            check hex_dump_bytes(dump) == x"0001ff1020"
            check hex_dump_bytes("no hex here") == x""
        }
        test "records" {
            let packets = [{time: 1}, {time: 2.5}, {time: 3}, {time: 9}]
            check count_during(packets, 1 s, 3 s) == 2
            check count_during(packets, 0, 10 s) == 4
        }
        test "missing number" { number_in("nothing", /count: (\\d+)/) }
        """,
    )
    assert statuses(report) == {
        "text": "PASS",
        "records": "PASS",
        "missing number": "FAIL",
    }, details(report)
    assert "no /count: (\\d+)/ in the output" in report.cases[2].detail


def test_stdlib_console_procedures(tmp_path: Path) -> None:
    sim = FakeSimulation(workdir=tmp_path / "out")
    consoles: list[FakeConsole] = []

    def script(machine: FakeMachine) -> None:
        console = machine.console
        console.feed("booting\nready> ")
        console.replies = {
            "reboot": "rebooting\nready> ",
            "stats": "can tx 4\ncan tx errors 1\ncsp rx 9\n",
        }
        consoles.append(console)

    sim.on_board["dut"] = script
    report = run_source(
        tmp_path,
        """
        import "std:console.jig"
        system { board dut: "example-zynq" }
        test "console" {
            check wait_ready(dut, "ready>").text == "ready>"
            check now() == 0.5
            check restart_via(dut, "reboot", /ready>/).text == "ready>"
            check counter(dut, "can tx") == 4
            check counter(dut, "csp rx", command: "stats") == 9
        }
        test "absent counter" { counter(dut, "dropped") }
        test "never ready" { wait_ready(dut, "never", timeout: 1 s) }
        """,
        simulation=sim,
    )
    assert statuses(report) == {
        "console": "PASS",
        "absent counter": "FAIL",
        "never ready": "FAIL",
    }, details(report)
    (console,) = consoles
    assert console.sent == ["reboot", "stats", "stats", "stats"]
    assert "stats printed no dropped counter" in report.cases[1].detail


# ------------------------------------------------------------- case reports


def test_deselected_tests_are_reported_as_skipped(tmp_path: Path) -> None:
    source = """
    setup { log "up" }
    test "first" [fast] { check true }
    test "second" { check true }
    test "third" [fast] { wait 1 s }
    teardown { log "down" }
    """
    report = run_source(tmp_path, source, options=RunOptions(tags=("fast",)))
    assert statuses(report) == {
        "(setup)": "PASS",
        "first": "PASS",
        "second": "SKIP",
        "third": "PASS",
        "(teardown)": "PASS",
    }
    skipped = report.cases[1]
    assert skipped.detail == "not selected (-k, --tags)"
    assert skipped.line == 4 and skipped.file.endswith("plan.jig")
    assert report.ok
    by_name = run_source(tmp_path, source, options=RunOptions(names=("sec",)))
    assert [c.status for c in by_name.cases] == ["SKIP", "PASS", "SKIP"]


def test_cases_carry_file_line_and_wall_time(tmp_path: Path) -> None:
    report = run_source(
        tmp_path,
        'test "one" {\n    wait 2 s\n}\n\ntest "two" { check false }\n',
    )
    one, two = report.cases
    assert one.line == 1 and two.line == 5
    assert one.file == two.file
    assert one.file.endswith("plan.jig")
    assert one.simulated_seconds == 2 and one.wall_seconds >= 0
    assert two.status == "FAIL"


def test_a_failing_teardown_fails_the_run(tmp_path: Path) -> None:
    report = run_source(tmp_path, 'test "t" { check true }\nteardown { fail "cleanup broke" }\n')
    assert report.teardown is not None and report.teardown.status == "FAIL"
    assert report.cases[0].status == "PASS"
    assert not report.ok and report.exit_code() == 1 and report.status == "FAIL"


def test_a_failing_setup_counts_as_a_test_failure(tmp_path: Path) -> None:
    report = run_source(tmp_path, 'setup { fail "no boot" }\ntest "t" { check true }\n')
    assert report.aborted.startswith("setup FAIL") and report.abort_kind == "failure"
    assert report.exit_code() == 1


def test_the_fixture_project_has_a_library_directory() -> None:
    assert (FIXTURES / "lib" / "common.jig").is_file()
