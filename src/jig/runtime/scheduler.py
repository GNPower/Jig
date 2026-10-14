"""Cooperative tasks on simulated time.

Everything that waits in a test (a console pattern, a CSP reply, a delay, a
whole protocol exchange) is a coroutine on one ``Scheduler``. The scheduler
runs every task that can make progress, and when all of them are blocked it
advances the emulation by one step: never more than the configured step, and
never past the earliest deadline a task is waiting for. After each step the
emulation's output has been delivered (UART bytes, CAN frames), so blocked
conditions are re-evaluated against it.

Tasks run in a fixed order (the order they were spawned), conditions are
evaluated in that order, and time only moves in whole nanoseconds, so a run
with the same inputs takes the same path every time.

    async def ping_twice(node):
        await node.ping(3)
        await scheduler.sleep(1.0)
        await node.ping(3)

    scheduler.run(ping_twice(gs))
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Coroutine, Generator, Iterable
from typing import Any, Generic, Protocol, TypeVar, cast

from jig.errors import JigError

log = logging.getLogger(__name__)

T = TypeVar("T")

NANOSECONDS_PER_SECOND = 1_000_000_000
_MAX_ROUNDS = 100_000  # task rounds without time passing before we call it a livelock


class Cancelled(BaseException):
    """Thrown into a task that was cancelled; derives from BaseException so that
    ``except Exception`` in task code does not swallow it."""


class WaitTimeoutError(JigError):
    """A wait reached its deadline before its condition held."""

    def __init__(self, what: str, timeout: float) -> None:
        super().__init__(f"{what}: not within {timeout:g} s")
        self.what = what
        self.timeout = timeout


class SchedulerError(JigError):
    """Misuse of the scheduler (re-entry, livelock)."""


class Clock(Protocol):
    """What the scheduler drives: a clock in nanoseconds that it can advance."""

    @property
    def now_ns(self) -> int: ...

    def advance(self, nanoseconds: int) -> None: ...


def seconds_to_ns(seconds: float) -> int:
    if seconds < 0:
        raise ValueError(f"negative duration {seconds}")
    return round(seconds * NANOSECONDS_PER_SECOND)


class Wait:
    """What a blocked task waits for: a condition, a deadline, or both.

    ``condition`` returns ``None`` while the wait should continue and any other
    value to end it; that value becomes the result of the ``await``. Without a
    condition the wait ends at the deadline with ``None``. With both, reaching
    the deadline first raises ``timeout_error``.
    """

    __slots__ = ("condition", "deadline_ns", "timeout_error")

    def __init__(
        self,
        condition: Callable[[], Any] | None,
        deadline_ns: int | None,
        timeout_error: Callable[[], BaseException] | None = None,
    ) -> None:
        if condition is None and deadline_ns is None:
            raise ValueError("a wait needs a condition or a deadline")
        self.condition = condition
        self.deadline_ns = deadline_ns
        self.timeout_error = timeout_error

    def __await__(self) -> Generator[Wait, Any, Any]:
        return (yield self)


class Task(Generic[T]):
    """A coroutine scheduled on simulated time."""

    def __init__(self, scheduler: Scheduler, coro: Coroutine[Any, Any, T], name: str) -> None:
        self.scheduler = scheduler
        self.name = name
        self._coro = coro
        self._waiting: Wait | None = None
        self._started = False
        self._cancel_requested = False
        self._cancel_delivered = False
        self.done = False
        self._result: T | None = None
        self._exception: BaseException | None = None

    def __repr__(self) -> str:
        state = "done" if self.done else "waiting" if self._waiting else "ready"
        return f"<Task {self.name} {state}>"

    @property
    def cancelled(self) -> bool:
        return self.done and isinstance(self._exception, Cancelled)

    @property
    def exception(self) -> BaseException | None:
        return self._exception

    def result(self) -> T:
        if not self.done:
            raise SchedulerError(f"task {self.name} has not finished")
        if self._exception is not None:
            raise self._exception
        return cast(T, self._result)

    def cancel(self) -> None:
        """Ask the task to stop: ``Cancelled`` is raised in it at its next turn.

        Only once: clean-up code that awaits after the first ``Cancelled`` runs
        to completion."""
        if not self.done and not self._cancel_delivered:
            self._cancel_requested = True

    def __await__(self) -> Generator[Wait, Any, T]:
        yield from Wait(lambda: True if self.done else None, None).__await__()
        return self.result()

    # -------------------------------------------------------------- stepping

    def _ready(self, now_ns: int) -> tuple[str, Any] | None:
        """How to resume the task now, or None if it stays blocked."""
        if self.done:
            return None
        if self._cancel_requested:
            return ("throw", Cancelled())
        if not self._started:
            return ("send", None)
        wait = self._waiting
        if wait is None:
            return ("send", None)
        if wait.condition is not None:
            try:
                value = wait.condition()
            except Exception as exc:
                return ("throw", exc)
            if value is not None:
                return ("send", value)
            if wait.deadline_ns is not None and now_ns >= wait.deadline_ns:
                error = wait.timeout_error() if wait.timeout_error else WaitTimeoutError("wait", 0)
                return ("throw", error)
            return None
        assert wait.deadline_ns is not None
        if now_ns >= wait.deadline_ns:
            return ("send", None)
        return None

    def _resume(self, how: str, value: Any) -> None:
        self._started = True
        self._waiting = None
        if how == "throw" and isinstance(value, Cancelled):
            self._cancel_requested = False
            self._cancel_delivered = True
        try:
            yielded = self._coro.throw(value) if how == "throw" else self._coro.send(value)
        except StopIteration as stop:
            self._finish(result=stop.value)
            return
        except BaseException as exc:
            self._finish(exception=exc)
            return
        if not isinstance(yielded, Wait):
            self._coro.close()
            self._finish(
                exception=SchedulerError(
                    f"task {self.name} awaited {yielded!r}; only jig waits work on "
                    "simulated time (not asyncio)"
                )
            )
            return
        self._waiting = yielded

    def _finish(self, *, result: Any = None, exception: BaseException | None = None) -> None:
        self.done = True
        self._result = result
        self._exception = exception
        self._waiting = None

    @property
    def deadline_ns(self) -> int | None:
        if self.done or self._waiting is None or self._cancel_requested:
            return None
        return self._waiting.deadline_ns


class Scheduler:
    """Runs tasks on a clock, advancing it only when every task is blocked."""

    def __init__(self, clock: Clock, *, step_ns: int) -> None:
        if step_ns <= 0:
            raise ValueError("the step must be positive")
        self.clock = clock
        self.step_ns = step_ns
        self._tasks: list[Task[Any]] = []
        self._services: list[Callable[[], None]] = []
        self._running = False
        self._names = 0

    # ----------------------------------------------------------------- time

    @property
    def now_ns(self) -> int:
        return self.clock.now_ns

    @property
    def now(self) -> float:
        return self.clock.now_ns / NANOSECONDS_PER_SECOND

    # ---------------------------------------------------------------- tasks

    def spawn(self, coro: Coroutine[Any, Any, T], name: str | None = None) -> Task[T]:
        self._names += 1
        task: Task[T] = Task(self, coro, name or f"task-{self._names}")
        self._tasks.append(task)
        return task

    def add_service(self, service: Callable[[], None]) -> None:
        """Call ``service`` after every step and every round of task execution
        (protocol timers, retransmissions, acknowledgments)."""
        self._services.append(service)

    def run(self, awaitable: Awaitable[T], name: str = "main") -> T:
        """Drive the scheduler until ``awaitable`` completes and return its result.

        Not callable from inside a task: tasks ``await`` instead."""
        if self._running:
            raise SchedulerError("Scheduler.run() called from inside a task; use await")
        task = self.spawn(_as_coroutine(awaitable), name)
        self._running = True
        try:
            while True:
                self._run_ready()
                if task.done:
                    break
                self._advance()
        finally:
            self._running = False
            self._tasks = [t for t in self._tasks if not t.done]
        return task.result()

    @property
    def running(self) -> bool:
        return self._running

    # ----------------------------------------------------------- awaitables

    def sleep(self, seconds: float) -> Wait:
        return Wait(None, self.now_ns + seconds_to_ns(seconds))

    def sleep_until_ns(self, when_ns: int) -> Wait:
        return Wait(None, max(when_ns, self.now_ns))

    def wait_for(
        self,
        condition: Callable[[], T | None],
        timeout: float | None,
        what: str = "condition",
        error: Callable[[], BaseException] | None = None,
    ) -> Wait:
        """Wait until ``condition()`` returns something other than None.

        Raises ``WaitTimeoutError`` (or ``error()``) after ``timeout`` simulated
        seconds; ``timeout=None`` waits without limit."""
        deadline = None if timeout is None else self.now_ns + seconds_to_ns(timeout)
        timeout_error = error or (lambda: WaitTimeoutError(what, timeout or 0.0))
        return Wait(condition, deadline, timeout_error)

    async def gather(
        self, coros: Iterable[Coroutine[Any, Any, Any]], names: Iterable[str]
    ) -> list[Any]:
        """Run coroutines concurrently; return their results in order.

        The first failure cancels the others and is raised once they stopped."""
        tasks = [self.spawn(coro, name) for coro, name in zip(coros, names, strict=True)]
        try:
            await Wait(lambda: True if _settled(tasks) else None, None)
        except Cancelled:
            await _cancel_all(tasks)
            raise
        failed = next((t for t in tasks if t.done and t.exception is not None), None)
        if failed is not None:
            await _cancel_all(tasks)
            raise cast(BaseException, failed.exception)
        return [t.result() for t in tasks]

    async def race(
        self, coros: Iterable[Coroutine[Any, Any, Any]], names: Iterable[str]
    ) -> tuple[str, Any]:
        """Run coroutines concurrently until the first finishes; cancel the rest.

        Returns (name, result) of the first to finish, or raises its exception."""
        tasks = [self.spawn(coro, name) for coro, name in zip(coros, names, strict=True)]
        try:
            await Wait(lambda: True if any(t.done for t in tasks) else None, None)
        except Cancelled:
            await _cancel_all(tasks)
            raise
        winner = next(t for t in tasks if t.done)
        await _cancel_all(tasks)
        return winner.name, winner.result()

    # ---------------------------------------------------------------- loop

    def _run_ready(self) -> None:
        # Services first in every round, so what they deliver or acknowledge is
        # seen by the tasks before time moves on.
        for _ in range(_MAX_ROUNDS):
            self._run_services()
            progressed = False
            now = self.now_ns
            for task in list(self._tasks):
                resume = task._ready(now)
                if resume is None:
                    continue
                task._resume(*resume)
                progressed = True
            if not progressed:
                return
        blocked = ", ".join(t.name for t in self._tasks if not t.done)
        raise SchedulerError(f"tasks keep each other runnable without time passing: {blocked}")

    def _advance(self) -> None:
        now = self.now_ns
        deadlines = [d for d in (t.deadline_ns for t in self._tasks) if d is not None]
        step = self.step_ns
        if deadlines:
            earliest = min(deadlines)
            if earliest > now:
                step = min(step, earliest - now)
        self.clock.advance(step)

    def _run_services(self) -> None:
        for service in self._services:
            service()


def _settled(tasks: list[Task[Any]]) -> bool:
    return all(t.done for t in tasks) or any(t.done and t.exception is not None for t in tasks)


async def _cancel_all(tasks: list[Task[Any]]) -> None:
    """Cancel every unfinished task and wait until all of them stopped."""
    for task in tasks:
        task.cancel()
    await Wait(lambda: True if all(t.done for t in tasks) else None, None)


async def _as_coroutine(awaitable: Awaitable[T]) -> T:
    return await awaitable
