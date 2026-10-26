"""Tests for build_forecast: the forecast from plain inputs, without Home Assistant."""

import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.planner import forecast, heat_pump

TZ = ZoneInfo("Europe/Stockholm")
NOW = dt.datetime(2026, 10, 7, 14, 10, tzinfo=TZ)  # Wednesday afternoon
TOMORROW = dt.datetime.combine(NOW.date() + dt.timedelta(days=1), dt.time(0), TZ)
CURVE = heat_pump.COP_CURVES["msz_ap42"]  # the owner's heat pump


def hours(days):
    """Return the UTC hour starts of the last days."""
    start = (NOW - dt.timedelta(days=days)).replace(minute=0).astimezone(dt.UTC)
    end = NOW.replace(minute=0).astimezone(dt.UTC)
    return [
        start + dt.timedelta(hours=i)
        for i in range(int((end - start) / dt.timedelta(hours=1)))
    ]


def inputs(**changes):
    """Return inputs: 1 kW house load for 35 days, 3 kW PV in daylight for 14 days.

    An EV session is in the load (and left out); no PV forecast, calendar or heat
    pump hours.
    """
    load = dict.fromkeys(hours(35), 1.0)
    ev = dict.fromkeys(hours(35), 0.0)
    session = (NOW - dt.timedelta(days=1)).replace(hour=18, minute=0).astimezone(dt.UTC)
    load[session], ev[session] = 8.0, 7.0
    pv = {h: 3.0 if 6 <= h.astimezone(TZ).hour < 18 else 0.0 for h in hours(14)}
    base = forecast.ForecastInputs(
        now=NOW,
        tz=TZ,
        load=load,
        pv=pv,
        ev=ev,
        indoor={},
        outdoor={},
        heating_shares={},
        planned_loads=[],
        away_periods=[],
        open_meteo=None,
        forecast_solar=[],
        load_model="profile",
        pv_calibration="daily",
        weekend_reserve_kwh=4.0,
        reserve_start=18,
        reserve_end=22,
        heat_pump_power_kw=0.3,
        cop_curve=CURVE,
    )
    return dataclasses.replace(base, **changes)


def open_meteo(temperatures=None):
    """Return Open-Meteo's PV and air temperature.

    Latest: a 0-4-4-0 kW triangle 10-14 tomorrow; day-before: 2 kW in daylight;
    temperature 0 °C at midnight tomorrow, +1 °C per hour.
    """
    latest = {
        (TOMORROW + dt.timedelta(hours=h)).astimezone(dt.UTC): kw
        for h, kw in zip(range(10, 14), [0, 4, 4, 0], strict=True)
    }
    previous = {h: 2.0 if 6 <= h.astimezone(TZ).hour < 18 else 0.0 for h in hours(15)}
    if temperatures is None:
        temperatures = {
            (TOMORROW + dt.timedelta(hours=h)).astimezone(dt.UTC): float(h)
            for h in range(24)
        }
    return latest, previous, temperatures


def build(**changes):
    """Return the published attributes of the forecast from changed inputs."""
    return forecast.build_forecast(inputs(**changes)).to_attributes()


def test_without_a_pv_source():
    """Test that the load forecast works without PV."""
    result = build()
    assert result["pv"] is None
    assert result["pv_tomorrow"] is None
    assert result["load_tomorrow"] == pytest.approx(24.0)


def test_pv_from_open_meteo():
    """Test that Open-Meteo is used and calibrated against actual production."""
    result = build(open_meteo=open_meteo())
    assert result["pv_source"] == "open-meteo"
    # Actual 3 kW where the day-before forecast said 2 kW
    assert result["pv_calibration"] == pytest.approx(1.5)
    assert result["pv_tomorrow_uncalibrated"] == pytest.approx(8.0)
    assert result["pv_tomorrow"] == pytest.approx(12.0)
    # Yesterday: 12 h at 2 kW day-before forecast, calibrated by 3/2; actual 12 h at 3
    assert len(result["yesterday_starts"]) == 96
    assert result["yesterday_pv_kwh"] == pytest.approx(36.0)
    assert result["yesterday_pv_actual_kwh"] == pytest.approx(36.0)
    # Flat 1 kWh per hour; the EV session yesterday is left out of the actual too
    assert result["yesterday_load_kwh"] == pytest.approx(24.0)
    assert result["yesterday_load_actual_kwh"] == pytest.approx(24.0)
    # Mid-quarter temperature tomorrow, None where Open-Meteo has none
    assert result["temperature"][95] is None
    assert result["temperature"][96 + 4 * 10] == pytest.approx(10.125)


