import datetime as dt
import logging

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_utils

from . import forecast
from .battery_plan import async_request_plan
from .manual_slots import add_manual_slots
from .utils import (
    reset,
    restore_disable_state,
    store_disable_state,
    update_entities,
    write_schedule,
)
from ..const import (
    DOMAIN,
    DEFAULT_FORECAST_CALENDAR,
    DEFAULT_FORECAST_EV_SENSOR,
    DEFAULT_FORECAST_LOAD_SENSOR,
    DEFAULT_FORECAST_PV_SENSOR,
    DEFAULT_HEAT_PUMP_ENERGY_SENSOR,
    FORECAST_SENSORS,
)

_LOGGER = logging.getLogger(__name__)

HISTORY_DAYS = 35
CALIBRATION_DAYS = 14
# Hourly PV means above this are Modbus glitches
PV_MAX_KW = 25
# Returns the latest forecast and the forecast made the day before, in one request
OPEN_METEO_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
OPEN_METEO_TIMEOUT = aiohttp.ClientTimeout(total=30)


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


async def _hourly_changes(hass: HomeAssistant, statistic_id, start, end):
    """Hourly change of an energy counter in kWh (= kW mean), keyed by UTC hour."""
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        {"energy": "kWh"},
        {"change"},
    )
    return {
        dt.datetime.fromtimestamp(row["start"], dt.UTC): row["change"]
        for row in stats.get(statistic_id, [])
        if row.get("change") is not None
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


def _solar_planes(
    hass: HomeAssistant,
) -> tuple[tuple[float, float] | None, list[tuple[float, float, float]]]:
    """Return the location and the planes (tilt, compass azimuth, kWp).

    Taken from the Forecast.Solar configuration, so the planes are only set up once.
    """
    location = None
    planes = []
    for entry in hass.config_entries.async_entries("forecast_solar"):
        latitude = entry.data.get("latitude")
        longitude = entry.data.get("longitude")
        if latitude is None or longitude is None:
            continue
        location = location or (latitude, longitude)
        settings = [sub.data for sub in entry.get_subentries_of_type("plane")]
        if not settings and "declination" in entry.options:
            settings = [entry.options]  # before planes became subentries
        planes.extend(
            (
                float(plane["declination"]),
                float(plane["azimuth"]),
                float(plane["modules_power"]) / 1000,
            )
            for plane in settings
        )
    return location, planes


async def _fetch_open_meteo(session, latitude, longitude, tilt, azimuth):
    """Fetch tilted irradiance and temperature for one plane: past 15 days + 2 days.

    15 days, so yesterday's forecast can be calibrated on the 14 days before it.
    """
    variables = [
        "global_tilted_irradiance",
        "global_tilted_irradiance_previous_day1",
        "temperature_2m",
        "temperature_2m_previous_day1",
    ]
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "tilt": tilt,
        "azimuth": forecast.open_meteo_azimuth(azimuth),
        "hourly": ",".join(variables),
        "models": ",".join(forecast.OPEN_METEO_MODELS),
        "past_days": CALIBRATION_DAYS + 1,
        "forecast_days": 2,
        "timezone": "UTC",
    }
    async with session.get(
        OPEN_METEO_URL, params=params, timeout=OPEN_METEO_TIMEOUT
    ) as response:
        response.raise_for_status()
        data = await response.json()
    return data["hourly"]


