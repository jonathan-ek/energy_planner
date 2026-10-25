import datetime as dt
import functools
import logging

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_utils

from . import ev as ev_need
from . import forecast, heat_pump, heating, settings
from .battery_plan import async_request_plan
from .manual_slots import add_manual_slots
from .utils import (
    reset,
    restore_disable_state,
    store_disable_state,
    update_entities,
    write_schedule,
)
from ..const import DOMAIN, FORECAST_SENSORS

_LOGGER = logging.getLogger(__name__)

HISTORY_DAYS = 35
CALIBRATION_DAYS = 14
# Today, tomorrow and the day after: the battery plan uses the third day once
# tomorrow's prices are published (it then estimates that day's prices)
FORECAST_DAYS = 3
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


async def _heating_shares(hass: HomeAssistant, entity_id, start, end):
    """Share of each UTC hour the climate entity was in `heat` mode."""
    states = await get_instance(hass).async_add_executor_job(
        functools.partial(
            state_changes_during_period,
            hass,
            start,
            end,
            entity_id,
            no_attributes=True,
            include_start_time_state=True,
        )
    )
    changes = [
        (max(state.last_changed, start), state.state == "heat")
        for state in states.get(entity_id, [])
    ]
    return forecast.heating_shares(changes, end)


def _local_hours(hourly: dict[dt.datetime, float]) -> dict[tuple[dt.date, int], float]:
    """Key hourly values by (local date, hour) instead of the UTC hour start."""
    result = {}
    for hour, value in hourly.items():
        local = dt_utils.as_local(hour)
        result[(local.date(), local.hour)] = value
    return result


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
    A disabled entry is left out (e.g. panels with a fault), as in the fallback.
    """
    location = None
    planes = []
    for entry in hass.config_entries.async_entries("forecast_solar"):
        if entry.disabled_by is not None:
            continue
        latitude = entry.data.get("latitude")
        longitude = entry.data.get("longitude")
        if latitude is None or longitude is None:
            continue
        location = location or (latitude, longitude)
        entry_planes = [sub.data for sub in entry.get_subentries_of_type("plane")]
        if not entry_planes and "declination" in entry.options:
            entry_planes = [entry.options]  # before planes became subentries
        planes.extend(
            (
                float(plane["declination"]),
                float(plane["azimuth"]),
                float(plane["modules_power"]) / 1000,
            )
            for plane in entry_planes
        )
    return location, planes


async def _fetch_open_meteo(session, latitude, longitude, tilt, azimuth):
    """Fetch tilted irradiance and temperature for one plane: past 15 days + 3 days.

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
        "forecast_days": FORECAST_DAYS,
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


async def _calendar_events(
    hass: HomeAssistant, entity_id, start, end, *, all_day=False
):
    """Return events (start, end, summary, description) from a calendar.

    All-day events (from local midnight) only with `all_day`.
    """
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
        if not isinstance(event_start, str) or not isinstance(event_end, str):
            continue
        if len(event_start) <= 10 or len(event_end) <= 10:
            # All-day events only have a date and say nothing about when a load runs
            if not all_day:
                continue
            start_date = dt_utils.parse_date(event_start)
            end_date = dt_utils.parse_date(event_end)
            if start_date is None or end_date is None:
                continue
            start_time = dt_utils.start_of_local_day(start_date)
            end_time = dt_utils.start_of_local_day(end_date)
        else:
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


async def _away_periods(hass: HomeAssistant, calendar_id, start, end):
    """Return (start, end) of calendar events saying nobody is home, all-day too."""
    try:
        events = await _calendar_events(hass, calendar_id, start, end, all_day=True)
    except Exception:
        _LOGGER.exception("Failed to read away events from %s", calendar_id)
        return []
    return [
        (event_start, event_end)
        for event_start, event_end, summary, _ in events
        if forecast.is_away_event(summary)
    ]


async def _planned_loads(hass: HomeAssistant, calendar_id, start, end):
    """Planned loads (start, end, kWh, summary) from the calendar."""
    try:
        events = await _calendar_events(hass, calendar_id, start, end)
    except Exception:
        _LOGGER.exception("Failed to read planned loads from %s", calendar_id)
        return []
    planned = []
    for event_start, event_end, summary, description in events:
        if forecast.is_away_event(summary):
            continue
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


