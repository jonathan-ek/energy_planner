"""Tests for the plan: what it says at a moment, staleness and its attributes."""

import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.planner import battery_action, ev, heat_pump
from custom_components.energy_planner.planner.plan import follow
from custom_components.energy_planner.planner.timeline import QUARTER, Timeline

from .plans import battery_settings, make_plan

TZ = ZoneInfo("Europe/Stockholm")
START = dt.datetime(2026, 10, 7, 22, 0, tzinfo=TZ)
PLAN = make_plan(
    START,
    ["charge", "charge", "self_use", "charge"],
    soc=[40.0, 60.0, 50.0, 70.0],
)


def at(minutes):
    """Return START plus minutes."""
    return START + dt.timedelta(minutes=minutes)


def test_index_at():
    """Test the quarter containing a moment, None before and after the plan."""
    assert PLAN.index_at(at(0)) == 0
    assert PLAN.index_at(at(14)) == 0
    assert PLAN.index_at(at(15)) == 1
    assert PLAN.index_at(at(59)) == 3
    assert PLAN.index_at(at(60)) is None
    assert PLAN.index_at(at(-1)) is None
    assert (PLAN.timeline.start, PLAN.timeline.end) == (START, at(60))


def test_value_at():
    """Test a series' value now, None outside the plan or past a short series."""
    assert PLAN.value_at(at(20), PLAN.modes) == "charge"
    assert PLAN.value_at(at(35), PLAN.soc) == 50.0
    assert PLAN.value_at(at(50), [True, True]) is None
    assert PLAN.value_at(at(70), PLAN.modes) is None


def test_run_at():
    """Test that a run extends over equal values from now, not back."""
    run = PLAN.run_at(at(5), PLAN.modes)
    assert run is not None
    assert (run.value, run.index, run.last, run.until) == ("charge", 0, 1, at(30))
    later = PLAN.run_at(at(20), PLAN.modes)
    assert later is not None
    assert (later.index, later.last) == (1, 1)
    # A derived series, and one shorter than the plan
    assert PLAN.run_at(at(50), [True, True]) is None
    assert PLAN.run_at(at(70), PLAN.modes) is None


def test_soc_at():
    """Test the SOC between the previous quarter's end and this one's."""
    # The first quarter: from the SOC when planned (5 min in) to its end SOC
    planned = dataclasses.replace(PLAN, soc_start=30.0, updated=at(5))
    assert planned.soc_at(at(5)) == 30.0
    assert planned.soc_at(at(10)) == pytest.approx(35.0)
    assert planned.soc_at(at(1)) == 30.0
    assert PLAN.soc_at(at(15)) == 40.0
    assert PLAN.soc_at(at(22.5)) == pytest.approx(50.0)
    assert PLAN.soc_at(at(60)) is None


def test_the_repeated_hour_when_dst_ends():
    """Test lookups and the run's end in a plan over the repeated hour."""
    # 01:30 CEST; quarters 2-5 are 02:00-02:45 CEST, 6-7 02:00-02:15 CET
    start = dt.datetime(2026, 10, 25, 1, 30, tzinfo=TZ)
    plan = make_plan(start, ["charge"] * 6 + ["sell"] * 2)
    second = dt.datetime(2026, 10, 25, 2, 20, tzinfo=TZ, fold=1)
    assert (plan.index_at(second.replace(fold=0)), plan.index_at(second)) == (3, 7)
    run = plan.run_at(start, plan.modes)
    assert run is not None
    assert run.until.isoformat() == "2026-10-25T02:00:00+01:00"


def test_staleness_from_when_the_plan_was_finished():
    """Test that a slow calculation does not count against the plan's age."""
    slow = dataclasses.replace(PLAN, finished=START + dt.timedelta(minutes=2))
    assert not slow.is_stale(START + dt.timedelta(minutes=31))
    assert PLAN.is_stale(START + dt.timedelta(minutes=31))


def test_follow_says_why_not():
    """Test the reasons a plan can not be followed."""
    assert follow(None, at(5), lambda p: p.modes) == (None, "no plan")
    assert follow(PLAN, at(25), lambda p: p.modes)[0] is not None
    old = PLAN.updated + dt.timedelta(hours=1)
    assert follow(PLAN, old, lambda p: p.modes) == (None, "plan is old")
    reason = follow(PLAN, at(5), lambda p: None)[1]
    assert reason == "plan does not cover now"


def test_attributes():
    """Test the published plan: ISO times, öre/kWh incl. VAT, the car's summary."""
    plan = make_plan(START, spot=[800.0, 200.0], ev_power_kw=[11.0, 0.0])
    attributes = plan.to_attributes()
    assert attributes["starts"] == [START.isoformat(), at(15).isoformat()]
    assert attributes["updated"] == attributes["finished"] == START.isoformat()
    assert attributes["prices"] == [100.0, 25.0]
    assert attributes["ev"]["power"] == [11.0, 0.0]
    assert attributes["ev"]["ready"] == (START + dt.timedelta(hours=8)).isoformat()
    assert attributes["target_kw"] is None


def test_every_action_has_the_common_keys():
    """Test that the battery, car and heat pump actions share their core keys."""
    plan = make_plan(START, ev_power_kw=[0.0] * 4, heat_pump=[True] * 4)
    battery = battery_settings()
    actions = [
        battery_action.resolve(None, plan, at(5), battery, 50.0, 500.0),
        ev.ev_action(plan, at(5), plugged=True),
        heat_pump.action(plan, at(5), {}, 5.0, 15.0),
    ]
    for action in actions:
        assert {"state", "source", "until", "reason", "plan_updated"} <= set(action)
        assert action["source"] == "plan"
        assert action["until"] == (START + 4 * QUARTER).isoformat()
        assert action["plan_updated"] == START.isoformat()
    # Old: each falls back and says why
    later = at(45) + dt.timedelta(hours=1)
    stale = dataclasses.replace(
        plan,
        timeline=Timeline.between(
            plan.timeline.start + dt.timedelta(hours=1),
            plan.timeline.end + dt.timedelta(hours=1),
            TZ,
        ),
    )
    for action in (
        battery_action.resolve(None, stale, later, battery, 50.0, 500.0),
        ev.ev_action(stale, later, plugged=True),
    ):
        assert (action["source"], action["reason"]) == ("fallback", "plan is old")
