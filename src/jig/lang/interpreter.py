"""Running plans: every statement is a coroutine on the simulation's scheduler.

Outcomes, per test:

* PASS: the block ran to its end.
* FAIL: a ``check`` was false, a ``fail`` statement ran, or the system did not
  do what a step waited for (a console pattern, a CSP reply, an upload).
* ERROR: the plan itself is wrong (an unknown name, a bad argument), or the
  emulation stopped (a machine aborted); the latter also stops the plan.

Every check, call, wait, log and record becomes a step in the report, stamped
with simulated time, the task it ran in and its place in the file.
"""

from __future__ import annotations

import functools
import inspect
import logging
import math
import re
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from textx import get_children_of_type

from jig import project
from jig.errors import ConfigError, JigError, SimulationError
from jig.lang import library
from jig.lang.library import (
    BoardHandle,
    BusHandle,
    BusSpec,
    DslError,
    Match,
    NodeDeclaration,
    SystemContext,
)
from jig.lang.parser import Module, PlanError, Program, unquote
from jig.lang.values import (
    Record,
    Regex,
    parse_quantity,
    record_of,
    show,
    truthy,
    type_name,
)
from jig.report.report import ABORT_FAILURE, CaseReport, Step, SuiteReport, abort_kind
from jig.runtime.environment import Profile, as_profile
from jig.runtime.scheduler import Cancelled, Wait, seconds_to_ns
from jig.system import boards, registry
from jig.system.boards import Board
from jig.system.registry import Entry, Member, Owner
from jig.system.system import Simulation

log = logging.getLogger(__name__)

_JIG = Owner("extension", "jig")  # owner of the built-in functions

DEFAULT_WAIT_LIMIT = 60.0  # seconds, for 'wait until' without 'within'
MAX_CALL_DEPTH = 200

_STR_METHODS = frozenset(
    {
        "upper",
        "lower",
        "strip",
        "split",
        "splitlines",
        "startswith",
        "endswith",
        "replace",
        "find",
        "count",
        "encode",
    }
)
_BYTES_METHODS = frozenset({"hex", "decode", "startswith", "endswith", "find", "count"})
_LIST_METHODS = frozenset({"append", "index", "count"})


class CheckFailedError(JigError):
    """A check was false or a fail statement ran."""


class StepFailedError(JigError):
    """A step the system had to satisfy did not happen (timeout, refused request)."""


class _Signal(Exception):  # noqa: N818 - control flow, never reported
    pass


class _Break(_Signal):
    pass


class _Continue(_Signal):
    pass


class _Return(_Signal):
    def __init__(self, value: Any) -> None:
        super().__init__()
        self.value = value


class Scope:
    """Variables of one block; names resolve outwards to the module's scope."""

    __slots__ = ("_task", "parent", "vars")

    def __init__(self, parent: Scope | None = None, task: str | None = None) -> None:
        self.vars: dict[str, Any] = {}
        self.parent = parent
        self._task = task

    @property
    def task(self) -> str:
        scope: Scope | None = self
        while scope is not None:
            if scope._task is not None:
                return scope._task
            scope = scope.parent
        return "main"

    def find(self, name: str) -> Scope | None:
        scope: Scope | None = self
        while scope is not None:
            if name in scope.vars:
                return scope
            scope = scope.parent
        return None

    def lookup(self, name: str) -> Any:
        scope = self.find(name)
        if scope is None:
            raise PlanError(f"unknown name {name!r}")
        return scope.vars[name]

    def declare(self, name: str, value: Any) -> None:
        self.vars[name] = value

    def assign(self, name: str, value: Any) -> None:
        scope = self.find(name)
        if scope is None:
            raise PlanError(f"{name!r} is not declared (use 'let {name} = ...')")
        scope.vars[name] = value


@dataclass
class Procedure:
    name: str
    node: Any
    scope: Scope  # the module scope it was defined in

    def __repr__(self) -> str:
        return f"<procedure {self.name}>"


class Namespace(Mapping[str, Any]):
    """The constants and procedures of an imported file (``import "x.jig" as lib``)."""

    def __init__(self, name: str, scope: Scope) -> None:
        self._name = name
        self._scope = scope

    def __getitem__(self, key: str) -> Any:
        return self._scope.vars[key]

    def __iter__(self) -> Any:
        return iter(self._scope.vars)

    def __len__(self) -> int:
        return len(self._scope.vars)

    def __repr__(self) -> str:
        return f"<library {self._name}>"


@dataclass
class RunOptions:
    boot: str | None = None  # boot mode for every board, instead of the plan's
    mips: int | None = None
    board_options: dict[str, str] = field(default_factory=dict)  # over the plan's choices
    default_board: str | None = None  # for a plan without a system declaration
    names: tuple[str, ...] = ()  # run only tests whose name contains one of these
    tags: tuple[str, ...] = ()  # run only tests carrying one of these tags
    echo: Callable[[str], None] | None = None  # live step log
    renode: Path | None = None  # the Renode to run (--renode), else discovery decides
    allow_untested_renode: bool = False  # accept a Renode outside the supported range


class _Dispatch:
    """A node or bus member board plugins registered: the call goes to the
    plugin of the board the first argument names, or to the one plugin in
    the system that registered the member."""

    def __init__(self, handle: Any, name: str, entries: list[Entry]) -> None:
        self._handle = handle
        self._name = name
        self._entries = entries
        self.__name__ = name

    def __repr__(self) -> str:
        return f"<member {self._name} of {self._handle!r}>"

    def _board(self, value: Any) -> BoardHandle | None:
        if isinstance(value, BoardHandle):
            return value
        resolve = getattr(self._handle, "target", None)
        if resolve is not None and isinstance(value, int) and not isinstance(value, bool):
            try:
                found: BoardHandle | None = resolve(value)[1]
            except JigError:
                return None
            return found
        return None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        board = self._board(args[0]) if args else None
        owners = ", ".join(entry.owner.name for entry in self._entries)
        if board is not None:
            chosen = [e for e in self._entries if e.owner.source == board.definition.source]
            if not chosen:
                raise DslError(
                    f"{self._name}: {board.name} is a {board.board}; the member belongs to {owners}"
                )
        elif len(self._entries) == 1:
            chosen = self._entries
        else:
            raise DslError(f"{self._name}: name the board first; {owners} each define the member")
        member: Member = chosen[0].value
        bound = functools.partial(member.function, self._handle)
        try:
            inspect.signature(bound).bind(*args, **kwargs)
        except TypeError as exc:
            raise DslError(f"{self._name}: {exc}") from None
        except ValueError:
            pass
        return bound(*args, **kwargs)


