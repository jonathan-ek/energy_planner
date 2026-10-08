"""Tests for the heat pump economy and its glue."""

import datetime as dt
import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.const import DOMAIN

heat_pump = importlib.import_module(
    "custom_components.energy_planner.planner.heat_pump"
)
heating = importlib.import_module("custom_components.energy_planner.planner.heating")

TZ = ZoneInfo("Europe/Stockholm")
CURVE = heat_pump.COP_CURVES["msz_ap42"]
TV = heat_pump.DISTRICT_HEATING_PRESETS["tekniska_verken_2026"]


@pytest.mark.parametrize(
    ("outdoor", "expected"),
    [(-16.0, None), (-15.0, 3.0), (0.0, 3.0), (7.0, 3.62), (13.5, 3.955), (30.0, 4.05)],
)
def test_cop(outdoor, expected):
    """Test the interpolation, and None below the operating range."""
    assert heat_pump.cop(CURVE, outdoor) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("month", "price"), [(1, 99.7), (2, 99.7), (3, 76.7), (10, 76.7), (7, 13.0)]
)
def test_district_heating_price(month, price):
    """Test the season prices."""
    moment = dt.datetime(2026, month, 15, 12, tzinfo=TZ)
    assert heat_pump.district_heating_price(TV, moment) == price


def test_heat_pump_pays_in_autumn():
    """Test bought electricity at 50 öre spot against 76.7 öre district heating."""
    result = heat_pump.evaluate(500.0, 63.0, 5.1, 0.0, CURVE, 76.7)
    # 62.5 + 63 öre electricity / COP 3
    assert result["electricity_price"] == pytest.approx(125.5)
    assert result["heat_pump_heat_price"] == pytest.approx(41.8)
    assert result["saving"] == pytest.approx(34.9)
    # (76.7 * 3 - 63) / 1.25
    assert result["break_even_spot"] == pytest.approx(133.7)


def test_heat_pump_does_not_pay_in_summer():
    """Test that 13 öre district heating beats bought electricity."""
    result = heat_pump.evaluate(100.0, 63.0, 5.1, 20.0, CURVE, 13.0)
    assert result["saving"] < 0
    assert result["break_even_spot"] < 0


def test_exported_electricity_is_cheap():
    """Test that electricity that would be exported only costs the export price."""
    result = heat_pump.evaluate(100.0, 63.0, 5.1, 20.0, CURVE, 13.0, 1.0)
    assert result["electricity_price"] == pytest.approx(15.1)
    assert result["saving"] > 0


def test_too_cold():
    """Test that below the operating range there is no COP and no saving."""
    result = heat_pump.evaluate(500.0, 63.0, 5.1, -20.0, CURVE, 99.7)
    assert result["cop"] is None
    assert result["saving"] is None


NOW = dt.datetime(2026, 10, 7, 21, 5, tzinfo=TZ)
QUARTER = dt.datetime(2026, 10, 7, 21, 0, tzinfo=TZ)


def mock_hass(states, plan=None):
    """Return a mock hass with prices and the given sensor states."""
    hass = MagicMock()
    hass.states.get = lambda entity_id: (
        SimpleNamespace(state=states[entity_id]) if entity_id in states else None
    )
    hass.data = {
        DOMAIN: {
            "options": {"tariff_preset": "tekniska_verken_alternativ"},
            "config": {},
            "prices": {QUARTER: 500.0},
            "plan": plan or {},
        }
    }
    return hass


def test_glue_uses_the_live_inputs():
    """Test the outdoor sensor, the price now and the tariff's day fee."""
    hass = mock_hass({"sensor.gw1100a_outdoor_temperature": "0.0"})
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        result = heating.heat_pump_economy(hass)
    assert result["district_heating_price"] == 76.7
    assert result["cop"] == 3.0
    assert result["spot"] == 50.0
    # 62.5 spot + 45 energy tax + 18 day transfer fee
    assert result["electricity_price"] == pytest.approx(125.5)
    assert result["export_share"] == 0.0


def test_glue_planned_export():
    """Test that the plan's export in this quarter makes the electricity cheaper."""
    plan = {"starts": [QUARTER.isoformat()], "grid_export": [2.0]}
    hass = mock_hass({"sensor.gw1100a_outdoor_temperature": "10"}, plan)
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        result = heating.heat_pump_economy(hass)
    assert result["export_share"] == 1.0