@pytest.mark.parametrize(
    ("model", "load_tomorrow", "slope"),
    [("profile", 24.0, None), ("temperature", 36.0, -1.0)],
)
def test_temperature_load_model(model, load_tomorrow, slope):
    """Test that the temperature model only changes the load when chosen."""
    # Measured outdoor temperature: 10 °C the last 9 days, 4 / 16 °C before, and
    # load on the line 24 + (10 - temperature) kWh per day
    outdoor, load = {}, {}
    for hour in hours(35):
        age = (NOW.date() - hour.astimezone(TZ).date()).days
        temperature = 10.0 if age <= 9 else (4.0 if age % 2 else 16.0)
        outdoor[hour] = temperature
        load[hour] = 1.0 + (10.0 - temperature) / 24
    # Open-Meteo: 10 °C until tomorrow, which is 12 °C colder
    temperatures = {
        hour: -2.0 if hour >= TOMORROW.astimezone(dt.UTC) else 10.0
        for hour in (
            (TOMORROW + dt.timedelta(hours=h)).astimezone(dt.UTC)
            for h in range(-72, 24)
        )
    }
    result = build(
        load=load,
        ev={},
        outdoor=outdoor,
        load_model=model,
        open_meteo=open_meteo(temperatures),
    )
    assert result["load_model"] == model
    assert result["heating_slope"] == slope
    # Profile 24 kWh at 10 °C; the temperature model adds 1 kWh per °C colder
    assert result["load_tomorrow"] == pytest.approx(load_tomorrow)
    assert result["yesterday_load_kwh"] == pytest.approx(24.0)


@pytest.mark.parametrize("model", ["daily", "hourly"])
def test_pv_hourly_calibration(model):
    """Test that the hourly calibration scales each hour by its own factor."""
    # Shade from 15: actual 0.5 kW there, 3 kW the rest of the day
    pv = {
        h: 0.5 if 15 <= h.astimezone(TZ).hour < 18 else kw
        for h, kw in inputs().pv.items()
    }
    result = build(pv=pv, pv_calibration=model, open_meteo=open_meteo())
    assert result["pv_calibration_model"] == model
    if model == "daily":
        assert result["pv_hourly_calibration"] is None
        return
    # Day-before forecast 2 kW: 3 / 2 before 15, 0.5 / 2 from 15
    factors = result["pv_hourly_calibration"]
    assert factors[12] == pytest.approx(1.5)
    assert factors[16] == pytest.approx(0.25)
    # Tomorrow's triangle (10-14) is all before the shade: 8 kWh x 1.5
    assert result["pv_tomorrow"] == pytest.approx(12.0)
    # Yesterday: 9 h at 2 kW x 1.5 and 3 h at 2 kW x 0.25, as produced (the
    # interpolation smears a little energy over the hour edges, at other factors)
    assert result["yesterday_pv_kwh"] == pytest.approx(28.5, abs=0.2)
    assert result["yesterday_pv_actual_kwh"] == pytest.approx(28.5)


def test_heat_pump_is_removed_from_the_load():
    """Test that the heat pump's draw is measured and taken out of the load history."""
    load = dict(inputs().load)
    shares = {}
    for hour in hours(3):
        if hour.astimezone(TZ).hour < 6:
            shares[hour] = 1.0
            load[hour] += 0.5
    result = build(
        load=load,
        heating_shares=shares,
        indoor=dict.fromkeys(hours(35), 21.0),
        outdoor=dict.fromkeys(hours(35), 9.0),
    )
    # 0.5 kW at COP 3.74 (9 °C) for 12 °C
    assert result["heat_pump_loss"] == pytest.approx(0.5 * 3.74 / 12, abs=1e-3)
    assert result["heat_pump_loss_hours"] == 18
    assert result["load_tomorrow"] == pytest.approx(24.0)
    assert result["temperature"] is None


def test_yesterday_includes_planned_loads():
    """Test that yesterday's expected load includes yesterday's planned loads."""
    yesterday = NOW.date() - dt.timedelta(days=1)
    sauna = (
        dt.datetime.combine(yesterday, dt.time(19), TZ),
        dt.datetime.combine(yesterday, dt.time(21), TZ),
        5.0,
        "Bastu",
    )
    result = build(planned_loads=[sauna])
    assert result["yesterday_load_kwh"] == pytest.approx(29.0)
    assert result["yesterday_pv"] is None
    # Past events are not listed as upcoming planned loads
    assert result["planned_events"] == []
    assert result["planned_tomorrow"] == 0


def test_published_series_keep_their_types():
    """Test the published values' shape: lists, Nones kept, rounded."""
    result = build(open_meteo=open_meteo())
    assert isinstance(result["load"], list) and isinstance(
        result["yesterday_load"], list
    )
    assert result["load"][:2] == [0.25, 0.25]
    assert isinstance(result["pv"], list) and isinstance(result["temperature"], list)
    assert result["temperature"][0] is None
    assert result["away"][:1] == [False]
    assert result["updated"] == NOW.isoformat()
