"""Tests for the actions now: every device resolved from one situation."""

import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

from typing import Any
from unittest.mock import patch

import pytest

from custom_components.energy_planner.planner import (
    actions,
    heat_pump,
    manual,
    settings,
)
from custom_components.energy_planner.planner.tariff import (
    TARIFF_PRESETS,
    tariff_from_dict,
)

from .plans import battery_settings, make_plan

TZ = ZoneInfo("Europe/Stockholm")
NOW = dt.datetime(2026, 10, 7, 21, 5, tzinfo=TZ)
QUARTER = dt.datetime(2026, 10, 7, 21, 0, tzinfo=TZ)
STARTS = [QUARTER + dt.timedelta(minutes=15 * i) for i in range(4)]
# A plan with the spot price now, made an hour ago: too old to follow
OLD_PLAN = make_plan(QUARTER, spot=[500.0], updated=QUARTER - dt.timedelta(hours=1))
TARIFF = tariff_from_dict(TARIFF_PRESETS["tekniska_verken_alternativ"])


BASE = actions.Situation(
    now=NOW,
    plan=OLD_PLAN,
    manual_slots=[],
    enabled=True,
    battery=battery_settings(reserve_soc=0.0),
    heat_pump=settings.heat_pump({}),
    tariff=TARIFF,
    spot=500.0,
    heat_pump_loss=None,
    soc=50.0,
    voltage=500.0,
    car_status=None,
    outdoor=None,
    indoor=None,
)


def situation(plan=OLD_PLAN, **changes) -> actions.Situation:
    """Return a situation at NOW: default settings, 50 %, 500 V, no car, no slots."""
    return dataclasses.replace(BASE, plan=plan, **changes)


@dataclasses.dataclass(frozen=True)
class Resolved:
    """The actions of a situation where nothing failed."""

    battery: dict[str, Any]
    car: dict[str, Any]
    heat_pump: dict[str, Any]
    heat_pump_economy: dict[str, Any]
    mode: str | None


def resolve(at: actions.Situation) -> Resolved:
    """Return the actions, checking that each resolved."""
    result = actions.resolve(at)
    assert not result.errors
    assert result.battery is not None
    assert result.car is not None
    assert result.heat_pump is not None
    assert result.heat_pump_economy is not None
    return Resolved(
        result.battery,
        result.car,
        result.heat_pump,
        result.heat_pump_economy,
        result.mode,
    )


def test_economy_uses_the_live_inputs():
    """Test the outdoor temperature, the price now and the tariff's day fee."""
    economy = resolve(situation(outdoor=0.0)).heat_pump_economy
    assert economy["district_heating_price"] == 76.7
    assert economy["cop"] == 3.0
    assert economy["spot"] == 50.0
    # 62.5 spot + 45 energy tax + 18 day transfer fee
    assert economy["electricity_price"] == pytest.approx(125.5)
    assert economy["export_share"] == 0.0


def test_economy_planned_export():
    """Test that the plan's export this quarter, even an old plan's, is cheap."""
    plan = make_plan(
        QUARTER,
        spot=[500.0],
        grid_export_kw=[2.0],
        updated=QUARTER - dt.timedelta(hours=1),
    )
    economy = resolve(situation(plan, outdoor=10.0)).heat_pump_economy
    assert economy["export_share"] == 1.0


def test_economy_missing_inputs():
    """Test that without an outdoor temperature only the district price is given."""
    economy = resolve(situation()).heat_pump_economy
    assert economy == {"district_heating_price": 76.7, "outdoor_temperature": None}


def test_heat_pump_follows_the_plan():
    """Test the plan's flag now and when the run ends."""
    plan = make_plan(QUARTER, heat_pump=[True, True, False, False])
    action = resolve(situation(plan)).heat_pump
    assert (action["state"], action["source"]) == ("on", "plan")
    assert action["until"] == STARTS[2].isoformat()


def test_heat_pump_without_heat_pump_power():
    """Test a plan made with heat_pump_power 0: no marginal prices, always off."""
    plan = make_plan(QUARTER, heat_pump=[False] * 4, marginal=[])
    action = resolve(situation(plan)).heat_pump
    assert action["state"] == "off"
    assert action["marginal"] is None
    assert action["until"] == (STARTS[-1] + dt.timedelta(minutes=15)).isoformat()


def test_heat_pump_falls_back_to_the_economy_now():
    """Test that without a fresh plan the saving now and the heating limit decide."""
    action = resolve(situation(outdoor=5.0)).heat_pump
    assert (action["state"], action["source"]) == ("on", "fallback")
    action = resolve(situation(outdoor=16.0)).heat_pump
    assert action["state"] == "off"
    # A failed plan (e.g. no battery SOC) still leaves the fallback
    action = resolve(situation(None, outdoor=5.0)).heat_pump
    assert action["source"] == "fallback"


