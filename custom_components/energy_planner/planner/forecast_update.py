"""The forecast's Home Assistant glue: fetch the inputs, build, publish.

`async_update_forecast` reads the recorder's hourly statistics, the heat pump's
`heat` hours, the calendar (planned loads, away periods), Open-Meteo (PV per panel
plane from the Forecast.Solar configuration, and the temperature) or else
Forecast.Solar's estimates into a `forecast.ForecastInputs`; the pure
`forecast.build_forecast` turns them into a `Forecast`, which is stored in
`hass.data[DOMAIN]["forecast"]` and shown by the two forecast sensors.
"""

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

from . import forecast, heating, settings
from ..const import DOMAIN, FORECAST_SENSORS

_LOGGER = logging.getLogger(__name__)

# Days of hourly history the load profile and the measurements use
HISTORY_DAYS = 35
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
        "past_days": forecast.CALIBRATION_DAYS + 1,
        "forecast_days": forecast.FORECAST_DAYS,
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


async def async_update_forecast(hass: HomeAssistant) -> None:
    """Fetch the inputs, build the forecast and publish it."""
    config = hass.data[DOMAIN]["config"]
    heat_pump_config = settings.heat_pump(config)
    load_id = settings.get(config, "forecast_load_sensor")
    pv_id = settings.get(config, "forecast_pv_sensor")
    ev_id = settings.get(config, "forecast_ev_sensor")
    indoor_id = heat_pump_config.indoor_sensor
    outdoor_id = heat_pump_config.outdoor_sensor
    calendar_id = settings.get(config, "forecast_calendar")

    now = dt_utils.now()
    today = now.date()
    history_start = dt_utils.start_of_local_day() - dt.timedelta(days=HISTORY_DAYS)
    yesterday_start = dt_utils.start_of_local_day(today - dt.timedelta(days=1))
    midnight = dt_utils.start_of_local_day()
    end = dt_utils.start_of_local_day(today + dt.timedelta(days=forecast.FORECAST_DAYS))

    sources = _forecast_solar_sources(hass)
    forecast_ids = [entity for _, entity in sources if entity]
    means = await _hourly_means(
        hass,
        {load_id, pv_id, ev_id, indoor_id, outdoor_id, *forecast_ids},
        history_start,
        now,
    )
    shares = await _heating_shares(hass, heat_pump_config.climate, history_start, now)
    open_meteo = None
    location, planes = _solar_planes(hass)
    if location and planes:
        try:
            open_meteo = await _open_meteo_pv(hass, location, planes)
        except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
            _LOGGER.warning("Open-Meteo failed, using Forecast.Solar: %s", err)

    inputs = forecast.ForecastInputs(
        now=now,
        tz=dt_utils.get_default_time_zone(),
        load=means.get(load_id, {}),
        pv=means.get(pv_id, {}),
        ev=means.get(ev_id, {}),
        indoor=means.get(indoor_id, {}),
        outdoor=means.get(outdoor_id, {}),
        heating_shares=shares,
        planned_loads=await _planned_loads(hass, calendar_id, yesterday_start, end),
        away_periods=await _away_periods(hass, calendar_id, midnight, end),
        open_meteo=open_meteo,
        forecast_solar=[
            # A copy: Forecast.Solar's coordinator may replace it while the build
            # runs in the executor
            (dict(watts), means.get(entity, {}) if entity else {})
            for watts, entity in sources
        ],
        load_model=settings.get(config, "forecast_load_model"),
        pv_calibration=settings.get(config, "forecast_pv_calibration"),
        weekend_reserve_kwh=settings.number(config, "forecast_weekend_reserve"),
        reserve_start=int(settings.number(config, "forecast_reserve_start")),
        reserve_end=int(settings.number(config, "forecast_reserve_end")),
        heat_pump_power_kw=heat_pump_config.power_kw,
        cop_curve=heating.curve(hass),
    )
    # A few hundred thousand operations: off the event loop, like the optimizer
    result = await hass.async_add_executor_job(forecast.build_forecast, inputs)
    hass.data[DOMAIN]["forecast"] = result
    details = result.details
    _LOGGER.info(
        "Forecast tomorrow: load %s kWh, PV %s kWh (%s, calibration %s)",
        details["load_tomorrow"],
        details["pv_tomorrow"],
        details["pv_source"],
        details["pv_calibration"],
    )
    for sensor in hass.data[DOMAIN].get(FORECAST_SENSORS, []):
        sensor.async_write_ha_state()
