"""Tests for the forecast glue in the dynamic planner."""

import datetime as dt
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
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
        patch.object(dynamic_planner, "_solar_planes", return_value=(None, [])),
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
        patch.object(dynamic_planner, "_solar_planes", return_value=(None, [])),
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
        patch.object(dynamic_planner, "_solar_planes", return_value=(None, [])),
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
        patch.object(dynamic_planner, "_solar_planes", return_value=(None, [])),
        patch.object(
            dynamic_planner, "_calendar_events", side_effect=RuntimeError("boom")
        ),
    ):
        await dynamic_planner.async_update_forecast(mock_hass)
    result = mock_hass.data[DOMAIN]["forecast"]
    assert result["planned_tomorrow"] == 0
    assert result["load_tomorrow"] == pytest.approx(24.0)


def open_meteo_forecast():
    """Latest and day-before forecast in kW per UTC hour, as _open_meteo_pv returns."""
    tomorrow = dt.datetime.combine(NOW.date() + dt.timedelta(days=1), dt.time(0), TZ)
    latest = {
        (tomorrow + dt.timedelta(hours=h)).astimezone(dt.UTC): kw
        for h, kw in zip(range(10, 14), [0, 4, 4, 0], strict=True)
    }
    previous = {}
    for hour in hours(14):
        if 6 <= hour.astimezone(TZ).hour < 18:
            previous[hour] = 2.0
    return latest, previous


async def test_pv_from_open_meteo(mock_hass):
    """Test that Open-Meteo is used and calibrated against actual production."""
    with (
        patch.object(dynamic_planner.dt_utils, "now", return_value=NOW),
        patch.object(dynamic_planner, "_forecast_solar_sources", return_value=[]),
        patch.object(dynamic_planner, "_hourly_means", return_value=statistics()),
        patch.object(dynamic_planner, "_calendar_events", return_value=[]),
        patch.object(
            dynamic_planner, "_solar_planes", return_value=((58.4, 15.6), [(27, 75, 6)])
        ),
        patch.object(
            dynamic_planner, "_open_meteo_pv", return_value=open_meteo_forecast()
        ),
    ):
        await dynamic_planner.async_update_forecast(mock_hass)
    result = mock_hass.data[DOMAIN]["forecast"]
    assert result["pv_source"] == "open-meteo"
    # Actual 3 kW where the day-before forecast said 2 kW
    assert result["pv_calibration"] == pytest.approx(1.5)
    assert result["pv_tomorrow_uncalibrated"] == pytest.approx(8.0)
    assert result["pv_tomorrow"] == pytest.approx(12.0)


async def test_pv_falls_back_to_forecast_solar(mock_hass):
    """Test that Forecast.Solar is used when Open-Meteo fails."""
    tomorrow = NOW.date() + dt.timedelta(days=1)
    watts = {
        dt.datetime.combine(tomorrow, dt.time(10), TZ): 0,
        dt.datetime.combine(tomorrow, dt.time(12), TZ): 4000,
        dt.datetime.combine(tomorrow, dt.time(14), TZ): 0,
    }
    with (
        patch.object(dynamic_planner.dt_utils, "now", return_value=NOW),
        patch.object(
            dynamic_planner,
            "_forecast_solar_sources",
            return_value=[(watts, FORECAST_NOW)],
        ),
        patch.object(dynamic_planner, "_hourly_means", return_value=statistics()),
        patch.object(dynamic_planner, "_calendar_events", return_value=[]),
        patch.object(
            dynamic_planner, "_solar_planes", return_value=((58.4, 15.6), [(27, 75, 6)])
        ),
        patch.object(
            dynamic_planner,
            "_open_meteo_pv",
            side_effect=aiohttp.ClientError("unreachable"),
        ),
    ):
        await dynamic_planner.async_update_forecast(mock_hass)
    result = mock_hass.data[DOMAIN]["forecast"]
    assert result["pv_source"] == "forecast.solar"
    assert result["pv_tomorrow"] == pytest.approx(12.0)


def test_solar_planes_from_forecast_solar():
    """Test reading location and planes from the Forecast.Solar entries."""
    with_subentries = MagicMock(data={"latitude": 58.4, "longitude": 15.6}, options={})
    with_subentries.get_subentries_of_type.return_value = [
        SimpleNamespace(data={"declination": 27, "azimuth": 75, "modules_power": 6000})
    ]
    old_style = MagicMock(
        data={"latitude": 58.4, "longitude": 15.6},
        options={"declination": 3, "azimuth": 345, "modules_power": 5000},
    )
    old_style.get_subentries_of_type.return_value = []
    no_location = MagicMock(data={}, options={})
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [
        no_location,
        with_subentries,
        old_style,
    ]
    location, planes = dynamic_planner._solar_planes(hass)
    assert location == (58.4, 15.6)
    assert planes == [(27.0, 75.0, 6.0), (3.0, 345.0, 5.0)]


async def test_fetch_open_meteo_request():
    """Test the Open-Meteo request parameters."""
    response = MagicMock()
    response.json = AsyncMock(return_value={"hourly": {"time": []}})
    session = MagicMock()
    session.get.return_value.__aenter__ = AsyncMock(return_value=response)
    session.get.return_value.__aexit__ = AsyncMock(return_value=None)
    hourly = await dynamic_planner._fetch_open_meteo(session, 58.4, 15.6, 27, 75)
    assert hourly == {"time": []}
    params = session.get.call_args.kwargs["params"]
    assert params["azimuth"] == -105
    assert params["tilt"] == 27
    assert params["models"] == "metno_seamless,ecmwf_ifs025,icon_seamless"
    assert "global_tilted_irradiance_previous_day1" in params["hourly"]
    assert params["past_days"] == dynamic_planner.CALIBRATION_DAYS


async def test_calendar_events_parsing(mock_hass):
    """Test that only well-formed timed events are returned, in local time."""
    mock_hass.states.get.return_value = MagicMock()
    mock_hass.services.async_call = AsyncMock(
        return_value={
            "calendar.energiplan": {
                "events": [
                    {
                        "start": "2026-10-11T19:00:00+02:00",
                        "end": "2026-10-11T21:00:00+02:00",
                        "summary": "Bastu",
                        "description": None,
                    },
                    {
                        "start": "2026-10-11",
                        "end": "2026-10-12",
                        "summary": "Hela dagen",
                    },
                    {"start": "not a date", "end": "also not", "summary": "Trasig"},
                    "not an event",
                ]
            }
        }
    )
    events = await dynamic_planner._calendar_events(
        mock_hass, "calendar.energiplan", NOW, NOW + dt.timedelta(days=2)
    )
    assert events == [
        (
            dt.datetime(2026, 10, 11, 19, tzinfo=TZ),
            dt.datetime(2026, 10, 11, 21, tzinfo=TZ),
            "Bastu",
            "",
        )
    ]
    mock_hass.services.async_call = AsyncMock(return_value=None)
    assert (
        await dynamic_planner._calendar_events(
            mock_hass, "calendar.energiplan", NOW, NOW
        )
        == []
    )
