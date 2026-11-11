"""Command-line entry point: ``jig <command>``.

Exit codes: 0 everything passed; 1 a test, a teardown or a board check failed,
or a repeated plan gave more than one fingerprint; 2 a usage or configuration
error (arguments, jig.toml, a board file, a plan that does not parse); 3 an
infrastructure error (Renode, a model compile, a firmware build, a Jig bug);
130 interrupted. With several plans, the highest code wins.
"""

from __future__ import annotations

import argparse
import glob
import importlib
import logging
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from jig import __version__
from jig.errors import JigError
from jig.renode.commands import add_renode_options
from jig.report.report import (
    ABORT_CONFIG,
    ABORT_FAILURE,
    EXIT_FAILURE,
    EXIT_INFRASTRUCTURE,
    EXIT_INTERRUPT,
    EXIT_PASS,
    EXIT_USAGE,
    SuiteReport,
    abort_kind,
    write_junit,
)
from jig.system import renode_shim as renode_commands

if TYPE_CHECKING:
    from jig.storage.layout import Layout, Partition
    from jig.system.boards import Board

log = logging.getLogger("jig")

_GLOB = frozenset("*?[")


def exit_code_for(error: BaseException) -> int:
    """The exit code an error that ended a command stands for."""
    kind = abort_kind(error)
    if kind == ABORT_FAILURE:
        return EXIT_FAILURE
    if kind == ABORT_CONFIG:
        return EXIT_USAGE
    return EXIT_INFRASTRUCTURE


def _configure_logging(verbosity: int) -> None:
    # 0: warnings, 1: progress, 2: progress and plan steps, 3: debug (monitor traffic).
    level = {0: logging.WARNING, 1: logging.INFO, 2: logging.INFO}.get(verbosity, logging.DEBUG)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _board_options(items: list[str] | None) -> dict[str, str]:
    options = {}
    for item in items or []:
        name, _, value = item.partition("=")
        if not name or not value:
            raise JigError(f"--option expects NAME=VALUE, got {item!r}")
        options[name] = value
    return options


def _plans(patterns: list[str]) -> list[Path]:
    """Plan arguments, with wildcards expanded (shells on Windows leave them)."""
    plans: list[Path] = []
    for pattern in patterns:
        if _GLOB & set(pattern) and not Path(pattern).exists():
            # glob.glob, unlike Path.glob, takes absolute patterns as well.
            matches = sorted(glob.glob(pattern, recursive=True))  # noqa: PTH207
            if not matches:
                raise JigError(f"no plan matches {pattern}")
            plans += [Path(match) for match in matches]
        else:
            plans.append(Path(pattern))
    return plans


# ------------------------------------------------------------------ commands


def _cmd_boards(args: argparse.Namespace) -> int:
    from jig.system import boards

    found = boards.catalog()
    if not found:
        print("no boards: add [paths] boards to jig.toml or set JIG_BOARD_PATH")
        return EXIT_PASS
    width = max(len(name) for name in found)
    for name, entry in found.items():
        print(f"{name:<{width}}  {entry.path}  ({entry.origin})")
        for line in _board_summary(name):
            print(f"    {line}")
    return EXIT_PASS


def _board_summary(name: str) -> list[str]:
    """A board's description and options, evaluated with the defaults."""
    from jig.errors import ConfigError
    from jig.system import boards

    try:
        board = boards.find(name)
    except ConfigError as exc:
        return [f"does not load: {exc}"]
    lines = [board.description] if board.description else []
    lines += [f"option {option.name}: {option.describe()}" for option in board.options.values()]
    return lines


def _storage_options(items: list[str] | None) -> dict[str, Path]:
    """``--storage NAME=PATH`` options as a storage map, each path from the
    current directory."""
    storage: dict[str, Path] = {}
    for item in items or []:
        name, _, path = item.partition("=")
        if not name or not path:
            raise JigError(f"--storage expects NAME=PATH, got {item!r}")
        storage[name] = Path(path).resolve()
    return storage


