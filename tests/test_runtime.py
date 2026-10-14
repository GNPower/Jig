"""The simulated-time scheduler, on a fake clock."""

from __future__ import annotations

from typing import Any

import pytest

from jig.runtime.scheduler import Cancelled, Scheduler, SchedulerError, WaitTimeoutError
from jig.testing import FakeClock


def make(step: float = 0.010) -> tuple[FakeClock, Scheduler]:
    clock = FakeClock()
    return clock, Scheduler(clock, step_ns=round(step * 1e9))


def test_sleep_steps_exactly_to_the_deadline() -> None:
    clock, scheduler = make()
    scheduler.run(scheduler.sleep(0.025))
    assert clock.now_ns == 25_000_000
    assert clock.steps == [10_000_000, 10_000_000, 5_000_000]


def test_wait_for_returns_the_condition_value() -> None:
    clock, scheduler = make()
    value = scheduler.run(
        scheduler.wait_for(lambda: "ready" if clock.now_ns >= 30_000_000 else None, 1.0)
    )
    assert value == "ready"
    assert clock.now_ns == 30_000_000


def test_wait_for_times_out_in_simulated_time() -> None:
    clock, scheduler = make()
    with pytest.raises(WaitTimeoutError):
        scheduler.run(scheduler.wait_for(lambda: None, 0.5, "never"))
    assert clock.now_ns == 500_000_000


def test_gather_runs_concurrently_and_keeps_order() -> None:
    clock, scheduler = make()
    order: list[str] = []

    async def worker(name: str, delay: float) -> str:
        await scheduler.sleep(delay)
        order.append(name)
        return name

    async def main() -> list[Any]:
        return await scheduler.gather([worker("slow", 0.3), worker("fast", 0.1)], ["slow", "fast"])

    assert scheduler.run(main()) == ["slow", "fast"]
    assert order == ["fast", "slow"]
    assert clock.now_ns == 300_000_000  # concurrent, not 0.4 s


def test_gather_failure_cancels_the_others_and_runs_their_cleanup() -> None:
    _clock, scheduler = make()
    cleaned: list[str] = []

    async def failing() -> None:
        await scheduler.sleep(0.05)
        raise ValueError("boom")

    async def long_running() -> None:
        try:
            await scheduler.sleep(10)
        finally:
            await scheduler.sleep(0.02)  # clean-up may wait too
            cleaned.append("long")

    async def main() -> None:
        await scheduler.gather([failing(), long_running()], ["failing", "long"])

    with pytest.raises(ValueError, match="boom"):
        scheduler.run(main())
    assert cleaned == ["long"]


def test_race_returns_the_first_and_cancels_the_rest() -> None:
    clock, scheduler = make()
    cancelled: list[str] = []

    async def finisher(name: str, delay: float) -> str:
        try:
            await scheduler.sleep(delay)
        except Cancelled:
            cancelled.append(name)
            raise
        return name

    async def main() -> tuple[str, Any]:
        return await scheduler.race([finisher("a", 1.0), finisher("b", 0.2)], ["a", "b"])

    assert scheduler.run(main()) == ("b", "b")
    assert cancelled == ["a"]
    assert clock.now_ns == 200_000_000


def test_services_run_before_time_moves() -> None:
    _clock, scheduler = make()
    calls: list[int] = []
    scheduler.add_service(lambda: calls.append(1))
    scheduler.run(scheduler.sleep(0.02))
    assert len(calls) >= 2


def test_run_is_not_reentrant() -> None:
    _clock, scheduler = make()

    async def inner() -> None:
        scheduler.run(scheduler.sleep(0.01))

    with pytest.raises(SchedulerError):
        scheduler.run(inner())


def test_awaiting_something_else_fails_the_task() -> None:
    _clock, scheduler = make()

    class Foreign:
        def __await__(self):  # type: ignore[no-untyped-def]
            yield "not a jig wait"

    async def main() -> None:
        await Foreign()

    with pytest.raises(SchedulerError, match="only jig waits"):
        scheduler.run(main())


def test_a_broken_condition_fails_its_task() -> None:
    _clock, scheduler = make()

    def broken() -> None:
        raise KeyError("missing")

    with pytest.raises(KeyError):
        scheduler.run(scheduler.wait_for(broken, 1.0))


def test_the_same_program_takes_the_same_steps() -> None:
    def program() -> list[int]:
        clock, scheduler = make(0.007)

        async def a() -> None:
            for _ in range(5):
                await scheduler.sleep(0.013)

        async def b() -> None:
            await scheduler.wait_for(lambda: True if clock.now_ns > 40_000_000 else None, 1.0)

        async def main() -> None:
            await scheduler.gather([a(), b()], ["a", "b"])

        scheduler.run(main())
        return clock.steps

    assert program() == program()
