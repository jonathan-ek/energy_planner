"""Comfort schedules: the indoor temperature wanted per room group and moment.

Pure functions, no Home Assistant imports. Rooms are grouped (`GROUPS`): the living
areas (heated by district heating and the heat pump in the hall), the bedrooms and
the offices. Each group has a day profile per day kind (`DAY_KINDS`):

- `workday`: everyone leaves in the day.
- `offday`: weekends and Swedish holidays (`price_estimate.is_offday`).
- `work_from_home`: a workday with someone working at home, the offices are heated.
- `away`: nobody home, a low temperature all day.

A profile is a list of points (time, °C); the temperature holds until the next point,
or with `ramp` changes linearly to it (e.g. cooling gradually in the evening).
`day_kind` picks the kind for a date from a manual override, calendar events and
presence. The profiles are what the rooms should reach; when to start heating so they
do is thermal.py's job.
"""

from collections.abc import Iterable, Mapping, Sequence
import dataclasses
import datetime as dt

from ..planner.forecast import AWAY_WORDS
from ..planner.price_estimate import is_offday

GROUPS = ("living", "bedrooms", "offices")
DAY_KINDS = ("workday", "offday", "work_from_home", "away")
# Calendar event titles (lower case) that set the day kind, besides AWAY_WORDS
WORK_FROM_HOME_WORDS = ("hemifrån", "jobbar hemma", "work from home", "wfh")
# Someone home on a workday after this time means a work from home day
WORK_FROM_HOME_AFTER = dt.time(8, 30)


@dataclasses.dataclass(frozen=True)
class Point:
    """A temperature from a time of day; `ramp`: change linearly to the next."""

    time: dt.time
    temperature: float
    ramp: bool = False


Profile = Sequence[Point]


def _p(hour: int, minute: int, temperature: float, *, ramp: bool = False) -> Point:
    return Point(dt.time(hour, minute), temperature, ramp)


# Defaults until the settings exist; the living areas follow the owner's example
# (night 18, morning 19.5, day 18, afternoon 22 cooling gradually)
DEFAULT_PROFILES: dict[str, dict[str, Profile]] = {
    "living": {
        "workday": (
            _p(0, 0, 18),
            _p(6, 0, 19.5),
            _p(8, 30, 18),
            _p(15, 30, 22, ramp=True),
            _p(22, 0, 18),
        ),
        "work_from_home": (
            _p(0, 0, 18),
            _p(6, 0, 19.5),
            _p(8, 30, 19),
            _p(15, 30, 22, ramp=True),
            _p(22, 0, 18),
        ),
        "offday": (
            _p(0, 0, 18),
            _p(7, 30, 20),
            _p(15, 0, 22, ramp=True),
            _p(22, 30, 18),
        ),
        "away": (_p(0, 0, 16),),
    },
    "bedrooms": {
        "workday": (_p(0, 0, 18), _p(6, 0, 19), _p(8, 30, 18)),
        "work_from_home": (_p(0, 0, 18), _p(6, 0, 19), _p(8, 30, 18)),
        "offday": (_p(0, 0, 18), _p(7, 30, 19), _p(10, 0, 18)),
        "away": (_p(0, 0, 16),),
    },
    "offices": {
        "workday": (_p(0, 0, 17),),
        "work_from_home": (_p(0, 0, 17), _p(7, 30, 20), _p(17, 0, 17)),
        "offday": (_p(0, 0, 17),),
        "away": (_p(0, 0, 16),),
    },
}


@dataclasses.dataclass(frozen=True)
class Event:
    """A calendar event (timed or all-day) that may set the day kind."""

    start: dt.datetime
    end: dt.datetime
    summary: str


def _event_kind(event: Event) -> str | None:
    title = event.summary.lower()
    if any(word in title for word in AWAY_WORDS):
        return "away"
    if any(word in title for word in WORK_FROM_HOME_WORDS):
        return "work_from_home"
    return None


def day_kind(
    day: dt.date,
    events: Iterable[Event] = (),
    override: str | None = None,
    home_since_morning: bool = False,
    tz: dt.tzinfo | None = None,
) -> str:
    """Return the day kind for a date.

    In order: a manual `override` (a DAY_KINDS value), a calendar event covering
    noon of the day (away wins over work from home), an offday, a workday with
    someone home after WORK_FROM_HOME_AFTER (`home_since_morning`, only known for
    today), otherwise a workday.
    """
    if override in DAY_KINDS:
        return str(override)
    noon = dt.datetime.combine(day, dt.time(12), tz)
    kinds = {
        _event_kind(e) for e in events if e.start <= noon < e.end and _event_kind(e)
    }
    if "away" in kinds:
        return "away"
    if "work_from_home" in kinds:
        return "work_from_home"
    if is_offday(day):
        return "offday"
    if home_since_morning:
        return "work_from_home"
    return "workday"


def _seconds(time: dt.time) -> int:
    return time.hour * 3600 + time.minute * 60 + time.second


def temperature_at(profile: Profile, moment: dt.datetime | dt.time) -> float:
    """Return the profile's temperature at a time of day."""
    if not profile:
        raise ValueError("empty profile")
    time = moment.time() if isinstance(moment, dt.datetime) else moment
    points = sorted(profile, key=lambda p: _seconds(p.time))
    now = _seconds(time)
    # The point in force: the last one at or before now, else the day's last
    index = max(
        (i for i, p in enumerate(points) if _seconds(p.time) <= now), default=-1
    )
    point = points[index]
    if not point.ramp:
        return point.temperature
    following = points[(index + 1) % len(points)]
    start, end = _seconds(point.time), _seconds(following.time)
    if end <= start:
        end += 86400
    if now < start:
        now += 86400
    share = (now - start) / (end - start)
    return point.temperature + (following.temperature - point.temperature) * share


def targets(
    starts: Sequence[dt.datetime],
    group: str,
    kinds: Mapping[dt.date, str],
    profiles: Mapping[str, Mapping[str, Profile]] = DEFAULT_PROFILES,
) -> list[float]:
    """Return the group's temperature at each start (local), by the day's kind."""
    result = []
    for start in starts:
        kind = kinds.get(start.date(), "workday")
        result.append(temperature_at(profiles[group][kind], start))
    return result
