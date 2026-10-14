"""Environment profiles and background work, on a fake clock."""

from __future__ import annotations

import math

import pytest

from jig.errors import ConfigError
from jig.runtime import environment
from jig.runtime.environment import Background, BackgroundRegistry
from jig.runtime.scheduler import Scheduler
from jig.testing import FakeClock


def test_profile_shapes() -> None:
    make = environment.constructors()
    sine = make["sine"](4.9, 0.1, 60)
    assert sine.at(0) == pytest.approx(4.9)
    assert sine.at(15) == pytest.approx(5.0)
    assert sine.at(45) == pytest.approx(4.8)
    ramp = make["ramp"](1.0, 2.0, 10)
    assert [ramp.at(t) for t in (-1, 0, 5, 10, 20)] == pytest.approx([1.0, 1.0, 1.5, 2.0, 2.0])
    table = make["table"]([[0, 0.0], [10, 1.0], [20, 0.5]])
    assert [table.at(t) for t in (5, 15, 30)] == pytest.approx([0.5, 0.75, 0.5])
    steps = make["steps"]([[0, 1.0], [10, 2.0]])
    assert [steps.at(t) for t in (0, 9.99, 10, 99)] == [1.0, 1.0, 2.0, 2.0]
    combined = sine + 0.1
    assert combined.at(15) == pytest.approx(5.1)
    assert (0.5 + make["constant"](1)).at(3) == pytest.approx(1.5)
    assert make["sine"](0, 1, 4, phase=1).at(0) == pytest.approx(math.sin(math.pi / 2))


def test_profile_errors() -> None:
    make = environment.constructors()
    with pytest.raises(ConfigError, match="increase"):
        make["table"]([[0, 1], [0, 2]])
    with pytest.raises(ConfigError, match="positive period"):
        make["sine"](0, 1, 0)
    with pytest.raises(ConfigError, match="positive duration"):
        make["ramp"](0, 1, 0)
    with pytest.raises(ConfigError, match=r"not a \[time, value\] pair"):
        make["steps"]([1, 2])


def test_background_work_runs_on_the_grid_and_stops() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, step_ns=10_000_000)
    registry = BackgroundRegistry()
    times: list[float] = []

    async def tick(entry: Background) -> None:
        while True:
            times.append(scheduler.now)
            entry.updates += 1
            await scheduler.sleep(1.0)

    entry = registry.start(scheduler, "ticker", tick)
    scheduler.run(scheduler.sleep(3.5))
    assert times == [0.0, 1.0, 2.0, 3.0]
    assert entry.running and entry.updates == 4
    entry.stop()
    scheduler.run(scheduler.sleep(0))
    assert not entry.running and entry.error is None
    assert registry.since(0) == [entry]


def test_background_failure_is_kept_for_its_owner() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock, step_ns=10_000_000)
    registry = BackgroundRegistry()

    async def broken(entry: Background) -> None:
        await scheduler.sleep(0.5)
        raise ConfigError("sensor rail out of range")

    entry = registry.start(scheduler, "broken", broken)
    scheduler.run(scheduler.sleep(1))
    assert not entry.running
    assert isinstance(entry.error, ConfigError)
    assert "out of range" in str(entry.error)


def test_calendar_time() -> None:
    assert environment.time_after("2027-03-21T06:00:00", 425.4) == "2027-03-21T06:07:05"
    assert environment.time_after("2027-12-31 23:59:59.50", 1) == "2028-01-01T00:00:00"
    assert environment.seconds_between("2027-03-21T06:00:00", "2027-03-21 06:10:03.25") == 603.25
    assert environment.date_fields("2027-03-21T06:07:05.00") == {
        "year": 2027,
        "month": 3,
        "day": 21,
        "hour": 6,
        "minute": 7,
        "second": 5,
    }
    with pytest.raises(ConfigError, match="not a time"):
        environment.parse_time("21/03/2027")
    with pytest.raises(ConfigError, match=r"month must be in 1\.\.12"):
        environment.parse_time("2027-13-01T00:00:00")
