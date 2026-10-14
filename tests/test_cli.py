"""The ``jig`` command line, without Renode."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from jig import __version__
from jig.cli import build_parser, main


@pytest.fixture(autouse=True)
def _plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Python 3.14's argparse colours help when FORCE_COLOR is set; these
    tests read it as plain text (PYTHON_COLORS=0 overrides FORCE_COLOR)."""
    monkeypatch.setenv("PYTHON_COLORS", "0")


def test_help_names_the_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: jig ")
    for command in ("doctor", "renode", "boards", "check", "boot", "run", "list"):
        assert command in out


def test_a_usage_error_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["run"])
    assert exit_info.value.code == 2


def test_version_names_the_distribution(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.strip() == f"jig-dsl {__version__}"


def test_python_dash_m_jig_runs_the_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "jig", "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: jig ")


def test_run_has_no_default_board() -> None:
    args = build_parser().parse_args(["run", "plan.jig"])
    assert args.board is None


@pytest.mark.parametrize("command", [["run", "plan.jig"], ["boot", "board"], ["check"]])
def test_commands_that_start_renode_take_the_renode_options(command: list[str]) -> None:
    args = build_parser().parse_args([*command, "--renode", "r", "--allow-untested-renode"])
    assert args.renode == Path("r") and args.allow_untested_renode is True
    args = build_parser().parse_args(command)
    assert args.renode is None and args.allow_untested_renode is False


@pytest.mark.parametrize(
    ("arguments", "level"),
    [
        (["run", "plan.jig"], 1),
        (["-v", "run", "plan.jig"], 2),
        (["run", "plan.jig", "-v"], 2),
        (["run", "-vv", "plan.jig"], 3),
        (["-q", "run", "plan.jig"], 0),
        (["run", "plan.jig", "--quiet"], 0),
        (["renode", "install", "-q", "--yes"], 0),
        (["renode", "-v", "list"], 2),
        (["models", "check", "--verbose"], 2),
    ],
)
def test_verbosity_options_go_before_or_after_the_command(arguments: list[str], level: int) -> None:
    from jig.cli import _verbosity

    assert _verbosity(build_parser().parse_args(arguments)) == level


def test_list_prints_the_tests_of_a_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "plan.jig"
    plan.write_text(
        'test "boots" { check true }\ntest "answers" [can, slow] { check true }\n',
        encoding="utf-8",
        newline="\n",
    )
    assert main(["-q", "list", str(plan)]) == 0
    assert capsys.readouterr().out.splitlines()[1:] == ["  boots", "  answers  [can, slow]"]


def test_a_plan_without_a_system_needs_a_board(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "bare.jig"
    plan.write_text('test "t" { check true }\n', encoding="utf-8", newline="\n")
    assert main(["-q", "run", str(plan)]) == 2
    assert "ABORTED: the plan declares no system; name its board with --board" in (
        capsys.readouterr().out
    )


def test_check_reports_what_a_board_is_missing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["-q", "check", "example-zynq"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("example-zynq: 1 problem(s)")
    assert "firmware app:" in out


def test_a_jig_error_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-q", "boot", "example-zynq", "--storage", "noequals"]) == 2
