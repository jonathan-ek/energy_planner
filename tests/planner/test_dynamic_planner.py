"""Tests for the forecast glue in the dynamic planner."""

import datetime as dt
import importlib
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.const import DOMAIN

# planner/__init__.py re-exports the planner function under the module's name
dynamic_planner = importlib.import_module(
    "custom_components.energy_planner.planner.dynamic_planner"
)

TZ = ZoneInfo("Europe/Stockholm")
NOW = dt.datetime(2026, 10, 7, 14, 10, tzinfo=TZ)  # Wednesday afternoon
LOAD = "sensor.solis_s6_solis_household_load_power"
PV = "sensor.solis_s6_solis_total_pv_power"
EV = "sensor.ehwuhqtp_effekt"
FORECAST_NOW = "sensor.power_production_now"


def hours(days):
    """Yield UTC hour starts for the last days."""
    start = (NOW - dt.timedelta(days=days)).replace(minute=0).astimezone(dt.UTC)
    end = NOW.replace(minute=0).astimezone(dt.UTC)
    while start < end:
        yield start
        start += dt.timedelta(hours=1)


def statistics():
    """Hourly means as returned by the recorder, in kW."""
    means = {LOAD: {}, PV: {}, EV: {}, FORECAST_NOW: {}}
    for hour in hours(35):
        means[LOAD][hour] = 1.0
        means[EV][hour] = 0.0
    # One EV session is removed from the load history
    session = (NOW - dt.timedelta(days=1)).replace(hour=18, minute=0).astimezone(dt.UTC)
    means[LOAD][session] = 8.0
    means[EV][session] = 7.0
    for hour in hours(14):
        if 6 <= hour.astimezone(TZ).hour < 18:
            means[PV][hour] = 3.0
            means[FORECAST_NOW][hour] = 2.0
    means[PV][hour] = 900.0  # Modbus glitch, ignored
    return means


@pytest.fixture
def mock_hass():
    """Create a mock hass with the domain data and Stockholm as time zone."""
    default = dynamic_planner.dt_utils.DEFAULT_TIME_ZONE
    dynamic_planner.dt_utils.set_default_time_zone(TZ)
    hass = MagicMock()
    hass.data = {DOMAIN: {"config": {"forecast_weekend_reserve": 4}}}
    yield hass
    dynamic_planner.dt_utils.set_default_time_zone(default)


async def test_update_forecast(mock_hass):
    """Test load, reserve and calibrated PV forecast."""
    tomorrow = NOW.date() + dt.timedelta(days=1)
    watts = {
        dt.datetime.combine(tomorrow, dt.time(10), TZ): 0,
        dt.datetime.combine(tomorrow, dt.time(12), TZ): 4000,
        dt.datetime.combine(tomorrow, dt.time(14), TZ): 0,
    }
    sensor = MagicMock()
    mock_hass.data[DOMAIN]["forecast_sensors"] = [sensor]
    with (
        patch.object(dynamic_planner.dt_utils, "now", return_value=NOW),
        patch.object(
            dynamic_planner,
            "_forecast_solar_sources",
            return_value=[(watts, FORECAST_NOW)],
        ),
        patch.object(dynamic_planner, "_hourly_means", return_value=statistics()),
        patch.object(dynamic_planner, "_calendar_events", return_value=[]),
    ):
        await dynamic_planner.async_update_forecast(mock_hass)

    result = mock_hass.data[DOMAIN]["forecast"]
    # All of today and tomorrow
    assert result["starts"][0] == NOW.replace(hour=0, minute=0).isoformat()
    assert len(result["starts"]) == 2 * 24 * 4
    # Today counts from the current quarter (14:00)
    assert result["load_today_remaining"] == pytest.approx(10.0)
    # Flat 1 kWh per hour, EV session excluded
    assert result["load_tomorrow"] == pytest.approx(24.0)
    assert set(result["load"]) == {0.25}
    # Weekday: no reserve
    assert sum(result["reserve"]) == 0
    # Triangle 0 -> 4 kW -> 0 over four hours = 8 kWh, scaled by 3/2
    assert result["pv_tomorrow_uncalibrated"] == pytest.approx(8.0)
    assert result["pv_calibration"] == pytest.approx(1.5)
    assert result["pv_tomorrow"] == pytest.approx(12.0)
    sensor.async_write_ha_state.assert_called_once()


async def test_update_forecast_without_forecast_solar(mock_hass):
    """Test that the load forecast works without Forecast.Solar."""
    with (
        patch.object(dynamic_planner.dt_utils, "now", return_value=NOW),
        patch.object(dynamic_planner, "_forecast_solar_sources", return_value=[]),
        patch.object(dynamic_planner, "_hourly_means", return_value=statistics()),
        patch.object(dynamic_planner, "_calendar_events", return_value=[]),
    ):
        await dynamic_planner.async_update_forecast(mock_hass)
    result = mock_hass.data[DOMAIN]["forecast"]
    assert result["pv"] is None
    assert result["pv_tomorrow"] is None
    assert result["load_tomorrow"] == pytest.approx(24.0)


async def test_update_forecast_with_planned_loads(mock_hass):
    """Test that calendar events are added as planned loads."""
    tomorrow = NOW.date() + dt.timedelta(days=1)
    sauna = (
        dt.datetime.combine(tomorrow, dt.time(19), TZ),
        dt.datetime.combine(tomorrow, dt.time(21), TZ),
        "Bastu",
        "",
    )
    dentist = (
        dt.datetime.combine(tomorrow, dt.time(9), TZ),
        dt.datetime.combine(tomorrow, dt.time(10), TZ),
        "Tandläkare",
        "",
    )
    with (
        patch.object(dynamic_planner.dt_utils, "now", return_value=NOW),
        patch.object(dynamic_planner, "_forecast_solar_sources", return_value=[]),
        patch.object(dynamic_planner, "_hourly_means", return_value=statistics()),
        patch.object(
            dynamic_planner, "_calendar_events", return_value=[sauna, dentist]
        ),
    ):
        await dynamic_planner.async_update_forecast(mock_hass)
    result = mock_hass.data[DOMAIN]["forecast"]
    assert result["base_tomorrow"] == pytest.approx(24.0)
    assert result["planned_tomorrow"] == pytest.approx(5.0)
    assert result["load_tomorrow"] == pytest.approx(29.0)
    assert [e["summary"] for e in result["planned_events"]] == ["Bastu"]
    planned = dict(zip(result["starts"], result["planned"], strict=True))
    assert planned[sauna[0].isoformat()] == pytest.approx(5.0 / 8)


async def test_calendar_failure_does_not_stop_forecast(mock_hass):
    """Test that a failing calendar only drops the planned loads."""
    with (
        patch.object(dynamic_planner.dt_utils, "now", return_value=NOW),
        patch.object(dynamic_planner, "_forecast_solar_sources", return_value=[]),
        patch.object(dynamic_planner, "_hourly_means", return_value=statistics()),
        patch.object(
            dynamic_planner, "_calendar_events", side_effect=RuntimeError("boom")
        ),
    ):
        await dynamic_planner.async_update_forecast(mock_hass)
    result = mock_hass.data[DOMAIN]["forecast"]
    assert result["planned_tomorrow"] == 0
    assert result["load_tomorrow"] == pytest.approx(24.0)