class Interpreter:
    """Builds a plan's system in a simulation and runs its blocks."""

    def __init__(
        self, program: Program, simulation: Simulation, options: RunOptions | None = None
    ) -> None:
        self.program = program
        self.simulation = simulation
        self.options = options or RunOptions()
        self.registry = registry.default()
        self.registry.ensure_extensions()
        self.system = SystemContext(simulation)
        self.boards: dict[str, BoardHandle] = self.system.boards
        self.buses: dict[str, BusHandle] = self.system.buses
        self.nodes: dict[str, Any] = self.system.nodes
        self.case: CaseReport | None = None
        self._depth = 0
        self._has_call: dict[int, bool] = {}
        self._builtins = Scope()
        self._owners: dict[str, Owner] = {}
        for name, function in library.builtin_functions(
            program.main.path.parent, lambda: self.simulation.now
        ).items():
            self._define_function(name, function, _JIG)
        for name, entry in self.registry.extension_functions().items():
            self._define_function(name, entry.value, entry.owner)
        self._module_scopes: dict[str, Scope] = {}
        self.globals = self._load_module(program.main)

    # ------------------------------------------------------------ modules

    def _load_module(self, module: Module) -> Scope:
        key = str(module.path)
        if key in self._module_scopes:
            return self._module_scopes[key]
        scope = Scope(self._builtins)
        self._module_scopes[key] = scope
        for child, alias in module.imports:
            child_scope = self._load_module(child)
            if alias:
                scope.declare(alias, Namespace(alias, child_scope))
            else:
                for name, value in child_scope.vars.items():
                    if name in scope.vars and scope.vars[name] is not value:
                        raise PlanError(
                            f"{name!r} is defined twice (imported from {child.path.name})",
                            module.path.name,
                        )
                    scope.declare(name, value)
        for item in module.model.items:
            kind = type(item).__name__
            if kind == "Procedure":
                self._define(scope, item.name, Procedure(item.name, item, scope), item)
            elif kind == "Const":
                where = module.location(item)
                try:
                    value = self._evaluate_now(item.value, scope)
                except PlanError as exc:
                    raise PlanError(exc.reason, exc.where or where) from None
                self._define(scope, item.name, value, item)
        return scope

    def _define(self, scope: Scope, name: str, value: Any, node: Any) -> None:
        if name in scope.vars:
            raise PlanError(f"{name!r} is already defined", self.program.location(node))
        scope.declare(name, value)

    # ------------------------------------------------------------- system

    def build_system(self) -> list[str]:
        """Declare the plan's boards, buses and nodes in the simulation (before start).

        Returns one description line per declaration."""
        systems = self.program.main.items("System")
        if len(systems) > 1:
            raise PlanError("a plan declares one system", self.program.location(systems[1]))
        lines: list[str] = []
        if not systems:
            name = self.options.default_board or project.current().default_board
            if name is None:
                raise PlanError(
                    "the plan declares no system; name its board with --board "
                    "or [defaults] board in jig.toml"
                )
            try:
                board = boards.find(name)
                machine = self.simulation.add_board(
                    "dut",
                    board,
                    boot=self.options.boot,
                    performance_mips=self.options.mips,
                    options=self._board_options(board, {}),
                )
                self._add_board_handle("dut", machine, lines)
            except (DslError, ConfigError) as exc:
                raise PlanError(str(exc)) from None
            return lines
        system_scope = Scope(self.globals)
        for name, function in library.system_constructors().items():
            system_scope.declare(name, function)
        self._declare_node_kinds(system_scope)
        for declaration in systems[0].declarations:
            where = self.program.location(declaration)
            try:
                self._declare(declaration, system_scope, lines)
            except (PlanError, DslError, ConfigError) as exc:
                reason = exc.reason if isinstance(exc, PlanError) else str(exc)
                raise PlanError(reason, where) from None
        return lines

    def _declare_node_kinds(self, scope: Scope) -> None:
        """The node kinds of the extensions and of the boards declared so far."""
        sources = [handle.definition.source for handle in self.boards.values()]
        for name, entry in self.registry.node_kinds_for(sources).items():
            scope.declare(name, entry.value)

    def _declare(self, declaration: Any, scope: Scope, lines: list[str]) -> None:
        kind = type(declaration).__name__
        name = declaration.name
        if name in self.globals.vars:
            raise PlanError(f"{name!r} is already defined")
        if kind == "BoardDeclaration":
            if any(arg.name is None for arg in declaration.args):
                raise PlanError('board settings are given by name, e.g. (boot: "qspi")')
            settings = {arg.name: self._evaluate_now(arg.value, scope) for arg in declaration.args}
            definition = boards.find(unquote(declaration.definition.value))
            storage = {
                str(k): (self.program.main.path.parent / str(v)).resolve()
                for k, v in dict(settings.pop("storage", None) or {}).items()
            }
            boot = settings.pop("boot", None)
            mips = settings.pop("mips", None)
            power = _power_setting(settings.pop("power", None))
            machine = self.simulation.add_board(
                name,
                definition,
                boot=self.options.boot or boot,
                performance_mips=self.options.mips or mips,
                storage=storage,
                options=self._board_options(definition, settings),
                power=power,
            )
            self._add_board_handle(name, machine, lines)
            self._declare_node_kinds(scope)
        elif kind == "BusDeclaration":
            spec = self._evaluate_now(declaration.value, scope)
            if not isinstance(spec, BusSpec):
                raise PlanError(f"a bus is declared with can(...), not {show(spec)}")
            members = [(ref.machine, ref.name) for ref in spec.members]
            self.simulation.add_can_bus(name, bitrate=spec.bitrate, members=members)
            bus = BusHandle(name, spec, self.simulation)
            self.buses[name] = bus
            self.globals.declare(name, bus)
            lines.append(
                f"bus {name}: CAN {spec.bitrate:g} bit/s, {', '.join(map(repr, spec.members))}"
            )
        else:
            spec = self._evaluate_now(declaration.value, scope)
            if not isinstance(spec, NodeDeclaration):
                kinds = sorted(
                    self.registry.node_kinds_for(
                        handle.definition.source for handle in self.boards.values()
                    )
                )
                listed = ", ".join(f"{kind}(...)" for kind in kinds) or "a node kind"
                raise PlanError(f"a node is declared with {listed}, not {show(spec)}")
            node = spec.create(name, self.system)
            self.nodes[name] = node
            self.globals.declare(name, node)
            lines.append(f"node {name}: {spec.describe()}")

    def _board_options(self, board: Board, settings: dict[str, Any]) -> dict[str, str]:
        """A board's option values: the project's defaults, then the plan's
        settings, then the run's."""
        unknown = set(settings) - set(board.options)
        if unknown:
            allowed = ", ".join(["boot", "mips", "storage", "power", *board.options])
            raise PlanError(
                f"board {board.name} has no setting {', '.join(sorted(unknown))} (has: {allowed})"
            )
        chosen = {
            name: value
            for name, value in project.current().default_options.items()
            if name in board.options
        }
        chosen.update({name: str(value) for name, value in settings.items()})
        for name, value in self.options.board_options.items():
            if name in board.options:
                chosen[name] = value
        return chosen

    def _add_board_handle(self, name: str, machine: Any, lines: list[str]) -> None:
        handle = BoardHandle(machine)
        self.boards[name] = handle
        self.globals.declare(name, handle)
        for function, entry in self.registry.board_functions(machine.board).items():
            self._define_function(function, entry.value, entry.owner)
        chosen = "".join(f", {option} {value}" for option, value in machine.config.options.items())
        power = "" if machine.initial_power == "on" else ", powered off"
        lines.append(
            f"board {name}: {machine.board.name}, {machine.config.boot} boot, "
            f"{machine.config.performance_mips} MIPS{chosen}{power}"
        )

    def _define_function(self, name: str, function: Any, owner: Owner) -> None:
        existing = self._builtins.vars.get(name)
        if existing is not None and not registry.same(existing, function):
            raise ConfigError(
                f"function {name!r}: {owner} defines it, and so does {self._owners[name]}"
            )
        self._builtins.declare(name, function)
        self._owners.setdefault(name, owner)

    def start_system(self) -> None:
        """Start the simulation and the plan's nodes."""
        self.simulation.start()
        for handle in self.nodes.values():
            start = getattr(handle, "start", None)
            if start is not None:
                start(self.system)

    # ---------------------------------------------------------------- run

    def _selected(self, test: Any) -> bool:
        name = unquote(test.name.value)
        if self.options.names and not any(part in name for part in self.options.names):
            return False
        return not (self.options.tags and not set(self.options.tags) & set(test.tags))

    def tests(self) -> list[Any]:
        """The tests -k and --tags select, in file order."""
        return [test for test in self.program.main.items("Test") if self._selected(test)]

    def run(self, report: SuiteReport) -> None:
        """Run setup, the selected tests and teardown into ``report``; the
        tests -k and --tags leave out are reported as skipped."""
        setups = self.program.main.items("Setup")
        teardowns = self.program.main.items("Teardown")
        try:
            if setups:
                report.setup = self._run_block_case(
                    "(setup)", setups[0].body, setups[0], [], keep_background=True
                )
                if report.setup.status != "PASS":
                    report.aborted = f"setup {report.setup.status}: {report.setup.detail}"
                    report.abort_kind = _abort_kind(report.setup)
                    return
            for test in self.program.main.items("Test"):
                if not self._selected(test):
                    report.cases.append(self._skipped(test))
                    continue
                case = self._run_block_case(
                    unquote(test.name.value), test.body, test, list(test.tags)
                )
                report.cases.append(case)
                if case.status == "ERROR" and self._fatal:
                    report.aborted = f"stopped after {case.name}: {case.detail}"
                    report.abort_kind = _abort_kind(case)
                    return
        finally:
            if teardowns and not self._fatal:
                report.teardown = self._run_block_case(
                    "(teardown)", teardowns[0].body, teardowns[0], []
                )
            self._stop_background(0)

    _fatal = False

    def _source_of(self, node: Any) -> tuple[str, int]:
        """(file relative to the project root, line) of a node."""
        module = self.program.module_of(node)
        try:
            file = module.path.relative_to(project.current().root).as_posix()
        except ValueError:
            file = module.path.as_posix()
        return file, module.line(node)

    def _skipped(self, test: Any) -> CaseReport:
        file, line = self._source_of(test)
        now = self.simulation.now
        return CaseReport(
            unquote(test.name.value),
            "SKIP",
            "not selected (-k, --tags)",
            started=now,
            finished=now,
            where=self.program.location(test),
            tags=list(test.tags),
            file=file,
            line=line,
        )

    def _run_block_case(
        self, name: str, block: Any, node: Any, tags: list[str], keep_background: bool = False
    ) -> CaseReport:
        file, line = self._source_of(node)
        case = CaseReport(
            name,
            "PASS",
            started=self.simulation.now,
            where=self.program.location(node),
            tags=tags,
            file=file,
            line=line,
        )
        self.case = case
        self._echo(f"\n[{name}]")
        scope = Scope(self.globals, task="main")
        background = getattr(self.simulation, "background", None)
        mark = background.mark() if background is not None else 0
        wall = time.monotonic()
        try:
            self.simulation.run(self._block(block, scope))
        except (CheckFailedError, StepFailedError) as exc:
            case.status, case.detail = "FAIL", str(exc)
        except _Signal as signal:
            case.status = "ERROR"
            case.detail = f"{type(signal).__name__.strip('_').lower()} outside a loop or procedure"
        except PlanError as exc:
            case.status, case.detail = "ERROR", str(exc)
        except SimulationError as exc:
            case.status, case.detail = "ERROR", str(exc)
            case.error = exc
            self._fatal = True
        except JigError as exc:
            case.status, case.detail = "FAIL", str(exc)
        except Exception as exc:
            case.status = "ERROR"
            case.detail = f"internal error: {exc!r}\n{traceback.format_exc()}"
            log.exception("internal error in %s", name)
        if not keep_background and not self._fatal:
            self._stop_background(mark)
        self._background_failures(case)
        case.finished = self.simulation.now
        case.simulated_seconds = case.finished - case.started
        case.wall_seconds = time.monotonic() - wall
        if case.status != "PASS":
            self._echo(f"  {case.status}: {case.detail}")
        return case

    def _stop_background(self, mark: int) -> None:
        """Stop the background work started since ``mark`` (sensor drivers and
        the like); it belongs to the block that started it."""
        background = getattr(self.simulation, "background", None)
        if background is None:
            return
        pending = []
        for entry in background.since(mark):
            entry.stop()
            if not entry.task.done:
                pending.append(entry.task)
        if pending:

            async def settled() -> None:
                await Wait(lambda: True if all(task.done for task in pending) else None, None)

            self.simulation.run(settled())

    def _background_failures(self, case: CaseReport) -> None:
        """A background failure not yet reported fails the block that was running."""
        background = getattr(self.simulation, "background", None)
        if background is None:
            return
        for entry in background.entries:
            if entry.error is None or entry.reported:
                continue
            entry.reported = True
            if case.status == "PASS":
                case.status = "ERROR"
                case.detail = f"background work {entry.name} failed: {entry.error}"

    def _echo(self, text: str) -> None:
        if self.options.echo is not None:
            self.options.echo(text)

    def _step(
        self,
        scope: Scope,
        where: str,
        kind: str,
        text: str,
        outcome: str = "ok",
        detail: str = "",
        duration: float = 0.0,
    ) -> None:
        if self.case is None:
            return
        step = Step(self.simulation.now, scope.task, where, kind, text, outcome, detail, duration)
        self.case.steps.append(step)
        recorder = getattr(self.simulation, "recorder", None)
        if recorder is not None:
            recorder.step(self.case.name, step)
        mark = {"ok": " ", "fail": "!", "error": "E", "info": "-"}.get(outcome, "?")
        line = f"{step.time:11.6f} {mark} [{step.task}] {text}"
        if detail:
            line += f"  -- {detail}"
        self._echo(line)

    # ---------------------------------------------------------- statements

    async def _block(self, block: Any, scope: Scope) -> None:
        for statement in block.statements:
            await self._statement(statement, scope)

    async def _statement(self, statement: Any, scope: Scope) -> None:
        where = self.program.location(statement)
        handler = getattr(self, f"_s_{type(statement).__name__}")
        try:
            await handler(statement, scope, where)
        except (_Signal, CheckFailedError, StepFailedError, SimulationError, Cancelled):
            raise
        except PlanError as exc:
            if exc.where:
                raise
            raise PlanError(exc.reason, where) from None
        except DslError as exc:
            raise PlanError(str(exc), where) from None
        except ConfigError as exc:
            raise PlanError(str(exc), where) from None
        except JigError as exc:
            raise StepFailedError(f"{where}: {exc}") from exc
        except (
            TypeError,
            ValueError,
            KeyError,
            IndexError,
            ZeroDivisionError,
            AttributeError,
            re.error,
        ) as exc:
            raise PlanError(f"{type(exc).__name__}: {exc}", where) from None

    async def _s_Let(self, node: Any, scope: Scope, where: str) -> None:
        value = await self._timed_value(node.value, scope, where, f"let {node.name} = ")
        scope.declare(node.name, value)

    async def _s_Assign(self, node: Any, scope: Scope, where: str) -> None:
        value = await self.evaluate(node.value, scope)
        target = node.target
        if node.op != "=":
            current = await self.evaluate(target, scope)
            value = self._arithmetic("+" if node.op == "+=" else "-", current, value)
        if target.ops:
            last = target.ops[-1]
            if type(last).__name__ != "Index":
                raise PlanError("only names and items ([...]) can be assigned to")
            container = await self._postfix(target.base, target.ops[:-1], scope)
            key = await self.evaluate(last.index, scope)
            if isinstance(container, list):
                container[int(key)] = value
            elif isinstance(container, dict):
                container[key] = value
            else:
                raise PlanError(f"cannot assign into {type_name(container)}")
            return
        if type(target.base).__name__ != "Name":
            raise PlanError("only names and items ([...]) can be assigned to")
        scope.assign(target.base.name, value)

    async def _s_ExprStatement(self, node: Any, scope: Scope, where: str) -> None:
        await self._timed_value(node.expr, scope, where, "")

    async def _timed_value(self, expr: Any, scope: Scope, where: str, prefix: str) -> Any:
        """Evaluate; when the expression calls something, log it as a step."""
        if not self._calls(expr):
            return await self.evaluate(expr, scope)
        text = prefix + self.program.source(expr)
        started = self.simulation.now
        try:
            value = await self.evaluate(expr, scope)
        except (_Signal, Cancelled):
            raise
        except Exception as exc:
            outcome = "error" if isinstance(exc, (PlanError, DslError)) else "fail"
            self._step(
                scope, where, "call", text, outcome, _first_line(exc), self.simulation.now - started
            )
            raise
        detail = "" if value is None else show(value, 100)
        self._step(scope, where, "call", text, "ok", detail, self.simulation.now - started)
        return value

    def _calls(self, expr: Any) -> bool:
        key = id(expr)
        if key not in self._has_call:
            self._has_call[key] = bool(get_children_of_type("Call", expr))
        return self._has_call[key]

    async def _s_If(self, node: Any, scope: Scope, where: str) -> None:
        if truthy(await self.evaluate(node.condition, scope)):
            await self._block(node.then, Scope(scope))
            return
        for branch in node.elifs:
            if truthy(await self.evaluate(branch.condition, scope)):
                await self._block(branch.body, Scope(scope))
                return
        if node.otherwise is not None:
            await self._block(node.otherwise, Scope(scope))

    async def _loop_body(self, body: Any, scope: Scope, variable: str | None, value: Any) -> bool:
        """Run one iteration; False when the loop must stop."""
        child = Scope(scope)
        if variable:
            child.declare(variable, value)
        try:
            await self._block(body, child)
        except _Break:
            return False
        except _Continue:
            pass
        return True

    async def _s_Repeat(self, node: Any, scope: Scope, where: str) -> None:
        count = await self.evaluate(node.count, scope)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise PlanError(f"repeat needs a whole number of times, not {show(count)}")
        for index in range(count):
            if not await self._loop_body(node.body, scope, node.var, index):
                break

    async def _s_While(self, node: Any, scope: Scope, where: str) -> None:
        while truthy(await self.evaluate(node.condition, scope)):
            if not await self._loop_body(node.body, scope, None, None):
                break

    async def _s_For(self, node: Any, scope: Scope, where: str) -> None:
        iterable = await self.evaluate(node.iterable, scope)
        if isinstance(iterable, RangeValue):
            items: list[Any] = iterable.items()
        elif isinstance(iterable, (list, tuple, str, bytes)):
            items = list(iterable)
        elif isinstance(iterable, Mapping):
            items = list(iterable.keys())
        else:
            raise PlanError(f"cannot loop over {type_name(iterable)}")
        for item in items:
            if not await self._loop_body(node.body, scope, node.var, item):
                break

    async def _s_Every(self, node: Any, scope: Scope, where: str) -> None:
        period = self._duration(await self.evaluate(node.period, scope), "every")
        if period <= 0:
            raise PlanError("the period of every must be positive")
        period_ns = seconds_to_ns(period)
        start_ns = self.simulation.now_ns
        end_ns = None
        count = None
        if node.duration is not None:
            end_ns = start_ns + seconds_to_ns(
                self._duration(await self.evaluate(node.duration, scope), "for")
            )
        if node.count is not None:
            count = await self.evaluate(node.count, scope)
            if not isinstance(count, int) or count < 0:
                raise PlanError(f"every ... times needs a whole number, not {show(count)}")
        slot = 0
        iteration = 0
        while True:
            if end_ns is not None and self.simulation.now_ns >= end_ns:
                break
            if count is not None and iteration >= count:
                break
            if node.until is not None and truthy(await self.evaluate(node.until, scope)):
                break
            if not await self._loop_body(node.body, scope, node.var, iteration):
                break
            iteration += 1
            slot += 1
            next_ns = start_ns + slot * period_ns
            now_ns = self.simulation.now_ns
            if now_ns > next_ns:
                skipped = math.ceil((now_ns - next_ns) / period_ns)
                self._step(
                    scope,
                    where,
                    "log",
                    f"every: iteration {iteration} overran its "
                    f"period by {(now_ns - next_ns) / 1e9:.6f} s",
                    "info",
                )
                slot += skipped
                next_ns = start_ns + slot * period_ns
            if end_ns is not None and next_ns >= end_ns:
                break
            await self.simulation.scheduler.sleep_until_ns(next_ns)

    async def _s_Parallel(self, node: Any, scope: Scope, where: str) -> None:
        names = [task.name for task in node.tasks]
        if len(set(names)) != len(names):
            raise PlanError("tasks in one block need different names")
        self._step(scope, where, "task", f"{node.kind} {', '.join(names)}", "info")
        coroutines = [self._task(task, scope) for task in node.tasks]
        scheduler = self.simulation.scheduler
        if node.kind == "parallel":
            await scheduler.gather(coroutines, names)
        else:
            winner, _value = await scheduler.race(coroutines, names)
            self._step(scope, where, "task", f"race won by {winner}", "info")

    async def _task(self, task: Any, scope: Scope) -> None:
        await self._run_task(task.name, task.body, task, scope)

    async def _run_task(self, name: str, body: Any, node: Any, scope: Scope) -> None:
        child = Scope(scope, task=name)
        try:
            await self._block(body, child)
        except (_Break, _Continue, _Return) as signal:
            raise PlanError(
                f"{type(signal).__name__.strip('_').lower()} cannot leave a task",
                self.program.location(node),
            ) from None
        self._step(child, self.program.location(node), "task", f"task {name} finished", "info")

    async def _s_Timeline(self, node: Any, scope: Scope, where: str) -> None:
        start_ns = self.simulation.now_ns
        entries: list[tuple[int, str, Any]] = []
        for entry in node.entries:
            offset = self._duration(await self.evaluate(entry.offset, scope), "at")
            name = entry.name or f"at {self.program.source(entry.offset)}"
            entries.append((start_ns + seconds_to_ns(offset), name, entry))
        names = [name for _start, name, _entry in entries]
        if len(set(names)) != len(names):
            raise PlanError("timeline entries need different names: add 'as <name>'")
        self._step(scope, where, "task", f"timeline: {', '.join(names)}", "info")

        async def run_entry(start: int, name: str, entry: Any) -> None:
            await self.simulation.scheduler.sleep_until_ns(start)
            await self._run_task(name, entry.body, entry, scope)

        coroutines = [run_entry(start, name, entry) for start, name, entry in entries]
        await self.simulation.scheduler.gather(coroutines, names)

    async def _s_Try(self, node: Any, scope: Scope, where: str) -> None:
        try:
            await self._block(node.body, Scope(scope))
        except (CheckFailedError, StepFailedError) as exc:
            error = Record(message=str(exc), kind=type(exc).__name__.removesuffix("Error"))
        else:
            return
        self._step(scope, where, "log", f"caught: {error.message}", "info")
        handler_scope = Scope(scope)
        if node.var:
            handler_scope.declare(node.var, error)
        await self._block(node.handler, handler_scope)

    async def _s_Check(self, node: Any, scope: Scope, where: str) -> None:
        value, detail = await self._condition(node.condition, scope)
        text = self.program.source(node.condition)
        if self.case is not None:
            self.case.checks += 1
        if truthy(value):
            self._step(scope, where, "check", f"check {text}", "ok")
            return
        message = ""
        if node.message is not None:
            message = self._text(await self.evaluate(node.message, scope))
        self._step(scope, where, "check", f"check {text}", "fail", detail)
        suffix = f" ({message})" if message else ""
        raise CheckFailedError(f"{where}: check {text} failed: {detail}{suffix}")

    async def _s_Wait(self, node: Any, scope: Scope, where: str) -> None:
        scheduler = self.simulation.scheduler
        started = self.simulation.now
        if node.condition is None:
            seconds = self._duration(await self.evaluate(node.duration, scope), "wait")
            await scheduler.sleep(seconds)
            self._step(
                scope,
                where,
                "wait",
                f"wait {self.program.source(node.duration)}",
                "ok",
                duration=self.simulation.now - started,
            )
            return
        limit = DEFAULT_WAIT_LIMIT
        if node.timeout is not None:
            limit = self._duration(await self.evaluate(node.timeout, scope), "within")
        text = self.program.source(node.condition)

        def condition() -> bool | None:
            return True if truthy(self._evaluate_now(node.condition, scope)) else None

        try:
            await scheduler.wait_for(
                condition,
                limit,
                error=lambda: StepFailedError(
                    f"{where}: wait until {text}: not within {limit:g} s"
                ),
            )
        except StepFailedError as exc:
            self._step(
                scope,
                where,
                "wait",
                f"wait until {text}",
                "fail",
                str(exc),
                self.simulation.now - started,
            )
            raise
        self._step(
            scope, where, "wait", f"wait until {text}", "ok", duration=self.simulation.now - started
        )

    async def _s_Log(self, node: Any, scope: Scope, where: str) -> None:
        self._step(
            scope, where, "log", self._text(await self.evaluate(node.message, scope)), "info"
        )

    async def _s_Record(self, node: Any, scope: Scope, where: str) -> None:
        value = await self.evaluate(node.value, scope)
        if self.case is not None:
            self.case.record(node.name, self.simulation.now, _plain(value))
            recorder = getattr(self.simulation, "recorder", None)
            if recorder is not None:
                recorder.record(self.case.name, node.name, self.simulation.now, _plain(value))
        self._step(scope, where, "record", f"{node.name} = {show(value, 80)}", "info")

    async def _s_Fail(self, node: Any, scope: Scope, where: str) -> None:
        message = self._text(await self.evaluate(node.message, scope))
        self._step(scope, where, "check", f"fail {message}", "fail")
        raise CheckFailedError(f"{where}: {message}")

    async def _s_Return(self, node: Any, scope: Scope, where: str) -> None:
        raise _Return(await self.evaluate(node.value, scope))

    async def _s_Break(self, node: Any, scope: Scope, where: str) -> None:
        raise _Break()

    async def _s_Continue(self, node: Any, scope: Scope, where: str) -> None:
        raise _Continue()

    # --------------------------------------------------------- expressions

    async def evaluate(self, node: Any, scope: Scope) -> Any:
        return await getattr(self, f"_e_{type(node).__name__}")(node, scope)

    def _evaluate_now(self, node: Any, scope: Scope) -> Any:
        """Evaluate without letting simulated time pass (constants, conditions)."""
        coroutine = self.evaluate(node, scope)
        try:
            coroutine.send(None)
        except StopIteration as stop:
            return stop.value
        coroutine.close()
        raise PlanError("this expression must not wait (it is evaluated without time passing)")

    async def _condition(self, node: Any, scope: Scope) -> tuple[Any, str]:
        """A condition's value and, for a single comparison, both sides."""
        inner = node
        while type(inner).__name__ in ("Or", "And") and len(inner.operands) == 1:
            inner = inner.operands[0]
        if type(inner).__name__ == "Not" and not inner.negated:
            inner = inner.operand
        if type(inner).__name__ == "Comparison" and inner.op is not None:
            left = await self.evaluate(inner.left, scope)
            right = await self.evaluate(inner.right, scope)
            value = self._compare(inner.op, left, right)
            return value, f"{show(left)} {' '.join(inner.op.split())} {show(right)}"
        value = await self.evaluate(node, scope)
        return value, f"the expression is {show(value)}"

    async def _e_TemplateExpr(self, node: Any, scope: Scope) -> Any:
        return await self.evaluate(node.expr, scope)

    async def _e_Or(self, node: Any, scope: Scope) -> Any:
        value = None
        for operand in node.operands:
            value = await self.evaluate(operand, scope)
            if len(node.operands) == 1 or truthy(value):
                return value
        return value

    async def _e_And(self, node: Any, scope: Scope) -> Any:
        value = None
        for operand in node.operands:
            value = await self.evaluate(operand, scope)
            if len(node.operands) > 1 and not truthy(value):
                return value
        return value

    async def _e_Not(self, node: Any, scope: Scope) -> Any:
        value = await self.evaluate(node.operand, scope)
        return not truthy(value) if node.negated else value

    async def _e_Comparison(self, node: Any, scope: Scope) -> Any:
        left = await self.evaluate(node.left, scope)
        if node.op is None:
            return left
        return self._compare(node.op, left, await self.evaluate(node.right, scope))

    def _compare(self, op: str, left: Any, right: Any) -> bool:
        op = " ".join(op.split())
        if op == "==":
            return _equal(left, right)
        if op == "!=":
            return not _equal(left, right)
        if op in ("<", "<=", ">", ">="):
            if not _comparable(left, right):
                raise PlanError(f"cannot order {type_name(left)} and {type_name(right)} with {op}")
            ordered: dict[str, bool] = {
                "<": left < right,
                "<=": left <= right,
                ">": left > right,
                ">=": left >= right,
            }
            return ordered[op]
        if op in ("in", "not in"):
            inside = _contains(right, left)
            return inside if op == "in" else not inside
        if op == "contains":
            return _contains(left, right)
        if op == "matches":
            if isinstance(left, bytes):
                left = left.decode("latin-1")
            if not isinstance(left, str):
                raise PlanError(f"matches needs text on the left, not {type_name(left)}")
            pattern = right.pattern if isinstance(right, Regex) else re.compile(str(right))
            return pattern.search(left) is not None
        raise PlanError(f"unknown operator {op}")

    async def _e_Range(self, node: Any, scope: Scope) -> Any:
        low = await self.evaluate(node.low, scope)
        if node.high is None:
            return low
        high = await self.evaluate(node.high, scope)
        if not _number(low) or not _number(high):
            raise PlanError(f"a range needs numbers, not {show(low)} .. {show(high)}")
        return RangeValue(low, high)

    async def _e_Sum(self, node: Any, scope: Scope) -> Any:
        value = await self.evaluate(node.operands[0], scope)
        for op, operand in zip(node.ops, node.operands[1:], strict=True):
            value = self._arithmetic(op, value, await self.evaluate(operand, scope))
        return value

    async def _e_Product(self, node: Any, scope: Scope) -> Any:
        value = await self.evaluate(node.operands[0], scope)
        for op, operand in zip(node.ops, node.operands[1:], strict=True):
            value = self._arithmetic(op, value, await self.evaluate(operand, scope))
        return value

    def _arithmetic(self, op: str, left: Any, right: Any) -> Any:
        if op == "+":
            if _number(left) and _number(right):
                return left + right
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            if isinstance(left, bytes) and isinstance(right, bytes):
                return left + right
            if isinstance(left, list) and isinstance(right, list):
                return left + right
            if isinstance(left, str) or isinstance(right, str):
                return self._text(left) + self._text(right)
            if isinstance(left, Profile) or isinstance(right, Profile):
                try:
                    return as_profile(left) + as_profile(right)
                except ConfigError as exc:
                    raise PlanError(str(exc)) from None
        elif op == "-":
            if _number(left) and _number(right):
                return left - right
        elif op == "*":
            if _number(left) and _number(right):
                return left * right
            if isinstance(left, (str, bytes, list)) and isinstance(right, int):
                return left * right
        elif op == "/":
            if _number(left) and _number(right):
                if right == 0:
                    raise PlanError("division by zero")
                return left / right
        elif op == "%":
            if _number(left) and _number(right):
                if right == 0:
                    raise PlanError("division by zero")
                return left % right
        raise PlanError(f"cannot apply {op} to {type_name(left)} and {type_name(right)}")

    async def _e_Unary(self, node: Any, scope: Scope) -> Any:
        value = await self.evaluate(node.operand, scope)
        if node.sign is None:
            return value
        if not _number(value):
            raise PlanError(f"cannot negate {type_name(value)}")
        return -value if node.sign == "-" else value

    async def _e_Postfix(self, node: Any, scope: Scope) -> Any:
        return await self._postfix(node.base, node.ops, scope)

    async def _postfix(self, base: Any, ops: list[Any], scope: Scope) -> Any:
        value = await self.evaluate(base, scope)
        for op in ops:
            kind = type(op).__name__
            if kind == "Attribute":
                value = self._attribute(value, op.name)
            elif kind == "Call":
                positional: list[Any] = []
                named: dict[str, Any] = {}
                for argument in op.args:
                    argument_value = await self.evaluate(argument.value, scope)
                    if argument.name:
                        if argument.name in named:
                            raise PlanError(f"argument {argument.name} given twice")
                        named[argument.name] = argument_value
                    elif named:
                        raise PlanError("positional arguments come before named ones")
                    else:
                        positional.append(argument_value)
                value = await self._call(value, positional, named, scope)
            else:
                value = self._index(value, await self.evaluate(op.index, scope))
        return value

    def _registered_member(self, value: Any, name: str) -> tuple[bool, Any]:
        """(found, value) of a member extensions or board plugins registered."""
        kind = getattr(type(value), "dsl_kind", None)
        if kind is None:
            return False, None
        entries = self.registry.member_entries(kind, name)
        if kind == "board":
            source = value.definition.source
            entries = [
                e for e in entries if e.owner.kind == "extension" or e.owner.source == source
            ]
        else:
            general = [e for e in entries if e.owner.kind == "extension"]
            if general:
                entries = general[:1]
            else:
                sources = {handle.definition.source for handle in self.boards.values()}
                entries = [e for e in entries if e.owner.source in sources]
                if entries:
                    return True, _Dispatch(value, name, entries)
        if not entries:
            return False, None
        member: Member = entries[0].value
        if member.attribute:
            return True, member.function(value)
        bound = functools.partial(member.function, value)
        return True, bound

    def _member_names(self, value: Any, members: frozenset[str]) -> list[str]:
        kind = getattr(type(value), "dsl_kind", None)
        names = set(members)
        if kind is not None:
            if kind == "board":
                sources: list[Path | None] = [value.definition.source]
            else:
                sources = [handle.definition.source for handle in self.boards.values()]
            names.update(self.registry.member_names(kind, sources))
        return sorted(names)

    def _attribute(self, value: Any, name: str) -> Any:
        if name.startswith("_"):
            raise PlanError(f"{name!r} is not a member a plan can use")
        if isinstance(value, Mapping):
            if name in value:
                return value[name]
            known = ", ".join(sorted(str(k) for k in value)) or "none"
            raise PlanError(f"no field {name!r} (fields: {known})")
        members = getattr(type(value), "dsl_members", None)
        if members is not None:
            if name in members:
                return getattr(value, name)
            found, registered = self._registered_member(value, name)
            if found:
                return registered
            dynamic = getattr(value, "dsl_attribute", None)
            if dynamic is not None:
                attribute = dynamic(name)
                if attribute is not None:
                    return attribute
            known = ", ".join(self._member_names(value, members))
            raise PlanError(f"{show(value)} has no member {name!r} (has: {known})")
        allowed = (
            _STR_METHODS
            if isinstance(value, str)
            else _BYTES_METHODS
            if isinstance(value, bytes)
            else _LIST_METHODS
            if isinstance(value, list)
            else frozenset()
        )
        if name in allowed:
            return getattr(value, name)
        raise PlanError(f"{type_name(value)} has no member {name!r}")

    def _index(self, value: Any, index: Any) -> Any:
        if isinstance(value, (list, tuple, str, bytes)):
            if not isinstance(index, int) or isinstance(index, bool):
                raise PlanError(f"an index into {type_name(value)} is a whole number")
            try:
                return value[index]
            except IndexError:
                raise PlanError(f"index {index} is outside 0..{len(value) - 1}") from None
        if isinstance(value, Mapping):
            if index not in value:
                raise PlanError(f"no key {show(index)}")
            return value[index]
        if isinstance(value, Match):
            return value[index]
        raise PlanError(f"cannot index {type_name(value)}")

    async def _call(
        self, function: Any, positional: list[Any], named: dict[str, Any], scope: Scope
    ) -> Any:
        if isinstance(function, Procedure):
            return await self._call_procedure(function, positional, named, scope)
        if not callable(function):
            raise PlanError(f"{show(function)} cannot be called")
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            try:
                signature.bind(*positional, **named)
            except TypeError as exc:
                raise PlanError(f"{getattr(function, '__name__', 'call')}: {exc}") from None
        result = function(*positional, **named)
        if inspect.isawaitable(result):
            result = await result
        return _wrap_result(result)

    async def _call_procedure(
        self, procedure: Procedure, positional: list[Any], named: dict[str, Any], caller: Scope
    ) -> Any:
        params = procedure.node.params
        if len(positional) > len(params):
            raise PlanError(
                f"{procedure.name} takes {len(params)} arguments, not {len(positional)}"
            )
        names = [param.name for param in params]
        for key in named:
            if key not in names:
                raise PlanError(f"{procedure.name} has no parameter {key!r}")
        if self._depth >= MAX_CALL_DEPTH:
            raise PlanError(f"procedures nested more than {MAX_CALL_DEPTH} deep")
        local = Scope(procedure.scope, task=caller.task)
        for index, param in enumerate(params):
            if index < len(positional):
                if param.name in named:
                    raise PlanError(f"{procedure.name}: {param.name} given twice")
                value = positional[index]
            elif param.name in named:
                value = named[param.name]
            elif param.default is not None:
                value = await self.evaluate(param.default, procedure.scope)
            else:
                raise PlanError(f"{procedure.name} needs a value for {param.name}")
            local.declare(param.name, value)
        self._depth += 1
        try:
            await self._block(procedure.node.body, local)
        except _Return as result:
            return result.value
        except (_Break, _Continue):
            raise PlanError(f"break/continue cannot leave procedure {procedure.name}") from None
        finally:
            self._depth -= 1
        return None

    async def _e_Quantity(self, node: Any, scope: Scope) -> Any:
        return parse_quantity(node.value)

    async def _e_Bool(self, node: Any, scope: Scope) -> Any:
        return node.value == "true"

    async def _e_Null(self, node: Any, scope: Scope) -> Any:
        return None

    async def _e_Bytes(self, node: Any, scope: Scope) -> Any:
        digits = "".join(node.value[2:-1].replace("_", "").split())
        if len(digits) % 2:
            raise PlanError(f"bytes literal {node.value} has an odd number of hex digits")
        return bytes.fromhex(digits)

    async def _e_Str(self, node: Any, scope: Scope) -> Any:
        template = self.program.template(node)
        if template is None:
            return unquote(node.value)  # a string inside a {field}
        if template.constant:
            return "".join(part for part in template.parts if isinstance(part, str))
        out: list[str] = []
        for part in template.parts:
            if isinstance(part, str):
                out.append(part)
                continue
            expression, spec = part
            value = await self.evaluate(expression, scope)
            if spec:
                try:
                    out.append(format(value, spec))
                except (ValueError, TypeError) as exc:
                    raise PlanError(
                        f"format {spec!r} does not apply to {show(value)}: {exc}"
                    ) from None
            else:
                out.append(self._text(value))
        return "".join(out)

    async def _e_Regex(self, node: Any, scope: Scope) -> Any:
        try:
            return Regex(node.value)
        except re.error as exc:
            raise PlanError(f"bad regular expression {node.value}: {exc}") from None

    async def _e_List(self, node: Any, scope: Scope) -> Any:
        return [await self.evaluate(item, scope) for item in node.items]

    async def _e_Map(self, node: Any, scope: Scope) -> Any:
        result: dict[str, Any] = {}
        for entry in node.entries:
            key = entry.key if entry.key else unquote(entry.quoted.value)
            result[key] = await self.evaluate(entry.value, scope)
        return result

    async def _e_Paren(self, node: Any, scope: Scope) -> Any:
        return await self.evaluate(node.expr, scope)

    async def _e_Name(self, node: Any, scope: Scope) -> Any:
        return scope.lookup(node.name)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _duration(value: Any, what: str) -> float:
        if not _number(value) or value < 0:
            raise PlanError(f"{what} needs a duration in seconds, not {show(value)}")
        return float(value)

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, float):
            return f"{value:.9g}"
        if isinstance(value, Match):
            return value.text
        return show(value, 10_000)


