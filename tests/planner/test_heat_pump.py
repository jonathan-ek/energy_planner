"""Tests for the heat pump economy, its schedule and its draw."""

import datetime as dt
import importlib
from zoneinfo import ZoneInfo

import pytest

heat_pump = importlib.import_module(
    "custom_components.energy_planner.planner.heat_pump"
)

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
