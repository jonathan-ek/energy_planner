"""Tests for the comfort schedules."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.comfort import schedule

TZ = ZoneInfo("Europe/Stockholm")
THURSDAY = dt.date(2026, 10, 8)
LIVING = schedule.DEFAULT_PROFILES["living"]["workday"]


@pytest.mark.parametrize(
    ("time", "expected"),
    [
        (dt.time(3, 0), 18.0),
        (dt.time(6, 0), 19.5),
        (dt.time(12, 0), 18.0),
        (dt.time(15, 30), 22.0),
        # Ramps from 22 at 15:30 to 18 at 22:00
        (dt.time(18, 45), 20.0),
        (dt.time(23, 0), 18.0),
    ],
)
def test_temperature_at(time, expected):
    """Test steps and the gradual cooling in the evening."""
    assert schedule.temperature_at(LIVING, time) == pytest.approx(expected)


def test_ramp_over_midnight():
    """Test a ramp from the day's last point to the first."""
    profile = (
        schedule.Point(dt.time(6), 20.0),
        schedule.Point(dt.time(22), 20.0, ramp=True),
    )
    assert schedule.temperature_at(profile, dt.time(2)) == pytest.approx(20.0)
    profile = (
        schedule.Point(dt.time(0), 18.0),
        schedule.Point(dt.time(20), 22.0, ramp=True),
    )
    assert schedule.temperature_at(profile, dt.time(22)) == pytest.approx(20.0)


def event(day, summary, days=1):
    """Return an all-day event."""
    start = dt.datetime.combine(day, dt.time(0), TZ)
    return schedule.Event(start, start + dt.timedelta(days=days), summary)


@pytest.mark.parametrize(
    ("day", "events", "override", "home", "expected"),
    [
        (THURSDAY, [], None, False, "workday"),
        (THURSDAY, [], None, True, "work_from_home"),
        (dt.date(2026, 10, 10), [], None, False, "offday"),  # Saturday
        (dt.date(2026, 12, 24), [], None, False, "offday"),
        (THURSDAY, [event(THURSDAY, "Jobbar hemifrån")], None, False, "work_from_home"),
        (
            dt.date(2026, 10, 10),
            [event(dt.date(2026, 10, 9), "Borta i helgen", days=3)],
            None,
            False,
            "away",
        ),
        (THURSDAY, [event(THURSDAY, "Borta")], "workday", True, "workday"),
        (THURSDAY, [event(THURSDAY, "Tandläkare")], None, False, "workday"),
    ],
)
def test_day_kind(day, events, override, home, expected):
    """Test the override, calendar words, offdays and presence."""
    assert schedule.day_kind(day, events, override, home, TZ) == expected


def test_targets_follow_the_day_kind():
    """Test that each start uses its own day's profile."""
    starts = [
        dt.datetime(2026, 10, 8, 12, tzinfo=TZ),
        dt.datetime(2026, 10, 9, 12, tzinfo=TZ),
    ]
    kinds = {THURSDAY: "workday", dt.date(2026, 10, 9): "away"}
    assert schedule.targets(starts, "living", kinds) == [18.0, 16.0]
    assert schedule.targets(starts[:1], "offices", {THURSDAY: "work_from_home"}) == [
        20.0
    ]