class RangeValue:
    """``low .. high``, both ends included."""

    def __init__(self, low: int | float, high: int | float) -> None:
        self.low = low
        self.high = high

    def __contains__(self, value: Any) -> bool:
        return _number(value) and self.low <= value <= self.high

    def items(self) -> list[Any]:
        if not isinstance(self.low, int) or not isinstance(self.high, int):
            raise PlanError("only a range of whole numbers can be looped over")
        return list(range(self.low, self.high + 1))

    def __repr__(self) -> str:
        return f"{show(self.low)} .. {show(self.high)}"


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _comparable(left: Any, right: Any) -> bool:
    if _number(left) and _number(right):
        return True
    return type(left) is type(right) and isinstance(left, (str, bytes))


def _equal(left: Any, right: Any) -> bool:
    if _number(left) and _number(right):
        return bool(left == right)
    if isinstance(left, Match):
        left = left.text
    if isinstance(right, Match):
        right = right.text
    if type(left) is not type(right) and not (
        isinstance(left, Mapping) and isinstance(right, Mapping)
    ):
        return left is None and right is None
    return bool(left == right)


def _contains(container: Any, item: Any) -> bool:
    if isinstance(container, RangeValue):
        return item in container
    if isinstance(container, Match):
        container = container.text
    if isinstance(container, str):
        if isinstance(item, Regex):
            return item.search(container) is not None
        if not isinstance(item, str):
            raise PlanError(f"text can contain text or a /regex/, not {type_name(item)}")
        return item in container
    if isinstance(container, bytes):
        if isinstance(item, str):
            item = item.encode("latin-1")
        if not isinstance(item, (bytes, int)):
            raise PlanError(f"bytes can contain bytes, not {type_name(item)}")
        return item in container
    if isinstance(container, (list, tuple)):
        return any(_equal(element, item) for element in container)
    if isinstance(container, Mapping):
        return item in container
    raise PlanError(f"{type_name(container)} has no members to look in")


