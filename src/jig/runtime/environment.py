"""Environment inputs that change over mission time: profiles and the
background work that applies them.

A profile is a value as a function of the time since it started, in SI units:

* ``constant(value)``
* ``sine(mean, amplitude, period, phase = 0)``: phase in seconds
* ``ramp(start, end, over)``: linear from ``start`` to ``end``, then held
* ``table([[t, v], ...])``: linear between points, held before the first and
  after the last
* ``steps([[t, v], ...])``: each value holds from its time until the next one

Profiles add to each other and to numbers (``sine(...) + 0.05``). A board's
``drive(sensor, profile, every)`` applies one to a sensor input in the
background; the work belongs to the test or setup block that started it and
ends with it (:class:`Background`).

Calendar time, for a mission epoch and the boards' real-time clocks, is text
as the clocks print it (``2027-03-21T06:00:00``, a space for the T and a
fraction of a second allowed): ``time_after(start, seconds)``,
``seconds_between(earlier, later)`` and ``date_fields(time)``.
"""

from __future__ import annotations

import bisect
import datetime
import math
import re
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any

from jig.errors import ConfigError

Number = int | float


class Profile:
    """A value as a function of time (seconds since the profile started)."""

    # Plans keep a profile as one value (it is not unpacked into a record) and
    # may sample it: rate.at(5 s).
    dsl_members = frozenset({"at"})

    def at(self, t: float) -> float:
        raise NotImplementedError

    def __add__(self, other: Profile | Number) -> Profile:
        return Sum(self, as_profile(other))

    def __radd__(self, other: Profile | Number) -> Profile:
        return Sum(as_profile(other), self)

    def plain(self) -> str:
        return repr(self)


def as_profile(value: Profile | Number) -> Profile:
    if isinstance(value, Profile):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Constant(float(value))
    raise ConfigError(f"a profile combines with numbers and profiles, not {type(value).__name__}")


@dataclass(frozen=True)
class Constant(Profile):
    value: float

    def at(self, t: float) -> float:
        return self.value

    def __repr__(self) -> str:
        return f"constant({self.value:g})"


@dataclass(frozen=True)
class Sine(Profile):
    mean: float
    amplitude: float
    period: float
    phase: float = 0.0

    def __post_init__(self) -> None:
        if self.period <= 0:
            raise ConfigError("a sine profile needs a positive period")

    def at(self, t: float) -> float:
        return self.mean + self.amplitude * math.sin(2 * math.pi * (t + self.phase) / self.period)

    def __repr__(self) -> str:
        return f"sine({self.mean:g}, {self.amplitude:g}, {self.period:g} s)"


@dataclass(frozen=True)
class Table(Profile):
    """Linear between points; held before the first and after the last."""

    times: tuple[float, ...]
    values: tuple[float, ...]
    hold: bool = False  # steps: each value holds until the next point

    def __post_init__(self) -> None:
        if not self.times:
            raise ConfigError("a table profile needs at least one point")
        if any(b <= a for a, b in zip(self.times, self.times[1:], strict=False)):
            raise ConfigError("table profile times must increase")

    def at(self, t: float) -> float:
        index = bisect.bisect_right(self.times, t)
        if index == 0:
            return self.values[0]
        if index == len(self.times) or self.hold:
            return self.values[index - 1]
        t0, t1 = self.times[index - 1], self.times[index]
        v0, v1 = self.values[index - 1], self.values[index]
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)

    def __repr__(self) -> str:
        kind = "steps" if self.hold else "table"
        return f"{kind}({len(self.times)} points over {self.times[-1] - self.times[0]:g} s)"


@dataclass(frozen=True)
class Sum(Profile):
    left: Profile
    right: Profile

    def at(self, t: float) -> float:
        return self.left.at(t) + self.right.at(t)

    def __repr__(self) -> str:
        return f"{self.left!r} + {self.right!r}"


