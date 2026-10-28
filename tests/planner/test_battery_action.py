"""Tests for the battery action: the current slot or the battery plan."""

import dataclasses
import datetime as dt
import importlib
from zoneinfo import ZoneInfo

import pytest

from .plans import battery_settings, make_plan

action = importlib.import_module(
    "custom_components.energy_planner.planner.battery_action"
)
manual = importlib.import_module("custom_components.energy_planner.planner.manual")

TZ = ZoneInfo("Europe/Stockholm")
NOW = dt.datetime(2026, 10, 7, 22, 5, tzinfo=TZ)
START = dt.datetime(2026, 10, 7, 22, 0, tzinfo=TZ)
STARTS = [START + dt.timedelta(minutes=15 * i) for i in range(8)]
BATTERY = battery_settings(
    capacity_kwh=20.0,
    shutdown_soc=15.0,
    reserve_soc=0.0,
    max_soc=90.0,
    max_charge_a=20.0,
    max_discharge_a=18.0,
)


def resolve(slot, planned, now=NOW, battery=BATTERY, soc=50.0, **kwargs):
    """Return the action at 500 V and `soc`."""
    return action.resolve(slot, planned, now, battery, soc, 500.0, **kwargs)


def plan(modes, soc, targets=None, updated=START, peak_levels=None):
    """Return a plan from START."""
    return make_plan(
        START,
        modes,
        soc=soc,
        targets=targets,
        peak_levels=peak_levels,
        updated=updated,
    )


def test_charge_follows_the_plan():
    """Test the target SOC, the current for the run and its end."""
    charge = plan(
        ["charge", "charge", "charge", "self_use"],
        [52.5, 55.0, 57.5, 57.0],
        [4.0, 4.0, 4.0, None],
    )
    result = resolve(None, charge)
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
    result = resolve(None, sell)
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
    """Test the action state and SOC of the modes without a current."""
    result = resolve(None, plan([mode], [50.0]))
    assert (result["state"], result["soc"], result["current_a"]) == (state, soc, 0)


RESERVE = dataclasses.replace(BATTERY, reserve_soc=20.0)


@pytest.mark.parametrize("mode", ["self_use", "sell_excess", "hold"])
def test_reserve_only_shaves_peaks(mode):
    """Test that at the reserve the battery holds and shaves to the power charge."""
    planned = plan([mode], [20.0], [1.0], peak_levels=[3.5])
    result = resolve(None, planned, battery=RESERVE, soc=20.0)
    assert (result["state"], result["mode"], result["soc"]) == ("pause", "hold", 20)
    assert result["import_target_kw"] == 3.5
    assert result["reserve"]


def test_reserve_without_power_charge_keeps_the_battery():
    """Test that without a power charge the reserve is just kept."""
    planned = plan(["self_use"], [20.0], peak_levels=[None])
    result = resolve(None, planned, battery=RESERVE, soc=20.0)
    assert (result["state"], result["import_target_kw"]) == ("pause", None)


def test_above_the_reserve_the_plan_is_followed():
    """Test that self-use runs while the battery is above the reserve."""
    planned = plan(["self_use"], [20.0], peak_levels=[3.5])
    result = resolve(None, planned, battery=RESERVE, soc=21.0)
    assert result["state"] == "discharge"
    sell_excess = resolve(
        None, plan(["sell_excess"], [50.0]), battery=RESERVE, soc=21.0
    )
    assert sell_excess["soc"] == 20


def test_missing_soc_is_not_an_empty_battery():
    """Test that without a SOC reading the plan's expected SOC is used."""
    planned = plan(["self_use"], [40.0], peak_levels=[3.5])
    result = resolve(None, planned, battery=RESERVE, soc=None)
    assert result["state"] == "discharge"
    assert "reserve" not in result
    # Expected at 22:05: 50 -> 60 % a third through the quarter, 53.3 %; the
    # 6.7 % left is 1.33 kWh in 10 minutes: 8 kW, 16 A at 500 V
    charge = plan(["charge"], [60.0], [4.0])
    result = resolve(None, charge, battery=RESERVE, soc=None)
    assert (result["soc"], result["current_a"]) == (60, 16)