def _cmd_boot(args: argparse.Namespace) -> int:
    from jig.system.system import Simulation

    storage = _storage_options(args.storage)
    started = time.monotonic()
    with Simulation(
        name=f"boot-{args.board}",
        renode=args.renode,
        allow_untested_renode=args.allow_untested_renode,
    ) as sim:
        machine = sim.add_board(
            "dut",
            args.board,
            boot=args.mode,
            performance_mips=args.mips,
            storage=storage,
            options=_board_options(args.option),
        )
        sim.start()
        print(f"run directory: {sim.workdir}", flush=True)
        console = machine.console

        def echo(data: bytes, _stamps: list[int]) -> None:
            sys.stdout.write(data.decode("latin-1"))
            sys.stdout.flush()

        console.port.subscribe(echo)
        wall = time.monotonic()
        sim.run_for(args.seconds)
        elapsed = time.monotonic() - wall
    rate = args.seconds / elapsed if elapsed else 0.0
    print()
    print(
        f"--- {args.seconds:g} s simulated in {elapsed:.1f} s ({rate:.2f}x real time); "
        f"total {time.monotonic() - started:.1f} s"
    )
    return EXIT_PASS


def _print_report(report: SuiteReport, verbose: int) -> None:
    for line in report.system:
        print(f"  {line}")
    for case in report.all_cases():
        if case in (report.setup, report.teardown) and case.status == "PASS":
            continue
        print(f"  {case.status:<5} {case.name}  ({case.simulated_seconds:.2f} s)")
        if case.status not in ("PASS", "SKIP"):
            for line in case.detail.splitlines()[: 12 if verbose < 2 else None]:
                print(f"        {line}")
    if report.aborted:
        print(f"  ABORTED: {report.aborted}")
    skipped = report.count("SKIP")
    print(
        f"  {report.count('PASS')} passed, {report.count('FAIL')} failed, "
        f"{report.count('ERROR')} errors"
        + (f", {skipped} skipped" if skipped else "")
        + f" ({report.simulated_seconds:.1f} s simulated, {report.wall_seconds:.1f} s wall); "
        f"fingerprint {report.fingerprint()[:16]}",
        flush=True,
    )


def _cmd_run(args: argparse.Namespace) -> int:
    from jig.lang import RunOptions, run_plan
    from jig.report.summary import PlanRun

    if args.repeat < 1:
        raise JigError("--repeat must be at least 1")
    echo = (lambda text: print(text, flush=True)) if args.verbose >= 2 else None
    options = RunOptions(
        boot=args.boot,
        mips=args.mips,
        board_options=_board_options(args.option),
        default_board=args.board,
        names=tuple(args.k or ()),
        tags=tuple(t for group in args.tags or () for t in group.split(",") if t),
        echo=echo,
        renode=args.renode,
        allow_untested_renode=args.allow_untested_renode,
    )
    runs: list[PlanRun] = []
    exit_code = EXIT_PASS
    for plan in _plans(args.plans):
        reports = []
        for attempt in range(args.repeat):
            label = f" (run {attempt + 1} of {args.repeat})" if args.repeat > 1 else ""
            print()
            print(f"PLAN  {plan}{label}", flush=True)
            report = run_plan(plan, options, seed=args.seed)
            _print_report(report, args.verbose)
            reports.append(report)
            exit_code = max(exit_code, report.exit_code())
        repeatable = None
        if args.repeat > 1:
            distinct = len({report.fingerprint() for report in reports})
            repeatable = distinct == 1
            if repeatable:
                print(f"  repeatable: {args.repeat} runs, identical results")
            else:
                print(f"  NOT REPEATABLE: {distinct} different results in {args.repeat} runs")
                exit_code = max(exit_code, EXIT_FAILURE)
        runs += [
            PlanRun(report, attempt + 1, args.repeat, repeatable)
            for attempt, report in enumerate(reports)
        ]
    suites = [run.report for run in runs]
    if args.junit:
        write_junit(suites, Path(args.junit))
    if args.html:
        from jig.report.html import write_html

        write_html(suites, Path(args.html))
        print()
        print(f"report: {Path(args.html).resolve()}")
    if args.json:
        from jig.report.summary import write_json

        write_json(runs, exit_code, Path(args.json))
    if len(suites) > 1:
        passed = sum(1 for suite in suites if suite.ok)
        print()
        print(f"{passed} of {len(suites)} plan runs passed")
    return exit_code


