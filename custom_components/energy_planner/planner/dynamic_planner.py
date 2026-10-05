import datetime as dt
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_utils

from . import forecast
from ..const import (
    DOMAIN,
    DEFAULT_FORECAST_CALENDAR,
    DEFAULT_FORECAST_EV_SENSOR,
    DEFAULT_FORECAST_LOAD_SENSOR,
    DEFAULT_FORECAST_PV_SENSOR,
    FORECAST_SENSORS,
)

_LOGGER = logging.getLogger(__name__)

HISTORY_DAYS = 35
CALIBRATION_DAYS = 14
# Hourly PV means above this are Modbus glitches
PV_MAX_KW = 25


async def _hourly_means(hass: HomeAssistant, statistic_ids, start, end):
    """Hourly mean per statistic in kW (= kWh for the hour), keyed by UTC hour start."""
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        set(statistic_ids),
        "hour",
        {"power": "kW"},
        {"mean"},
    )
    return {
        statistic_id: {
            dt.datetime.fromtimestamp(row["start"], dt.UTC): row["mean"]
            for row in rows
            if row.get("mean") is not None
        }
        for statistic_id, rows in stats.items()
    }


def _forecast_solar_sources(hass: HomeAssistant):
    """Return (watts forecast, power_production_now entity id) per plane."""
    registry = er.async_get(hass)
    sources = []
    for entry in hass.config_entries.async_entries("forecast_solar"):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        estimate = getattr(getattr(entry, "runtime_data", None), "data", None)
        watts = getattr(estimate, "watts", None)
        if watts is None:
            continue
        now_entity = registry.async_get_entity_id(
            "sensor", "forecast_solar", f"{entry.entry_id}_power_production_now"
        )
        sources.append((watts, now_entity))
    return sources


async def _calendar_events(hass: HomeAssistant, entity_id, start, end):
    """Return timed events (start, end, summary, description) from a calendar."""
    if hass.states.get(entity_id) is None:
        return []
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": entity_id, "start_date_time": start, "end_date_time": end},
        blocking=True,
        return_response=True,
    )
    events = []
    for event in response.get(entity_id, {}).get("events", []):
        # All-day events only have a date and say nothing about when the load runs
        if len(event["start"]) <= 10 or len(event["end"]) <= 10:
            continue
        events.append(
            (
                dt_utils.as_local(dt_utils.parse_datetime(event["start"])),
                dt_utils.as_local(dt_utils.parse_datetime(event["end"])),
                event.get("summary", ""),
                event.get("description", ""),
            )
        )
    return events


async def _planned_loads(hass: HomeAssistant, calendar_id, start, end):
    """Planned loads (start, end, kWh, summary) from the calendar."""
    try:
        events = await _calendar_events(hass, calendar_id, start, end)
    except Exception:
        _LOGGER.exception("Failed to read planned loads from %s", calendar_id)
        return []
    planned = []
    for event_start, event_end, summary, description in events:
        kwh = forecast.planned_event_energy(summary, description)
        if kwh is None:
            _LOGGER.debug("Ignoring calendar event without energy: %s", summary)
            continue
        planned.append((event_start, event_end, kwh, summary))
    return planned


def _daily_totals(quarters, values, since):
    """Sum per local date, counting only quarters from since."""
    totals = {}
    for start, value in zip(quarters, values, strict=True):
        if value is not None and start >= since:
            totals[start.date()] = totals.get(start.date(), 0.0) + value
    return totals