def test_old_plan_falls_back_to_self_use():
    """Test that a plan not updated for over half an hour is not followed."""
    old = plan(["charge"], [60.0], updated=START - dt.timedelta(hours=1))
    result = resolve(None, old)
    assert (result["state"], result["source"]) == ("discharge", "fallback")


def test_plan_not_covering_now():
    """Test the fallback when the plan has ended."""
    later = NOW + dt.timedelta(hours=3)
    result = resolve(None, plan(["charge"], [60.0]), later)
    assert (result["state"], result["source"]) == ("discharge", "fallback")


def test_manual_slot_wins():
    """Test that a manual slot overrides the plan, at its target and highest current."""
    slot = manual.ManualSlot(START, STARTS[4], "charge", 80)
    result = resolve(slot, plan(["sell"], [20.0]))
    assert result["state"] == "charge"
    assert result["source"] == "manual"
    assert (result["soc"], result["current_a"]) == (80, 20)
    assert result["until"] == STARTS[4].isoformat()


@pytest.mark.parametrize(
    ("state", "soc", "expected"),
    [
        ("charge", None, 90),  # the max SOC
        ("sell", None, 20),  # the reserve
        ("sell", 5, 15),  # not below the shutdown SOC
        ("pause", 40, 90),  # keeps up to the max SOC
        ("sell-excess", None, 20),  # exports down to the reserve
        ("discharge", 40, None),
    ],
)
def test_manual_slot_soc(state, soc, expected):
    """Test the action's SOC for each manual state, without a plan."""
    battery = dataclasses.replace(BATTERY, reserve_soc=20.0)
    slot = manual.ManualSlot(START, STARTS[4], state, soc)
    result = resolve(slot, None, battery=battery, enabled=False)
    assert (result["state"], result["source"], result["soc"]) == (
        state,
        "manual",
        expected,
    )


def test_quarter_of_a_removed_manual_slot_is_not_followed():
    """Test that a plan made around a slot since removed is not followed there."""
    planned = dataclasses.replace(
        plan(["sell", "self_use"], [40.0, 40.0]), manual=("sell", None)
    )
    result = resolve(None, planned)
    assert (result["source"], result["reason"]) == ("fallback", "manual slot removed")


def test_charge_before_a_manual_slot_ends_where_it_starts():
    """Test that the plan's charge is not spread into a following manual charge."""
    planned = dataclasses.replace(
        plan(["charge"] * 4, [55.0, 60.0, 75.0, 90.0], [4.0] * 4),
        manual=(None, None, "charge", "charge"),
    )
    result = resolve(None, planned)
    # Up to 60 % by 22:30 (two quarters), not 90 % by 23:00
    assert result["until"] == STARTS[2].isoformat()
    assert result["soc"] == 60
    # 10 % of 20 kWh = 2 kWh in 25 minutes: 4.8 kW, 10 A at 500 V
    assert result["current_a"] == 10


def test_planner_off_is_self_use():
    """Test that with the planner off the plan is not followed."""
    result = resolve(None, plan(["charge"], [60.0]), enabled=False)
    assert (result["state"], result["source"]) == ("discharge", "none")


def test_battery_current_in_the_repeated_hour():
    """Test the time left to the end of a run across the repeated hour."""
    # 02:20 CEST to 02:00 CET is 40 min, though the wall clock goes back
    now = dt.datetime(2026, 10, 25, 2, 20, tzinfo=TZ)
    until = dt.datetime(2026, 10, 25, 2, 0, tzinfo=TZ, fold=1)
    # 2 kWh in 40 min at 500 V: 6 A
    assert action._current(2.0, until, now, 500.0, 100) == 6