def _rounded_value(value: float | None) -> float | None:
    """Round a value to 2 decimals, keeping None."""
    return None if value is None else round(value, 2)


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
    """Forecast load and PV per quarter for all of today and the next two days.

    Today's forecast only uses days before today, so it is the same all day and can
    be compared with what actually happens. Totals for today count from now.
    """
    config = hass.data[DOMAIN]["config"]
    heat_pump_config = settings.heat_pump(config)
    load_id = settings.get(config, "forecast_load_sensor")
    pv_id = settings.get(config, "forecast_pv_sensor")
    ev_id = settings.get(config, "forecast_ev_sensor")
    heat_pump_id = heat_pump_config.climate
    indoor_id = heat_pump_config.indoor_sensor
    outdoor_id = heat_pump_config.outdoor_sensor
    calendar_id = settings.get(config, "forecast_calendar")

    now = dt_utils.now()
    today = now.date()
    yesterday = today - dt.timedelta(days=1)
    end = dt_utils.start_of_local_day(today + dt.timedelta(days=FORECAST_DAYS))
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
        {load_id, pv_id, ev_id, indoor_id, outdoor_id, *forecast_ids},
        dt_utils.start_of_local_day() - dt.timedelta(days=HISTORY_DAYS),
        now,
    )

    shares = await _heating_shares(
        hass,
        heat_pump_id,
        dt_utils.start_of_local_day() - dt.timedelta(days=HISTORY_DAYS),
        now,
    )
    # House load without EV charging and the heat pump, which are planned separately.
    # The heat pump has no reliable meter, so its draw is measured from the load
    ev = means.get(ev_id, {})
    without_ev = {
        hour: max(kw - max(ev.get(hour, 0.0), 0.0), 0.0)
        for hour, kw in means.get(load_id, {}).items()
    }
    # and its heat loss (kW of heat per °C indoor-outdoor) from that
    indoor = means.get(indoor_id, {})
    outdoor = means.get(outdoor_id, {})
    curve = heating.curve(hass)
    loss, loss_hours = heat_pump.heat_loss(
        forecast.heat_pump_excess(_local_hours(without_ev), _local_hours(shares)),
        _local_hours(indoor),
        _local_hours(outdoor),
        curve,
    )
    setting = heat_pump_config.power_kw

    def heat_pump_kw(hour):
        """Return what the heat pump drew while heating in an hour (kW)."""
        if loss is None or indoor.get(hour) is None or outdoor.get(hour) is None:
            return setting
        return heat_pump.draw(loss, indoor[hour], outdoor[hour], curve)

    house = {
        hour: max(kw - shares.get(hour, 0.0) * heat_pump_kw(hour), 0.0)
        for hour, kw in without_ev.items()
    }
    history = _local_hours(house)
    # One-off large loads are added back from the calendar or covered by the reserve
    history = forecast.cap_large_loads(history)
    load = forecast.load_quarters(history, quarters, today)
    yesterday_load = forecast.load_quarters(history, yesterday_quarters, yesterday)
    # Nobody home: no reserve for unplanned load (here and the battery's reserve SOC)
    away = forecast.away_quarters(
        await _away_periods(hass, calendar_id, quarters[0], end), quarters
    )
    reserve = forecast.reserve_quarters(
        quarters,
        settings.number(config, "forecast_weekend_reserve"),
        int(settings.number(config, "forecast_reserve_start")),
        int(settings.number(config, "forecast_reserve_end")),
        away,
    )
    planned_loads = await _planned_loads(hass, calendar_id, yesterday_start, end)
    planned_energy = [(start, stop, kwh) for start, stop, kwh, _ in planned_loads]
    planned = forecast.planned_quarters(planned_energy, quarters)
    yesterday_planned = forecast.planned_quarters(planned_energy, yesterday_quarters)

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
    temperatures = {}
    # Hourly calibration: a factor per local hour of the day, for shaded panels
    pv_model = settings.get(config, "forecast_pv_calibration")
    hourly = None

    def hour_factors(produced, predicted):
        """Return the factor per local hour with the hourly calibration, else None."""
        if pv_model != "hourly":
            return None
        return forecast.pv_hourly_calibration(
            _local_hours(produced), _local_hours(predicted)
        )

    location, planes = _solar_planes(hass)
    if location and planes:
        try:
            latest, previous, temperatures = await _open_meteo_pv(
                hass, location, planes
            )
        except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
            _LOGGER.warning("Open-Meteo failed, using Forecast.Solar: %s", err)
        else:
            recent = {hour: kw for hour, kw in previous.items() if hour >= since}
            calibration = forecast.pv_calibration(actual, recent)
            hourly = hour_factors(actual, recent)
            raw = forecast.pv_quarters(forecast.hourly_power_to_watts(latest), quarters)
            pv_source = "open-meteo"
            temperature = forecast.temperature_quarters(temperatures, quarters)
            # Yesterday: the day-before forecast, calibrated on the 14 days before it
            window = (
                yesterday_start - dt.timedelta(days=CALIBRATION_DAYS),
                yesterday_start,
            )
            produced = {
                h: kw for h, kw in pv_actual.items() if window[0] <= h < window[1]
            }
            predicted = {
                h: kw for h, kw in previous.items() if window[0] <= h < window[1]
            }
            yesterday_pv = forecast.calibrated_quarters(
                forecast.pv_quarters(
                    forecast.hourly_power_to_watts(previous), yesterday_quarters
                ),
                yesterday_quarters,
                forecast.pv_calibration(produced, predicted),
                hour_factors(produced, predicted),
            )
    if raw is None and sources:
        predicted = {}
        for entity in forecast_ids:
            for hour, kw in means.get(entity, {}).items():
                if hour >= since:
                    predicted[hour] = predicted.get(hour, 0.0) + kw
        calibration = forecast.pv_calibration(actual, predicted)
        hourly = hour_factors(actual, predicted)
        raw = [0.0] * len(quarters)
        for watts, _ in sources:
            plane = forecast.pv_quarters(watts, quarters)
            raw = [a + b for a, b in zip(raw, plane, strict=True)]
        pv_source = "forecast.solar"
    if raw is not None and calibration is not None:
        raw_totals = _daily_totals(quarters, raw, current)
        pv = forecast.calibrated_quarters(raw, quarters, calibration, hourly)
    if pv is not None and pv_source == "forecast.solar":
        # Forecast.Solar may not reach the last day: no PV there (not 0), so the
        # battery plan ends where the forecast does
        covered = min(
            (
                dt_utils.start_of_local_day(
                    dt_utils.as_local(max(watts)).date() + dt.timedelta(days=1)
                )
                for watts, _ in sources
                if watts
            ),
            default=quarters[0],
        )
        pv = [
            value if start < covered else None
            for start, value in zip(quarters, pv, strict=True)
        ]

    # Houses heated with electricity: the profile corrected for the outdoor temperature,
    # measured for the history days, Open-Meteo's for the forecast days
    load_model = settings.get(config, "forecast_load_model")
    slope = None
    if load_model == "temperature" and temperatures:
        measured = forecast.daily_means(_local_hours(outdoor))
        expected = forecast.daily_means(_local_hours(temperatures))
        slope = forecast.heating_slope(history, measured, today)
        load = forecast.temperature_load_quarters(
            history, quarters, today, measured, expected
        )
        yesterday_load = forecast.temperature_load_quarters(
            history, yesterday_quarters, yesterday, measured, expected
        )
    yesterday_expected = [
        None if base is None else base + extra
        for base, extra in zip(yesterday_load, yesterday_planned, strict=True)
    ]

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
        "away": away,
        "pv": _rounded(pv),
        # Outdoor air temperature (°C) mid-quarter, None without Open-Meteo
        "temperature": _rounded(temperature),
        # Measured heat pump heat loss (kW per °C), None until enough hours exist
        "heat_pump_loss": None if loss is None else round(loss, 3),
        "heat_pump_loss_hours": loss_hours,
        "load_model": load_model,
        # The car's need from its charging nights (kWh at the plug), see planner/ev.py
        "ev_need_kwh": _rounded_value(
            ev_need.need_from_history(_local_hours(ev), today)
        ),
        # Temperature model: daily load change per °C warmer (kWh/°C), 0 = none
        "heating_slope": None if slope is None else round(slope, 2),
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
        "pv_calibration_model": pv_model,
        # Hourly calibration: the factor per local hour of the day (others: daily)
        "pv_hourly_calibration": None
        if hourly is None
        else {hour: round(factor, 3) for hour, factor in sorted(hourly.items())},
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
    requested = dt_utils.now()
    # Wait for a calculation already running, and the one after it
    await async_request_plan(hass, "dynamic planner", wait=True)
    plan = hass.data[DOMAIN].get("plan")
    # Planned for a moment after the request, so on the new forecast
    if plan is None or plan.updated < requested:
        _LOGGER.warning(
            "No new battery plan, the dynamic planner keeps the schedule it has"
        )
        return
    start, end = plan.start, plan.end
    await store_disable_state(hass)
    await reset(hass)
    write_schedule(hass, [{"start": start, "end": end, "state": "auto", "soc": 50}])
    await add_manual_slots(hass)
    await restore_disable_state(hass)
    await update_entities(hass)
    await hass.data[DOMAIN]["save"]()