async def async_update_forecast(hass: HomeAssistant):
    """Forecast load and PV per quarter for all of today and tomorrow.

    Today's forecast only uses days before today, so it is the same all day and can
    be compared with what actually happens. Totals for today count from now.
    """
    config = hass.data[DOMAIN]["config"]
    load_id = config.get("forecast_load_sensor", DEFAULT_FORECAST_LOAD_SENSOR)
    pv_id = config.get("forecast_pv_sensor", DEFAULT_FORECAST_PV_SENSOR)
    ev_id = config.get("forecast_ev_sensor", DEFAULT_FORECAST_EV_SENSOR)
    calendar_id = config.get("forecast_calendar", DEFAULT_FORECAST_CALENDAR)

    now = dt_utils.now()
    today = now.date()
    end = dt_utils.start_of_local_day(today + dt.timedelta(days=2))
    quarters = forecast.quarter_starts(dt_utils.start_of_local_day(), end)
    current = forecast.quarter_starts(now, now + forecast.QUARTER)[0]

    sources = _forecast_solar_sources(hass)
    forecast_ids = [entity for _, entity in sources if entity]
    means = await _hourly_means(
        hass,
        {load_id, pv_id, ev_id, *forecast_ids},
        dt_utils.start_of_local_day() - dt.timedelta(days=HISTORY_DAYS),
        now,
    )

    # House load without EV charging, which is planned separately
    ev = means.get(ev_id, {})
    history = {}
    for hour, kw in means.get(load_id, {}).items():
        local = dt_utils.as_local(hour)
        history[(local.date(), local.hour)] = max(kw - max(ev.get(hour, 0.0), 0.0), 0.0)
    # One-off large loads are added back from the calendar or covered by the reserve
    history = forecast.cap_large_loads(history)
    load = forecast.load_quarters(history, quarters, today)
    reserve = forecast.reserve_quarters(
        quarters,
        float(config.get("forecast_weekend_reserve", 4)),
        int(config.get("forecast_reserve_start", 18)),
        int(config.get("forecast_reserve_end", 22)),
    )
    planned_loads = await _planned_loads(hass, calendar_id, quarters[0], end)
    planned = forecast.planned_quarters(
        [(start, stop, kwh) for start, stop, kwh, _ in planned_loads], quarters
    )

    pv = None
    calibration = None
    raw_totals = {}
    if sources:
        since = now - dt.timedelta(days=CALIBRATION_DAYS)
        actual = {
            hour: kw
            for hour, kw in means.get(pv_id, {}).items()
            if hour >= since and 0 <= kw <= PV_MAX_KW
        }
        predicted = {}
        for entity in forecast_ids:
            for hour, kw in means.get(entity, {}).items():
                if hour >= since:
                    predicted[hour] = predicted.get(hour, 0.0) + kw
        calibration = forecast.pv_calibration(actual, predicted)
        raw = [0.0] * len(quarters)
        for watts, _ in sources:
            plane = forecast.pv_quarters(watts, quarters)
            raw = [a + b for a, b in zip(raw, plane, strict=True)]
        raw_totals = _daily_totals(quarters, raw, current)
        pv = [value * calibration for value in raw]

    tomorrow = today + dt.timedelta(days=1)
    base_totals = _daily_totals(quarters, load, current)
    planned_totals = _daily_totals(quarters, planned, current)
    pv_totals = _daily_totals(quarters, pv, current) if pv is not None else {}

    def total(day):
        """Return the expected load for a day: profile plus planned loads."""
        if day not in base_totals:
            return None
        return round(base_totals[day] + planned_totals.get(day, 0.0), 2)

    hass.data[DOMAIN]["forecast"] = {
        "updated": now.isoformat(),
        "starts": [q.isoformat() for q in quarters],
        "load": [None if v is None else round(v, 3) for v in load],
        "planned": [round(v, 3) for v in planned],
        "reserve": [round(v, 3) for v in reserve],
        "pv": None if pv is None else [round(v, 3) for v in pv],
        "planned_events": [
            {
                "summary": summary,
                "start": start.isoformat(),
                "end": stop.isoformat(),
                "kwh": kwh,
            }
            for start, stop, kwh, summary in planned_loads
        ],
        "load_today_remaining": total(today),
        "load_tomorrow": total(tomorrow),
        "base_tomorrow": round(base_totals[tomorrow], 2)
        if tomorrow in base_totals
        else None,
        "planned_tomorrow": round(planned_totals.get(tomorrow, 0.0), 2),
        "pv_today_remaining": round(pv_totals.get(today, 0.0), 2)
        if pv is not None
        else None,
        "pv_tomorrow": round(pv_totals[tomorrow], 2) if tomorrow in pv_totals else None,
        "pv_tomorrow_uncalibrated": round(raw_totals[tomorrow], 2)
        if tomorrow in raw_totals
        else None,
        "pv_calibration": None if calibration is None else round(calibration, 3),
    }
    _LOGGER.info(
        "Forecast tomorrow: load %s kWh, PV %s kWh (calibration %s)",
        hass.data[DOMAIN]["forecast"]["load_tomorrow"],
        hass.data[DOMAIN]["forecast"]["pv_tomorrow"],
        hass.data[DOMAIN]["forecast"]["pv_calibration"],
    )
    for sensor in hass.data[DOMAIN].get(FORECAST_SENSORS, []):
        sensor.async_write_ha_state()


async def planner(hass: HomeAssistant, *args, **kwargs):
    """Planner (WIP): only produces the forecast so far, no schedule."""
    await async_update_forecast(hass)