def _cmd_list(args: argparse.Namespace) -> int:
    from jig.lang import describe_plan

    for plan in _plans(args.plans):
        print(plan)
        for name, tags in describe_plan(plan):
            suffix = f"  [{', '.join(tags)}]" if tags else ""
            print(f"  {name}{suffix}")
    return EXIT_PASS


def _contract_problems(board: Board, args: argparse.Namespace) -> list[str]:
    """Build the board's machine in Renode, without booting it, and check each
    peripheral the board's settings name against the contract the harness
    calls."""
    from jig.system.system import Simulation

    # Through the import system, so a stand-in in sys.modules (tests) is honoured.
    contracts = importlib.import_module("jig.contracts")

    from jig.errors import ConfigError

    with Simulation(
        name=f"check-{board.name}",
        renode=args.renode,
        allow_untested_renode=args.allow_untested_renode,
    ) as simulation:
        machine = simulation.add_board("dut", board)
        try:
            # Building composes the storage with each model's erased value.
            simulation.start(boot=False)
        except ConfigError as exc:
            return [str(exc)]
        found = contracts.verify(simulation.session, "dut", machine.board)
        return [str(problem) for problem in found]


def _cmd_check(args: argparse.Namespace) -> int:
    from jig.errors import ConfigError
    from jig.system import boards, checks

    names = args.boards or boards.available()
    options = _board_options(args.option)
    failed = 0
    for name in names:
        try:
            # As in jig run, an option applies to the boards that have it.
            offered = boards.board_file(name).options
            board = boards.find(name, options={k: v for k, v in options.items() if k in offered})
        except ConfigError as exc:
            if name not in boards.catalog():
                raise
            problems = [str(exc)]
        else:
            problems = checks.problems(board)
            if args.contracts and not problems:
                problems += _contract_problems(board, args)
        if problems:
            failed += 1
            print(f"{name}: {len(problems)} problem(s)")
            for problem in problems:
                print(f"  {problem}")
        else:
            print(f"{name}: ok")
    return EXIT_FAILURE if failed else EXIT_PASS


def _cmd_platform(args: argparse.Namespace) -> int:
    from jig.system import boards, generate, registry
    from jig.system.boardfile import PLACEHOLDER_WORKDIR
    from jig.system.socs import MachineConfig, driver_for

    board = boards.find(args.board, options=_board_options(args.option))
    plugins = registry.default()
    plugins.load_board(board)
    driver = driver_for(board)
    config = MachineConfig(
        name="dut",
        board=board,
        boot=board.default_boot,
        performance_mips=board.performance_mips,
        workdir=Path(PLACEHOLDER_WORKDIR),
        options=dict(board.option_values),
        hooks=plugins.hooks_for(board),
    )
    text = generate.description(board, driver, config)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {target.resolve()}")
    else:
        sys.stdout.write(text)
    return EXIT_PASS


# --------------------------------------------------------------------- image


def _erased_values(items: list[str] | None) -> dict[str, int]:
    values = {}
    for item in items or []:
        name, _, text = item.partition("=")
        try:
            value = int(text, 0)
        except ValueError:
            value = -1
        if not name or not 0 <= value <= 0xFF:
            raise JigError(f"--erased expects STORAGE=BYTE, such as flash0=0x00, got {item!r}")
        values[name] = value
    return values