def points(value: Any, what: str) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """[[t, v], ...] as (times, values)."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ConfigError(f"{what}: expected a list of [time, value] pairs")
    times, values = [], []
    for pair in value:
        if not isinstance(pair, Sequence) or len(pair) != 2:
            raise ConfigError(f"{what}: {pair!r} is not a [time, value] pair")
        times.append(float(pair[0]))
        values.append(float(pair[1]))
    return tuple(times), tuple(values)


def ramp(start: Number, end: Number, over: Number) -> Profile:
    if over <= 0:
        raise ConfigError("a ramp needs a positive duration")
    return Table((0.0, float(over)), (float(start), float(end)))


def constructors() -> dict[str, Callable[..., Profile]]:
    """The profile functions a plan sees."""
    return {
        "constant": lambda value: Constant(float(value)),
        "sine": lambda mean, amplitude, period, phase=0.0: Sine(
            float(mean), float(amplitude), float(period), float(phase)
        ),
        "ramp": ramp,
        "table": lambda pairs: Table(*points(pairs, "table")),
        "steps": lambda pairs: Table(*points(pairs, "steps"), hold=True),
    }


# --------------------------------------------------------- calendar time

_TIME = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?")


def parse_time(text: str) -> datetime.datetime:
    """A calendar time as a clock prints it (``2027-03-21T06:00:00.25``)."""
    found = _TIME.fullmatch(str(text).strip())
    if found is None:
        raise ConfigError(f"{text!r} is not a time like 2027-03-21T06:00:00")
    year, month, day, hour, minute, second = (int(found.group(i)) for i in range(1, 7))
    micro = int(((found.group(7) or "") + "000000")[:6])
    try:
        return datetime.datetime(year, month, day, hour, minute, second, micro)
    except ValueError as exc:
        raise ConfigError(f"{text!r}: {exc}") from None


def time_after(start: str, seconds: Number) -> str:
    """The time ``seconds`` after ``start``, to the nearest second."""
    moment = parse_time(start) + datetime.timedelta(seconds=round(float(seconds)))
    return moment.replace(microsecond=0).isoformat()


def seconds_between(earlier: str, later: str) -> float:
    return (parse_time(later) - parse_time(earlier)).total_seconds()


def date_fields(text: str) -> dict[str, int]:
    """Year, month, day, hour, minute and second, as a clock's set command takes them."""
    moment = parse_time(text)
    return {
        "year": moment.year,
        "month": moment.month,
        "day": moment.day,
        "hour": moment.hour,
        "minute": moment.minute,
        "second": moment.second,
    }


def time_functions() -> dict[str, Callable[..., Any]]:
    """The calendar-time functions a plan sees."""
    return {
        "time_after": time_after,
        "seconds_between": seconds_between,
        "date_fields": date_fields,
    }


# ------------------------------------------------------------- background


@dataclass
class Background:
    """Work a plan started that runs alongside it until stopped, or until the
    block that started it ends."""

    name: str
    task: Any  # jig.runtime.scheduler.Task
    started: float  # simulated seconds
    updates: int = 0
    error: BaseException | None = field(default=None, repr=False)
    reported: bool = False  # the error has failed a block

    @property
    def running(self) -> bool:
        return not self.task.done

    def stop(self) -> None:
        if not self.task.done:
            self.task.cancel()


class BackgroundRegistry:
    """The background work of one simulation, in start order."""

    def __init__(self) -> None:
        self.entries: list[Background] = []

    def mark(self) -> int:
        return len(self.entries)

    def since(self, mark: int) -> list[Background]:
        return self.entries[mark:]

    def start(
        self,
        scheduler: Any,
        name: str,
        body: Callable[[Background], Coroutine[Any, Any, None]],
    ) -> Background:
        holder: list[Background] = []

        async def run() -> None:
            entry = holder[0]
            try:
                await body(entry)
            except Exception as exc:  # reported by the block that owns the work
                entry.error = exc

        task = scheduler.spawn(run(), name)
        entry = Background(name, task, scheduler.now)
        holder.append(entry)
        self.entries.append(entry)
        return entry