def test_heat_pump_without_prices_decides_nothing():
    """Test that missing prices give no decision, so the heat pump is left alone."""
    action = resolve(situation(outdoor=5.0, spot=None)).heat_pump
    assert (action["state"], action["source"]) == (None, "none")


def test_a_manual_slot_covering_now_wins():
    """Test that the slot running now, not an ended one, drives the battery."""
    plan = make_plan(QUARTER, ["sell"] * 4)
    slots = [
        manual.make(QUARTER - dt.timedelta(hours=1), QUARTER, "pause"),
        manual.make(QUARTER, STARTS[2], "charge", 80),
    ]
    resolved = resolve(situation(plan, manual_slots=slots))
    assert (resolved.battery["state"], resolved.battery["source"]) == (
        "charge",
        "manual",
    )
    assert resolved.battery["soc"] == 80
    # The plan sensor still shows the plan
    assert resolved.mode == "sell"


def test_missing_voltage_gives_no_current():
    """Test that a missing voltage reading gives 0 A, not a division by 0."""
    plan = make_plan(QUARTER, ["charge"] * 4, soc=[55.0, 60.0, 65.0, 70.0])
    battery = resolve(situation(plan, voltage=None)).battery
    assert (battery["state"], battery["current_a"]) == ("charge", 0)


def test_every_device_falls_back_together_from_a_stale_plan():
    """Test that a stale plan is followed by no device, and each says why."""
    stale = make_plan(
        QUARTER,
        ["charge"] * 4,
        heat_pump=[True] * 4,
        ev_power_kw=[11.0] * 4,
        updated=QUARTER - dt.timedelta(hours=1),
    )
    resolved = resolve(situation(stale, car_status="connected_charging", outdoor=5.0))
    for action in (resolved.battery, resolved.car, resolved.heat_pump):
        assert (action["source"], action["reason"]) == ("fallback", "plan is old")
    assert resolved.battery["state"] == "discharge"
    # The car charges as without the planner, and its need is not published
    assert (resolved.car["state"], resolved.car["need_kwh"]) == ("charge", None)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (STARTS[1] - dt.timedelta(seconds=1), ("charge", "charge", "on", "charge")),
        (STARTS[1], ("self_use", "discharge", "off", "wait")),
    ],
)
def test_actions_agree_at_a_quarter_boundary(now, expected):
    """Test that every action and the plan's mode come from the same quarter."""
    plan = make_plan(
        QUARTER,
        ["charge", "self_use"],
        soc=[60.0, 60.0],
        heat_pump=[True, False],
        ev_power_kw=[11.0, 0.0],
    )
    resolved = resolve(situation(plan, now=now, car_status="connected_charging"))
    assert (
        resolved.mode,
        resolved.battery["state"],
        resolved.heat_pump["state"],
        resolved.car["state"],
    ) == expected


def test_planner_off_still_follows_a_manual_slot():
    """Test that with the planner off only a manual slot moves the battery."""
    plan = make_plan(QUARTER, ["charge"] * 4)
    off = situation(plan, enabled=False)
    assert resolve(off).battery["source"] == "none"
    slot = manual.make(QUARTER, STARTS[2], "sell", 30)
    resolved = resolve(dataclasses.replace(off, manual_slots=[slot]))
    assert (resolved.battery["state"], resolved.battery["source"]) == (
        "sell",
        "manual",
    )


def test_a_heat_pump_error_leaves_the_battery_and_the_car():
    """Test that a failing heat pump economy only takes the heat pump's fallback."""
    plan = make_plan(QUARTER, ["charge"] * 4, ev_power_kw=[11.0] * 4)
    fresh = situation(plan, car_status="connected_charging", outdoor=5.0)
    with patch.object(heat_pump, "evaluate", side_effect=ValueError("broken")):
        resolved = actions.resolve(fresh)
        # Without a plan the heat pump has no decision
        unplanned = actions.resolve(dataclasses.replace(fresh, plan=None)).heat_pump
    assert resolved.heat_pump_economy is None
    assert set(resolved.errors) == {"heat_pump_economy"}
    assert resolved.heat_pump is not None
    assert resolved.heat_pump["source"] == "plan"
    working = resolve(fresh)
    assert (resolved.battery, resolved.car) == (working.battery, working.car)
    assert unplanned is not None
    assert (unplanned["state"], unplanned["source"]) == (None, "none")


def test_a_failing_action_does_not_take_the_others():
    """Test that each action resolves on its own."""
    plan = make_plan(QUARTER, ["charge"] * 4, ev_power_kw=[11.0] * 4)
    with patch.object(actions, "battery_action", side_effect=IndexError("short")):
        resolved = actions.resolve(situation(plan, car_status="connected_charging"))
    assert resolved.battery is None
    assert isinstance(resolved.errors["battery"], IndexError)
    assert resolved.car is not None
    assert resolved.car["state"] == "charge"
    assert resolved.mode == "charge"
