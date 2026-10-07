"""Tests for the battery action: the current slot or the battery plan."""

import datetime as dt
import importlib
from zoneinfo import ZoneInfo

import pytest

action = importlib.import_module(
    "custom_components.energy_planner.planner.battery_action"
)

TZ = ZoneInfo("Europe/Stockholm")
NOW = dt.datetime(2026, 10, 7, 22, 5, tzinfo=TZ)
START = dt.datetime(2026, 10, 7, 22, 0, tzinfo=TZ)
STARTS = [START + dt.timedelta(minutes=15 * i) for i in range(8)]
BATTERY = action.BatteryState(
    soc=50.0,
    voltage=500.0,
    capacity_kwh=20.0,
    min_soc=15.0,
    max_soc=90.0,
    max_charge_a=20.0,
    max_discharge_a=18.0,
)


def plan(modes, soc, targets=None, updated=START):
    """Return a plan dict as battery_plan stores it."""
    return {
        "updated": updated.isoformat(),
        "starts": [s.isoformat() for s in STARTS[: len(modes)]],
        "modes": modes,
        "soc": soc,
        "targets": targets or [None] * len(modes),
    }


def auto(start=START, end=None):
    """Return an active auto slot."""
    return action.Slot("auto", True, start, end, 50)


def test_charge_follows_the_plan():
    """Test the target SOC, the current for the run and its end."""
    charge = plan(
        ["charge", "charge", "charge", "self_use"],
        [52.5, 55.0, 57.5, 57.0],
        [4.0, 4.0, 4.0, None],
    )
    result = action.resolve(auto(), charge, NOW, BATTERY)
    assert result["state"] == "charge"
    assert result["source"] == "plan"
    assert result["soc"] == 58
    assert result["until"] == STARTS[3].isoformat()
    # 7.5 % of 20 kWh = 1.5 kWh in 40 minutes: 2.25 kW, 4.5 A at 500 V
    assert result["current_a"] == 5
    assert result["import_target_kw"] == 4.0


def test_sell_current_is_limited():
    """Test that selling never asks for more than the max discharge current."""
    sell = plan(["sell", "self_use"], [20.0, 20.0])
    result = action.resolve(auto(), sell, NOW, BATTERY)
    assert result["state"] == "sell"
    assert result["soc"] == 20
    assert result["current_a"] == 18


@pytest.mark.parametrize(
    ("mode", "state", "soc"),
    [
        ("self_use", "discharge", None),
        ("hold", "pause", 50),
        ("sell_excess", "sell-excess", 15),
        ("discard_excess", "discard-excess", None),
    ],
)
def test_other_modes(mode, state, soc):
    """Test the slot state and SOC of the modes without a current."""
    result = action.resolve(auto(), plan([mode], [50.0]), NOW, BATTERY)
    assert (result["state"], result["soc"], result["current_a"]) == (state, soc, 0)


def test_old_plan_falls_back_to_self_use():
    """Test that a plan not updated for over half an hour is not followed."""
    old = plan(["charge"], [60.0], updated=START - dt.timedelta(hours=1))
    result = action.resolve(auto(), old, NOW, BATTERY)
    assert (result["state"], result["source"]) == ("discharge", "fallback")


def test_plan_not_covering_now():
    """Test the fallback when the plan has ended."""
    later = NOW + dt.timedelta(hours=3)
    result = action.resolve(auto(), plan(["charge"], [60.0]), later, BATTERY)
    assert (result["state"], result["source"]) == ("discharge", "fallback")


def test_manual_slot_wins():
    """Test that a slot with a real state overrides the plan."""
    slot = action.Slot("charge", True, START, STARTS[4], 80)
    result = action.resolve(slot, plan(["sell"], [20.0]), NOW, BATTERY)
    assert result["state"] == "charge"
    assert result["source"] == "slot"
    assert (result["soc"], result["current_a"]) == (80, 20)
    assert result["until"] == STARTS[4].isoformat()


@pytest.mark.parametrize(
    "slot",
    [
        None,
        action.Slot("off", True, START, None, 50),
        action.Slot("auto", False, START, None, 50),
        action.Slot("auto", True, NOW + dt.timedelta(minutes=5), None, 50),
    ],
)
def test_no_slot_is_self_use(slot):
    """Test that without a running slot the battery is in self-use."""
    result = action.resolve(slot, plan(["charge"], [60.0]), NOW, BATTERY)
    assert (result["state"], result["source"]) == ("discharge", "none")
