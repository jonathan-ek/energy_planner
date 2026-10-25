"""Tests for the car charging functions."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.planner import ev

from .plans import make_plan

TZ = ZoneInfo("Europe/Stockholm")
TODAY = dt.date(2026, 10, 10)


@pytest.mark.parametrize(
    ("state", "plugged"),
    [
        ("connected_charging", True),
        ("connected_finished", True),
        ("connected_requesting", True),
        ("disconnected", False),
        ("unavailable", False),
        (None, False),
    ],
)
def test_is_plugged(state, plugged):
    """Test the charger states that mean a car."""
    assert ev.is_plugged(state) == plugged


def test_charging_nights_run_noon_to_noon():
    """Test that evening and early morning charging count as one night."""
    day = dt.date(2026, 10, 5)
    hourly = {
        (day, 17): 11.0,
        (day + dt.timedelta(days=1), 4): 7.0,
        (day + dt.timedelta(days=1), 13): 0.5,  # too little for a night
    }
    assert ev.charging_nights(hourly) == {day: 18.0}


def test_need_from_history_largest_night():
    """Test the largest of the ended nights."""
    hourly = {
        (TODAY - dt.timedelta(days=i), 18): float(kwh)
        for i, kwh in zip(
            range(2, 12), [10, 12, 14, 16, 18, 20, 22, 24, 26, 40], strict=True
        )
    }
    # Last night (from yesterday) has not ended and is left out
    hourly[(TODAY - dt.timedelta(days=1), 18)] = 99.0
    assert ev.need_from_history(hourly, TODAY) == 40.0
    few = dict(list(hourly.items())[:4])
    assert ev.need_from_history(few, TODAY) is None


def test_need_from_soc():
    """Test the energy at the plug from the SOC to the charge limit."""
    assert ev.need_from_soc(50, 80, 75) == pytest.approx(0.3 * 75 / 0.9)
    assert ev.need_from_soc(85, 80, 75) == 0.0


def test_deadline_is_the_next_ready_time():
    """Test that the deadline is the coming morning's ready time."""
    evening = dt.datetime(2026, 10, 13, 17, 0, tzinfo=TZ)  # Tuesday
    starts = [evening + dt.timedelta(minutes=15 * i) for i in range(96)]
    index = ev.deadline_index(starts, dt.time(6, 30))
    assert starts[index] == dt.datetime(2026, 10, 14, 6, 30, tzinfo=TZ)
    early = [s - dt.timedelta(hours=15) for s in starts]  # from 02:00
    assert early[ev.deadline_index(early, dt.time(6, 30))].hour == 6
    assert ev.deadline_index(starts[:8], dt.time(6, 30)) == 8


def test_ready_on_working_days_only():
    """Test that weekends and holidays wait for the next working morning."""
    friday = dt.datetime(2026, 10, 9, 17, 0, tzinfo=TZ)
    monday = dt.datetime(2026, 10, 12, 6, 30, tzinfo=TZ)
    assert ev.ready_time(friday, dt.time(6, 30)) == monday
    # Saturday morning, before the ready time
    assert ev.ready_time(monday - dt.timedelta(days=2, hours=2), dt.time(6, 30)) == (
        monday
    )
    # Christmas Eve to Boxing Day are holidays; 2026-12-28 is a Monday
    assert ev.ready_time(
        dt.datetime(2026, 12, 23, 18, 0, tzinfo=TZ), dt.time(6, 30)
    ) == dt.datetime(2026, 12, 28, 6, 30, tzinfo=TZ)
    starts = [friday + dt.timedelta(minutes=15 * i) for i in range(96)]
    assert ev.deadline_index(starts, dt.time(6, 30)) == len(starts)


def test_departure_comes_first():
    """Test that a trip before the next working morning sets the deadline."""
    friday = dt.datetime(2026, 10, 9, 17, 0, tzinfo=TZ)
    trip = dt.datetime(2026, 10, 10, 9, 0, tzinfo=TZ)
    assert ev.ready_time(friday, dt.time(6, 30), trip) == trip
    starts = [friday + dt.timedelta(minutes=15 * i) for i in range(96)]
    assert starts[ev.deadline_index(starts, dt.time(6, 30), trip)] == trip
    # Past, or after the working morning: ignored
    monday = dt.datetime(2026, 10, 12, 6, 30, tzinfo=TZ)
    for departure in (friday - dt.timedelta(hours=1), monday + dt.timedelta(days=1)):
        assert ev.ready_time(friday, dt.time(6, 30), departure) == monday


def plan(updated, power):
    """Build a plan with a car power per quarter from 22:00."""
    start = dt.datetime(2026, 10, 10, 22, 0, tzinfo=TZ)
    return make_plan(start, ev_power_kw=power, updated=updated)


def test_action_follows_the_plan():
    """Test charge and wait from the plan's car power, and until when."""
    now = dt.datetime(2026, 10, 10, 22, 20, tzinfo=TZ)
    action = ev.ev_action(plan(now, [0.0, 0.0, 11.0, 11.0, 0.0]), now, plugged=True)
    assert action["state"] == "wait"
    assert action["source"] == "plan"
    assert action["until"] == dt.datetime(2026, 10, 10, 22, 30, tzinfo=TZ).isoformat()
    later = now + dt.timedelta(minutes=15)
    action = ev.ev_action(plan(now, [0.0, 0.0, 11.0, 11.0, 0.0]), later, plugged=True)
    assert action["state"] == "charge"
    assert action["power_kw"] == 11.0
    assert action["until"] == dt.datetime(2026, 10, 10, 23, 0, tzinfo=TZ).isoformat()


def test_action_falls_back_to_charging():
    """Test that an old or missing plan lets the car charge, unplugged: nothing."""
    now = dt.datetime(2026, 10, 10, 22, 20, tzinfo=TZ)
    old = plan(now - dt.timedelta(hours=1), [0.0] * 8)
    assert ev.ev_action(old, now, plugged=True)["state"] == "charge"
    assert ev.ev_action(old, now, plugged=True)["source"] == "fallback"
    assert ev.ev_action(None, now, plugged=True)["state"] == "charge"
    assert ev.ev_action(old, now, plugged=False)["state"] is None


def test_action_has_the_plans_need():
    """Test that the action carries the plan's need, also while unplugged."""
    now = dt.datetime(2026, 10, 10, 22, 20, tzinfo=TZ)
    planned = plan(now, [0.0, 11.0])
    for plugged in (True, False):
        action = ev.ev_action(planned, now, plugged=plugged)
        assert (action["need_kwh"], action["need_source"]) == (20.0, "history")
        assert action["planned_kwh"] == pytest.approx(2.75)
    assert ev.ev_action(None, now, plugged=True)["need_kwh"] is None
    # An old plan's need is not published as if it were current
    old = plan(now - dt.timedelta(hours=1), [0.0, 11.0])
    assert ev.ev_action(old, now, plugged=True)["need_kwh"] is None
