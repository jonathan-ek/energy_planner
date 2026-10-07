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