def _image_layout(
    args: argparse.Namespace, storage: dict[str, Path] | None = None
) -> tuple[Board, Layout, object]:
    """The board ``--board`` (or BOARD) names with its options, its plugin
    loaded, and its partitions resolved, with ``storage`` as a system's
    storage map gives it."""
    from jig.storage import layout
    from jig.system import boards, registry
    from jig.system.socs import driver_for

    board = boards.find(args.board, options=_board_options(args.option))
    registry.default().load_board(board)
    driver = driver_for(board)
    resolved = layout.for_board(board, driver, layout.sources(storage) if storage else None)
    resolved.raise_problems()
    return board, resolved, driver


def _image_path(path: str) -> str:
    """A path inside a partition, from its root; ``nested`` is ``/nested``,
    so Git Bash, which turns ``/nested`` into a Windows path, can be given
    one without the slash."""
    return "/" + path.strip("/")


def _cmd_image_build(args: argparse.Namespace) -> int:
    from jig.storage import readback
    from jig.storage.compose import compose
    from jig.system import registry
    from jig.system.socs import MachineConfig

    board, resolved, driver = _image_layout(args, _storage_options(args.storage))
    erased = _erased_values(args.erased)
    names = {storage.name for storage in board.storage}
    unknown = sorted(set(erased) - names)
    if unknown:
        raise JigError(
            f"--erased: board {board.name} has no storage {', '.join(unknown)} "
            f"(has: {', '.join(sorted(names)) or 'none'})"
        )
    output = Path(args.output or ".")
    boot = args.boot or board.default_boot
    modes = board.boot_modes or tuple(getattr(driver, "boot_modes", ()))
    if modes and boot not in modes:
        raise JigError(f"board {board.name} has no boot mode {boot!r} (has: {', '.join(modes)})")
    config = MachineConfig(
        name=board.name,
        board=board,
        boot=boot,
        performance_mips=board.performance_mips,
        workdir=output,
        options=dict(board.option_values),
        hooks=registry.default().hooks_for(board),
    )
    family = getattr(driver, "base_images", None)
    bases: dict[str, Path] = family(config) if callable(family) else {}
    if not board.storage:
        print(f"board {board.name} has no storage devices")
        return EXIT_PASS
    output.mkdir(parents=True, exist_ok=True)
    for storage in board.storage:
        composed = compose(
            resolved,
            storage.name,
            erased=erased.get(storage.name, 0xFF),
            base=bases.get(storage.name),
            base_label=f"the {board.soc} family's {boot} boot image",
        )
        target = readback.image_path(output, storage.name)
        composed.write(target)
        parts = [p.name for p in resolved.on(storage.name) if p.source is not None]
        filled = f"; partitions {', '.join(parts)}" if parts else ""
        base = "; the boot image at 0" if composed.base is not None else ""
        print(
            f"wrote {target} ({composed.size} bytes, erased 0x{composed.erased:02X}{base}{filled})"
        )
    return EXIT_PASS


def _image_partition(args: argparse.Namespace) -> tuple[Partition, bytes]:
    from jig.storage import readback

    _board, resolved, _driver = _image_layout(args)
    partition = resolved.partition(args.partition)
    return partition, readback.from_file(partition, Path(args.image))


def _cmd_image_ls(args: argparse.Namespace) -> int:
    from jig.storage import readback

    partition, data = _image_partition(args)
    view = readback.view(partition, data)
    path = _image_path(args.path)
    if args.recursive:
        if path != "/" and view.stat(path).kind != "dir":
            raise JigError(f"partition {partition.name}: {path} is not a directory")
        under = "/" if path == "/" else path + "/"
        entries = [entry for entry in view.walk() if entry.path.startswith(under)]
    else:
        entries = view.files(path)
    for entry in entries:
        size = "" if entry.kind == "dir" else str(entry.size)
        print(f"{entry.kind:<4} {size:>10}  {entry.path}")
    return EXIT_PASS


