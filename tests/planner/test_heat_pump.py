"""Tests for the heat pump economy and its glue."""

import datetime as dt
import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.const import DOMAIN
from custom_components.energy_planner.planner.prices import SpotPrices

from .plans import make_plan

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
# A plan with the spot price now, made an hour ago: too old to follow
OLD_PLAN = make_plan(QUARTER, spot=[500.0], updated=QUARTER - dt.timedelta(hours=1))


def mock_hass(states, plan=OLD_PLAN, published=None):
    """Return a mock hass with the spot price now, a plan and sensor states."""
    hass = MagicMock()
    hass.states.get = lambda entity_id: (
        SimpleNamespace(state=states[entity_id]) if entity_id in states else None
    )
    hass.data = {
        DOMAIN: {
            "options": {"tariff_preset": "tekniska_verken_alternativ"},
            "config": {},
            "plan": plan,
            "prices": SpotPrices(
                {QUARTER: 500.0} if published is None else published, {}
            ),
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
    plan = make_plan(QUARTER, spot=[500.0], grid_export_kw=[2.0])
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


def plan_hass(states, plan=OLD_PLAN, published=None):
    """Return a mock hass with heat pump settings, prices and a plan."""
    hass = mock_hass(states, plan, published)
    hass.data[DOMAIN]["config"] = {
        "heat_pump_power": 300,
        "heat_pump_margin": 5,
        "heat_pump_heating_limit": 15,
    }
    return hass


def test_heat_loss():
    """Test heat per °C indoor-outdoor: draw * COP / difference."""
    excess = dict.fromkeys(range(6), 0.5)
    indoor = dict.fromkeys(range(6), 21.0)
    # COP 3 at 0 °C: 1.5 kW heat for 21 °C
    outdoor = dict.fromkeys(range(6), 0.0)
    loss, hours = heat_pump.heat_loss(excess, indoor, outdoor, CURVE)
    assert (loss, hours) == (pytest.approx(1.5 / 21), 6)
    # Hours without temperatures or with a small difference do not count
    outdoor[0] = 19.0
    del indoor[1]
    assert heat_pump.heat_loss(excess, indoor, outdoor, CURVE) == (None, 4)


def test_draw():
    """Test the draw from the heat loss, zero above indoor and below the curve."""
    loss = 1.5 / 21
    assert heat_pump.draw(loss, 21.0, 0.0, CURVE) == pytest.approx(0.5)
    # Colder: more heat at a lower COP
    assert heat_pump.draw(loss, 21.0, -10.0, CURVE) > 0.7
    assert heat_pump.draw(loss, 21.0, 22.0, CURVE) == 0.0
    assert heat_pump.draw(loss, 21.0, -20.0, CURVE) == 0.0
    assert heat_pump.draw(10.0, 21.0, -10.0, CURVE) == heat_pump.MAX_DRAW_KW


def test_power_from_the_loss_or_the_setting():
    """Test the measured draw, the setting without it, and nothing when not planned."""
    loss = 1.5 / 21
    assert heat_pump.power_kw(0.3, loss, 21.0, 0.0, CURVE) == pytest.approx(0.5)
    assert heat_pump.power_kw(0.3, None, 21.0, 0.0, CURVE) == 0.3
    assert heat_pump.power_kw(0.3, loss, None, 0.0, CURVE) == 0.3
    # Not planned: the forecast keeps the draw in the house load
    assert heat_pump.power_kw(0.0, loss, 21.0, 0.0, CURVE) == 0.0


def test_action_follows_the_plan():
    """Test the plan's flag now and when the run ends."""
    plan = make_plan(QUARTER, heat_pump=[True, True, False, False])
    starts = plan.timeline.locals
    hass = plan_hass({}, plan)
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        action = heating.heat_pump_action(hass)
    assert action["state"] == "on"
    assert action["source"] == "plan"
    assert action["until"] == starts[2].isoformat()
    # Following the plan, the economy is not worked out
    with (
        patch.object(heating.dt_utils, "now", return_value=NOW),
        patch.object(heating, "heat_pump_economy") as economy,
    ):
        heating.heat_pump_action(hass)
    economy.assert_not_called()


def test_action_without_heat_pump():
    """Test a plan made with heat_pump_power 0: no marginal prices, always off."""
    plan = make_plan(QUARTER, heat_pump=[False] * 4, marginal=[])
    starts = plan.timeline.locals
    hass = plan_hass({}, plan)
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        action = heating.heat_pump_action(hass)
    assert action["state"] == "off"
    assert action["marginal"] is None
    assert action["until"] == (starts[-1] + dt.timedelta(minutes=15)).isoformat()


def test_action_falls_back_to_the_economy_now():
    """Test that without a plan the saving now and the heating limit decide."""
    hass = plan_hass({"sensor.gw1100a_outdoor_temperature": "5"})
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        action = heating.heat_pump_action(hass)
    assert (action["state"], action["source"]) == ("on", "fallback")
    hass = plan_hass({"sensor.gw1100a_outdoor_temperature": "16"})
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        action = heating.heat_pump_action(hass)
    assert action["state"] == "off"


def test_action_without_inputs_decides_nothing():
    """Test that missing prices give no decision, so the heat pump is left alone."""
    hass = plan_hass({"sensor.gw1100a_outdoor_temperature": "5"}, published={})
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        action = heating.heat_pump_action(hass)
    assert (action["state"], action["source"]) == (None, "none")


def test_action_without_a_plan_uses_the_prices():
    """Test that a failed plan (e.g. no battery SOC) still leaves the fallback."""
    hass = plan_hass({"sensor.gw1100a_outdoor_temperature": "5"}, plan=None)
    with patch.object(heating.dt_utils, "now", return_value=NOW):
        action = heating.heat_pump_action(hass)
    assert action["source"] == "fallback"
    assert action["state"] in ("on", "off")