def _abort_kind(case: CaseReport) -> str:
    """How a block that stopped the plan counts: a failure of the system under
    test, unless the emulation itself failed."""
    return ABORT_FAILURE if case.error is None else abort_kind(case.error)


def _power_setting(value: Any) -> str | None:
    """A board's ``power`` setting in a system declaration: "on" or "off"."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "on" if value else "off"
    if value in ("on", "off"):
        return str(value)
    raise PlanError(f'power is "on" or "off", not {show(value)}')


def _wrap_result(value: Any) -> Any:
    if isinstance(value, re.Match):
        return Match(value)
    if isinstance(value, dict):
        return Record(value)
    if (
        hasattr(value, "__dataclass_fields__")
        and not isinstance(value, type)
        and not hasattr(type(value), "dsl_members")
        and not isinstance(value, (BusSpec, NodeDeclaration))
    ):
        return record_of(value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Record):
        return value.plain()
    if isinstance(value, Match):
        return value.text
    return value


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0][:300] if text else type(exc).__name__


def describe_plan(path: Path) -> list[tuple[str, list[str]]]:
    """(test name, tags) of every test in a plan, without running it."""
    from jig.lang.parser import load

    with project.activate(project.discover(path)):
        program = load(path)
    return [(unquote(t.name.value), list(t.tags)) for t in program.main.items("Test")]