def _cmd_image_extract(args: argparse.Namespace) -> int:
    from jig.storage import readback

    partition, data = _image_partition(args)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if partition.format.open(data, partition.arguments) is None:
        target = output / f"{partition.name}.bin"
        target.write_bytes(data)
        print(
            f"partition {partition.name} is {partition.format_name}, without files: wrote {target}"
        )
        return EXIT_PASS
    view = readback.view(partition, data)
    files = directories = 0
    for entry in view.walk():
        target = (output / entry.path.lstrip("/")).resolve()
        if not target.is_relative_to(output):
            raise JigError(f"partition {partition.name}: {entry.path} leads outside {output}")
        if entry.kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
            directories += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(view.read(entry.path))
            files += 1
    print(f"extracted {files} files and {directories} directories to {output}")
    return EXIT_PASS


def _add_image_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    image = sub.add_parser(
        "image", help="build a board's storage images, list and extract a partition's files"
    )
    commands = image.add_subparsers(dest="image_command", required=True)
    build = commands.add_parser(
        "build",
        help="write each storage device's composed image, without Renode",
        description="Write <storage>.img for each storage device of the board: its erased "
        "value, the boot image of the board's boot mode, then the partitions' contents, as "
        "a run composes it.",
    )
    build.add_argument("board", help="board name (on the board search path)")
    build.add_argument(
        "--option", action="append", metavar="NAME=VALUE", help="board option (repeatable)"
    )
    build.add_argument("--boot", help="boot mode (default: the board's)")
    build.add_argument(
        "--erased",
        action="append",
        metavar="STORAGE=BYTE",
        help="a storage device's erased value, when its model's is not 0xFF (repeatable)",
    )
    build.add_argument(
        "--storage",
        action="append",
        metavar="NAME=PATH",
        help="a file for a storage device, a partition or a device memory, or a folder "
        "for a partition, as a system's storage map gives (repeatable)",
    )
    build.add_argument("-o", "--output", help="directory for the images (default: here)")
    build.set_defaults(func=_cmd_image_build)
    for name, func, text in (
        ("ls", _cmd_image_ls, "list the files in a partition of an image"),
        ("extract", _cmd_image_extract, "copy the files of a partition of an image out"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument(
            "image",
            help="an image of the partition's storage device (a run's <machine>/<storage>.img) "
            "or of its device memory",
        )
        command.add_argument("--board", required=True, help="the board the image belongs to")
        command.add_argument("--partition", required=True, help="the partition's name")
        command.add_argument(
            "--option", action="append", metavar="NAME=VALUE", help="board option (repeatable)"
        )
        if name == "ls":
            command.add_argument(
                "--path",
                default="/",
                help="the directory to list, from the partition's root (default /); "
                "nested and /nested are the same",
            )
            command.add_argument(
                "-r", "--recursive", action="store_true", help="list every entry under it"
            )
        else:
            command.add_argument("-o", "--output", required=True, help="directory for the files")
        command.set_defaults(func=func)


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jig", description=__doc__)
    parser.add_argument("--version", action="version", version=f"jig-dsl {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=1,
        help="-v prints plan steps as they run, -vv adds debug logging",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_const",
        const=0,
        dest="verbose",
        help="warnings and errors only",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    before = set(sub.choices)
    renode_commands.add_commands(sub)
    for name in set(sub.choices) - before:
        sub.choices[name].set_defaults(func=renode_commands.run)

    listing = sub.add_parser("boards", help="list the boards on the search path")
    listing.set_defaults(func=_cmd_boards)

    check = sub.add_parser(
        "check", help="check board files: devices, stacks, the platform description"
    )
    check.add_argument("boards", nargs="*", help="board names (default: all)")
    check.add_argument(
        "--contracts",
        action="store_true",
        help="also build each board in Renode and check its peripherals' contracts",
    )
    check.add_argument(
        "--option", action="append", metavar="NAME=VALUE", help="board option (repeatable)"
    )
    add_renode_options(check)
    check.set_defaults(func=_cmd_check)

    platform = sub.add_parser(
        "platform", help="print the platform description Jig writes for a board"
    )
    platform.add_argument("board", help="board name (on the board search path)")
    platform.add_argument(
        "--option", action="append", metavar="NAME=VALUE", help="board option (repeatable)"
    )
    platform.add_argument("-o", "--output", help="write it to this file instead (UTF-8)")
    platform.set_defaults(func=_cmd_platform)

    boot = sub.add_parser("boot", help="boot a board and show its console")
    boot.add_argument("board", help="board name (on the board search path)")
    boot.add_argument("--mode", help="boot mode (default: the board's)")
    boot.add_argument(
        "--seconds", type=float, default=5.0, help="simulated seconds to run (default 5)"
    )
    boot.add_argument("--mips", type=int, help="virtual CPU performance (default: the board's)")
    boot.add_argument(
        "--option", action="append", metavar="NAME=VALUE", help="board option (repeatable)"
    )
    boot.add_argument(
        "--storage",
        action="append",
        metavar="NAME=PATH",
        help="a file for a storage device, a partition or a device memory, or a folder "
        "for a partition, as a system's storage map gives (repeatable)",
    )
    add_renode_options(boot)
    boot.set_defaults(func=_cmd_boot)

    run = sub.add_parser("run", help="run test plans (.jig)")
    run.add_argument("plans", nargs="+", help="plan files (wildcards allowed)")
    run.add_argument("--board", help="board for plans that declare no system")
    run.add_argument("--boot", help="boot mode for every board (default: the plan's)")
    run.add_argument("--mips", type=int, help="virtual CPU performance for every board")
    run.add_argument(
        "--option",
        action="append",
        metavar="NAME=VALUE",
        help="board option for every board that has it, over the plan's (repeatable)",
    )
    run.add_argument("-k", action="append", metavar="TEXT", help="run tests whose name has TEXT")
    run.add_argument("--tags", action="append", metavar="TAG[,TAG]", help="run tests with a tag")
    run.add_argument("--seed", type=int, default=0, help="emulation seed (default 0)")
    run.add_argument(
        "--repeat", type=int, default=1, help="run each plan N times and compare the results"
    )
    run.add_argument("--junit", help="write a JUnit XML report here")
    run.add_argument("--html", help="write an HTML report here")
    run.add_argument("--json", help="write a JSON summary of every plan run here")
    add_renode_options(run)
    run.set_defaults(func=_cmd_run)

    plans = sub.add_parser("list", help="list the tests in plans")
    plans.add_argument("plans", nargs="+", help="plan files (wildcards allowed)")
    plans.set_defaults(func=_cmd_list)
    _add_image_commands(sub)
    _verbosity_after_commands(parser)
    return parser


def _verbosity_after_commands(parser: argparse.ArgumentParser) -> None:
    """``-v`` and ``-q`` after a subcommand as well as before it
    (``jig run plan.jig -v``), on every subcommand at every level."""
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for command in action.choices.values():
            command.add_argument(
                "-v",
                "--verbose",
                dest="verbose_after",
                action="count",
                default=argparse.SUPPRESS,
                help="as before the command: -v plan steps, -vv debug logging",
            )
            command.add_argument(
                "-q",
                "--quiet",
                dest="quiet_after",
                action="store_true",
                default=argparse.SUPPRESS,
                help="as before the command: warnings and errors only",
            )
            _verbosity_after_commands(command)


def _verbosity(args: argparse.Namespace) -> int:
    """0 quiet, 1 progress (the default), 2 plan steps, 3 or more debug: the
    -v and -q given before the subcommand and after it, together."""
    if getattr(args, "quiet_after", False):
        return 0
    return int(args.verbose) + int(getattr(args, "verbose_after", 0))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)  # a usage error exits 2 here
    args.verbose = _verbosity(args)
    _configure_logging(args.verbose)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return EXIT_INTERRUPT
    except JigError as exc:
        log.error("%s", exc)
        return exit_code_for(exc)
    except Exception:
        log.exception("internal error")
        return EXIT_INFRASTRUCTURE


if __name__ == "__main__":
    sys.exit(main())