def test_glue_missing_inputs():
    """Test that without an outdoor temperature only the district price is given."""
    hass = mock_hass({})
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        result = heating.heat_pump_economy(hass)
    assert result == {"district_heating_price": 76.7, "outdoor_temperature": None}


def test_schedule_runs_where_heat_is_cheaper():
    """Test the rule: district heating minus electricity / COP at least the margin."""
    # COP 3, district heating 76.7: pays up to 215 öre/kWh electricity with margin 5
    marginal = [100.0, 214.0, 216.0, 300.0, 300.0, 100.0, 100.0, 100.0]
    on = heat_pump.schedule(marginal, [3.0] * 8, [76.7] * 8, [True] * 8, 5.0)
    # 216 is just too much, but the one-quarter gap between runs is filled
    assert on == [True, True, False, False, False, True, True, True]


def test_schedule_needs_heating_and_a_cop():
    """Test that it does not run without a heat need or below the operating range."""
    heating = [True, True, False, False, True, True]
    cops = [3.0, 3.0, 3.0, 3.0, None, None]
    on = heat_pump.schedule([0.0] * 6, cops, [76.7] * 6, heating, 5.0)
    assert on == [True, True, False, False, False, False]


def test_schedule_removes_short_gaps_and_runs():
    """Test that a single-quarter gap is filled and a single-quarter run dropped."""
    cheap, dear = 0.0, 1000.0
    marginal = [cheap, cheap, dear, cheap, cheap, dear, dear, cheap, dear, dear, cheap]
    on = heat_pump.schedule(marginal, [3.0] * 11, [76.7] * 11, [True] * 11, 5.0)
    # Gap at 2 filled, run at 7 dropped, the run at the end is kept
    assert on == [True] * 5 + [False] * 5 + [True]


def plan_hass(states, plan):
    """Return a mock hass with heat pump settings, prices and a plan."""
    hass = mock_hass(states, plan)
    hass.data[DOMAIN]["config"] = {
        "heat_pump_power": 300,
        "heat_pump_margin": 5,
        "heat_pump_heating_limit": 15,
    }
    return hass


def test_plan_inputs():
    """Test COP, price and heat need per quarter, the live temperature as fallback."""
    hass = plan_hass({"sensor.gw1100a_outdoor_temperature": "20"}, {})
    starts = [QUARTER, QUARTER + dt.timedelta(minutes=15)]
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        inputs = heating.plan_inputs(hass, starts, [0.0, None])
    assert inputs.probe_kwh == pytest.approx(0.075)
    assert inputs.temperatures == [0.0, 20.0]
    assert inputs.cops[0] == pytest.approx(3.0)
    assert inputs.district == [76.7, 76.7]
    assert inputs.heating == [True, False]


def test_action_follows_the_plan():
    """Test the plan's flag now and when the run ends."""
    starts = [QUARTER + dt.timedelta(minutes=15 * i) for i in range(4)]
    plan = {
        "updated": QUARTER.isoformat(),
        "starts": [s.isoformat() for s in starts],
        "heat_pump": [True, True, False, False],
        "marginal": [100.0] * 4,
        "heat_pump_cop": [3.0] * 4,
    }
    hass = plan_hass({}, plan)
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        action = heating.heat_pump_action(hass)
    assert action["state"] == "on"
    assert action["source"] == "plan"
    assert action["until"] == starts[2].isoformat()


def test_action_falls_back_to_the_economy_now():
    """Test that without a plan the saving now and the heating limit decide."""
    hass = plan_hass({"sensor.gw1100a_outdoor_temperature": "5"}, {})
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        action = heating.heat_pump_action(hass)
    assert (action["state"], action["source"]) == ("on", "fallback")
    hass = plan_hass({"sensor.gw1100a_outdoor_temperature": "16"}, {})
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        action = heating.heat_pump_action(hass)
    assert action["state"] == "off"


def test_action_without_inputs_decides_nothing():
    """Test that missing prices give no decision, so the heat pump is left alone."""
    hass = plan_hass({"sensor.gw1100a_outdoor_temperature": "5"}, {})
    hass.data[DOMAIN]["prices"] = {}
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        action = heating.heat_pump_action(hass)
    assert (action["state"], action["source"]) == (None, "none")