async def _open_meteo_pv(
    hass: HomeAssistant,
    location: tuple[float, float],
    planes: list[tuple[float, float, float]],
):
    """Return (latest, day-before) PV power forecast in kW per UTC hour, all planes.

    Also the latest forecast's air temperature per UTC timestamp (first plane).
    """
    session = async_get_clientsession(hass)
    latitude, longitude = location
    latest, previous, temperatures = {}, {}, {}
    for tilt, azimuth, kwp in planes:
        hourly = await _fetch_open_meteo(session, latitude, longitude, tilt, azimuth)
        plane_latest, plane_previous = forecast.open_meteo_plane_power(hourly, kwp)
        for total, plane in ((latest, plane_latest), (previous, plane_previous)):
            for hour, kw in plane.items():
                total[hour] = total.get(hour, 0.0) + kw
        temperatures = temperatures or forecast.open_meteo_temperature(hourly)
    return latest, previous, temperatures


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
    calendar = (response or {}).get(entity_id)
    raw_events = calendar.get("events") if isinstance(calendar, dict) else None
    events = []
    for event in raw_events if isinstance(raw_events, list) else []:
        if not isinstance(event, dict):
            continue
        event_start, event_end = event.get("start"), event.get("end")
        # All-day events only have a date and say nothing about when the load runs
        if not isinstance(event_start, str) or not isinstance(event_end, str):
            continue
        if len(event_start) <= 10 or len(event_end) <= 10:
            continue
        start_time = dt_utils.parse_datetime(event_start)
        end_time = dt_utils.parse_datetime(event_end)
        if start_time is None or end_time is None:
            continue
        summary, description = event.get("summary"), event.get("description")
        events.append(
            (
                dt_utils.as_local(start_time),
                dt_utils.as_local(end_time),
                summary if isinstance(summary, str) else "",
                description if isinstance(description, str) else "",
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


def _day_total(hourly, start, end):
    """Sum hourly kWh from start to end, None if more than an hour is missing."""
    start_utc, end_utc = start.astimezone(dt.UTC), end.astimezone(dt.UTC)
    values = [kw for hour, kw in hourly.items() if start_utc <= hour < end_utc]
    if len(values) < (end_utc - start_utc) / dt.timedelta(hours=1) - 1:
        return None
    return round(sum(values), 2)


def _list_total(values):
    """Sum a per-quarter list, None if it has no values."""
    if values is None or all(v is None for v in values):
        return None
    return round(sum(v for v in values if v is not None), 2)


def _rounded(values):
    return (
        None if values is None else [None if v is None else round(v, 3) for v in values]
    )


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
    heat_pump_id = config.get(
        "heat_pump_energy_sensor", DEFAULT_HEAT_PUMP_ENERGY_SENSOR
    )
    calendar_id = config.get("forecast_calendar", DEFAULT_FORECAST_CALENDAR)

    now = dt_utils.now()
    today = now.date()
    yesterday = today - dt.timedelta(days=1)
    end = dt_utils.start_of_local_day(today + dt.timedelta(days=2))
    quarters = forecast.quarter_starts(dt_utils.start_of_local_day(), end)
    current = forecast.quarter_starts(now, now + forecast.QUARTER)[0]
    # Yesterday's forecast is rebuilt as it was: the load profile only uses earlier
    # days and the PV uses the forecast Open-Meteo made the day before
    yesterday_start = dt_utils.start_of_local_day(yesterday)
    yesterday_quarters = forecast.quarter_starts(yesterday_start, quarters[0])

    sources = _forecast_solar_sources(hass)
    forecast_ids = [entity for _, entity in sources if entity]
    means = await _hourly_means(
        hass,
        {load_id, pv_id, ev_id, *forecast_ids},
        dt_utils.start_of_local_day() - dt.timedelta(days=HISTORY_DAYS),
        now,
    )

    heat_pump = await _hourly_changes(
        hass,
        heat_pump_id,
        dt_utils.start_of_local_day() - dt.timedelta(days=HISTORY_DAYS),
        now,
    )
    # House load without EV charging and the heat pump, which are planned separately
    ev = means.get(ev_id, {})
    house = {
        hour: max(
            kw - max(ev.get(hour, 0.0), 0.0) - max(heat_pump.get(hour, 0.0), 0.0), 0.0
        )
        for hour, kw in means.get(load_id, {}).items()
    }
    history = {}
    for hour, kw in house.items():
        local = dt_utils.as_local(hour)
        history[(local.date(), local.hour)] = kw
    # One-off large loads are added back from the calendar or covered by the reserve
    history = forecast.cap_large_loads(history)
    load = forecast.load_quarters(history, quarters, today)
    yesterday_load = forecast.load_quarters(history, yesterday_quarters, yesterday)
    reserve = forecast.reserve_quarters(
        quarters,
        float(config.get("forecast_weekend_reserve", 4)),
        int(config.get("forecast_reserve_start", 18)),
        int(config.get("forecast_reserve_end", 22)),
    )
    planned_loads = await _planned_loads(hass, calendar_id, yesterday_start, end)
    planned_energy = [(start, stop, kwh) for start, stop, kwh, _ in planned_loads]
    planned = forecast.planned_quarters(planned_energy, quarters)
    yesterday_planned = forecast.planned_quarters(planned_energy, yesterday_quarters)
    yesterday_expected = [
        None if base is None else base + extra
        for base, extra in zip(yesterday_load, yesterday_planned, strict=True)
    ]

    # PV: Open-Meteo, or Forecast.Solar if that fails. Both are scaled by the
    # actual / forecast ratio of the last two weeks
    since = now - dt.timedelta(days=CALIBRATION_DAYS)
    pv_actual = {
        hour: kw for hour, kw in means.get(pv_id, {}).items() if 0 <= kw <= PV_MAX_KW
    }
    actual = {hour: kw for hour, kw in pv_actual.items() if hour >= since}
    yesterday_pv = None
    pv = None
    calibration = None
    raw = None
    raw_totals = {}
    pv_source = None
    temperature = None
    location, planes = _solar_planes(hass)
    if location and planes:
        try:
            latest, previous, temperatures = await _open_meteo_pv(
                hass, location, planes
            )
        except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
            _LOGGER.warning("Open-Meteo failed, using Forecast.Solar: %s", err)
        else:
            calibration = forecast.pv_calibration(
                actual, {hour: kw for hour, kw in previous.items() if hour >= since}
            )
            raw = forecast.pv_quarters(forecast.hourly_power_to_watts(latest), quarters)
            pv_source = "open-meteo"
            temperature = forecast.temperature_quarters(temperatures, quarters)
            # Yesterday: the day-before forecast, calibrated on the 14 days before it
            window = (
                yesterday_start - dt.timedelta(days=CALIBRATION_DAYS),
                yesterday_start,
            )
            yesterday_calibration = forecast.pv_calibration(
                {h: kw for h, kw in pv_actual.items() if window[0] <= h < window[1]},
                {h: kw for h, kw in previous.items() if window[0] <= h < window[1]},
            )
            yesterday_pv = [
                value * yesterday_calibration
                for value in forecast.pv_quarters(
                    forecast.hourly_power_to_watts(previous), yesterday_quarters
                )
            ]
    if raw is None and sources:
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
        pv_source = "forecast.solar"
    if raw is not None and calibration is not None:
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
        # Outdoor air temperature (°C) mid-quarter, None without Open-Meteo
        "temperature": _rounded(temperature),
        "planned_events": [
            {
                "summary": summary,
                "start": start.isoformat(),
                "end": stop.isoformat(),
                "kwh": kwh,
            }
            for start, stop, kwh, summary in planned_loads
            if stop > quarters[0]
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
        "pv_source": pv_source,
        "yesterday_starts": [q.isoformat() for q in yesterday_quarters],
        "yesterday_load": _rounded(yesterday_expected),
        "yesterday_pv": _rounded(yesterday_pv),
        "yesterday_load_kwh": _list_total(yesterday_expected),
        "yesterday_load_actual_kwh": _day_total(house, yesterday_start, quarters[0]),
        "yesterday_pv_kwh": _list_total(yesterday_pv),
        "yesterday_pv_actual_kwh": _day_total(pv_actual, yesterday_start, quarters[0]),
    }
    _LOGGER.info(
        "Forecast tomorrow: load %s kWh, PV %s kWh (%s, calibration %s)",
        hass.data[DOMAIN]["forecast"]["load_tomorrow"],
        hass.data[DOMAIN]["forecast"]["pv_tomorrow"],
        pv_source,
        hass.data[DOMAIN]["forecast"]["pv_calibration"],
    )
    for sensor in hass.data[DOMAIN].get(FORECAST_SENSORS, []):
        sensor.async_write_ha_state()


async def planner(hass: HomeAssistant, *args, **kwargs):
    """Follow the battery plan: one `auto` slot from now to the plan's end.

    The plan is recalculated every quarter, so it is not written to the slots; the
    `auto` slot makes sensor.energy_planner_battery_action follow it. Manual slots are
    laid over it as with the other planners.
    """
    await async_update_forecast(hass)
    await async_request_plan(hass, "dynamic planner")
    plan = hass.data[DOMAIN].get("plan")
    if not plan or not plan.get("starts"):
        _LOGGER.warning("No battery plan, the dynamic planner writes no schedule")
        return
    start = dt.datetime.fromisoformat(plan["starts"][0])
    end = dt.datetime.fromisoformat(plan["starts"][-1]) + dt.timedelta(minutes=15)
    await store_disable_state(hass)
    await reset(hass)
    write_schedule(hass, [{"start": start, "end": end, "state": "auto", "soc": 50}])
    await add_manual_slots(hass)
    await restore_disable_state(hass)
    await update_entities(hass)
    await hass.data[DOMAIN]["save"]()
