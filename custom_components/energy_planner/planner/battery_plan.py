"""Battery plan: runs the optimizer on the forecast, prices and tariff.

The plan is published in `hass.data[DOMAIN]["plan"]` (shown by
`sensor.energy_planner_battery_plan`). It controls nothing by itself: an `auto` slot
makes `sensor.energy_planner_battery_action` follow it (see battery_action.py), and
an inverter script follows that sensor.
"""

import asyncio
import datetime as dt
import functools
import logging
from statistics import mean

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_utils

from ..const import (
    DEFAULT_BATTERY_SOC_SENSOR,
    DEFAULT_BATTERY_VOLTAGE_SENSOR,
    DEFAULT_GRID_IMPORT_SENSOR,
    DOMAIN,
    PLAN_SENSORS,
)
from ..store import async_load_from_store, async_save_to_store
from .battery_action import QUARTER, SLOT_MODES, BatteryState, Slot, resolve
from . import ev
from .battery_optimizer import (
    QUARTER_HOURS,
    Battery,
    Limit,
    Vehicle,
    charger_powers,
    optimize,
)
from .forecast import quarter_starts
from .heat_pump import schedule
from .heating import plan_inputs
from .nordpool_utils import fetch_nordpool_data
from .price_estimate import (
    MIN_HOURS,
    WIND_FIT_DAYS,
    WIND_SITES,
    Prices,
    Wind,
    estimate,
    merge,
    scale_to,
)
from .tariff import fuse_power
from .utils import get_tariff

_LOGGER = logging.getLogger(__name__)

# Price history for the estimate of tomorrow's prices
PRICE_HISTORY_DAYS = 400
MIN_SCALE_HOURS = 4
# Day-before wind forecasts for the estimate's wind correction (price_estimate.py)
WIND_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
WIND_TIMEOUT = aiohttp.ClientTimeout(total=60)
# The plan's own copy of each day's prices (`_record_prices`): the recorder's history
# of the Nord Pool sensor loses the afternoon when its attributes pass 16 KB (from
# about 13:00, when tomorrow's prices arrive; SEK/kWh sensors, Oct 2026)
PRICE_STORE = "price_history"
# Complete day-ahead prices per bidding zone (EUR/MWh) to fill the gaps
ENERGY_CHARTS_URL = "https://api.energy-charts.info/price"


def _state_float(hass: HomeAssistant, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


async def _hourly_import(hass: HomeAssistant, statistic_id: str, start, end):
    """Grid import per local hour start in kWh (= the hourly mean kW)."""
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
        dt_utils.as_local(dt.datetime.fromtimestamp(row["start"], dt.UTC)): row[
            "change"
        ]
        for row in stats.get(statistic_id, [])
        if row.get("change") is not None
    }


async def _prices(hass: HomeAssistant) -> dict[dt.datetime, float]:
    """Spot prices (SEK/MWh excl. VAT) per quarter start, yesterday to tomorrow."""
    entity_id = hass.data[DOMAIN]["config"].get("nordpool_entity_id")
    state = hass.states.get(entity_id) if entity_id else None
    if entity_id is None or state is None:
        return {}
    currency = entity_id.split("_")[3].upper()
    area = entity_id.split("_")[2].upper()
    tomorrow_valid = state.attributes.get("tomorrow_valid")
    yesterday, today, tomorrow = await fetch_nordpool_data(
        hass, currency, area, bool(tomorrow_valid)
    )
    prices = {}
    for value in [*(yesterday or []), *(today or []), *(tomorrow or [])]:
        start = value["start"]
        if isinstance(start, str):
            start = dt.datetime.fromisoformat(start)
        prices[dt_utils.as_local(start)] = float(value["value"])
    return prices


async def _price_history(
    hass: HomeAssistant, statistic_id: str, start: dt.datetime, end: dt.datetime
) -> dict[dt.date, dict[int, float]]:
    """Return the Nord Pool sensor's price per local date and hour, in its unit.

    The recorder keeps the sensor's last state in each hour (the hour's last quarter
    since quarter prices), which is what the estimate was backtested on.
    """
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"state"},
    )
    result: dict[dt.date, dict[int, float]] = {}
    for row in stats.get(statistic_id, []):
        state = row.get("state")
        if state is not None:
            local = dt_utils.as_local(dt.datetime.fromtimestamp(row["start"], dt.UTC))
            result.setdefault(local.date(), {})[local.hour] = state
    return result


async def _wind_forecast(hass: HomeAssistant, tomorrow: dt.date) -> Wind | None:
    """Return Open-Meteo's day-before wind forecast for WIND_SITES, None on failure.

    Wind speed at 100 m (km/h) per site, local date and hour, from WIND_FIT_DAYS + 1
    days before tomorrow to tomorrow; one request for all sites.
    """
    sites = list(WIND_SITES)
    params = {
        "latitude": ",".join(str(WIND_SITES[site][0]) for site in sites),
        "longitude": ",".join(str(WIND_SITES[site][1]) for site in sites),
        "hourly": "wind_speed_100m_previous_day1",
        "start_date": (tomorrow - dt.timedelta(days=WIND_FIT_DAYS + 1)).isoformat(),
        "end_date": tomorrow.isoformat(),
        "timezone": str(dt_utils.get_default_time_zone()),
    }
    try:
        async with async_get_clientsession(hass).get(
            WIND_URL, params=params, timeout=WIND_TIMEOUT
        ) as response:
            response.raise_for_status()
            data = await response.json()
        wind: dict[str, dict[dt.date, dict[int, float]]] = {}
        for site, location in zip(sites, data, strict=True):
            hourly = location["hourly"]
            for time, speed in zip(
                hourly["time"], hourly["wind_speed_100m_previous_day1"], strict=True
            ):
                if speed is not None:
                    local = dt.datetime.fromisoformat(time)
                    wind.setdefault(site, {}).setdefault(local.date(), {})[
                        local.hour
                    ] = float(speed)
    except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError) as err:
        _LOGGER.warning("No wind forecast for the price estimate: %s", err)
        return None
    return wind


async def _stored_prices(hass: HomeAssistant) -> dict[dt.date, dict[int, float]]:
    """Return the recorded hourly prices (SEK/MWh excl. VAT), loaded once."""
    stored = hass.data[DOMAIN].get(PRICE_STORE)
    if stored is None:
        raw = await async_load_from_store(hass, PRICE_STORE)
        stored = {
            dt.date.fromisoformat(day): {int(h): float(p) for h, p in hours.items()}
            for day, hours in raw.items()
        }
        hass.data[DOMAIN][PRICE_STORE] = stored
    return stored


async def _record_prices(hass: HomeAssistant, prices: dict[dt.datetime, float]):
    """Store each complete day's hourly mean prices, for PRICE_HISTORY_DAYS."""
    stored = await _stored_prices(hass)
    changed = False
    for day, hours in _hourly(prices).items():
        if len(hours) >= MIN_HOURS and stored.get(day) != hours:
            stored[day] = hours
            changed = True
    oldest = dt_utils.now().date() - dt.timedelta(days=PRICE_HISTORY_DAYS)
    for day in [day for day in stored if day < oldest]:
        del stored[day]
        changed = True
    if changed:
        await async_save_to_store(
            hass,
            PRICE_STORE,
            {
                day.isoformat(): {str(hour): price for hour, price in hours.items()}
                for day, hours in sorted(stored.items())
            },
        )


async def _energy_charts_history(
    hass: HomeAssistant, area: str, start: dt.date, end: dt.date
) -> Prices | None:
    """Return Energy-Charts' day-ahead prices (EUR/MWh) per local date and hour.

    The mean of the hour's quarters; None on failure (e.g. an unknown zone).
    """
    params = {"bzn": area, "start": start.isoformat(), "end": end.isoformat()}
    try:
        async with async_get_clientsession(hass).get(
            ENERGY_CHARTS_URL, params=params, timeout=WIND_TIMEOUT
        ) as response:
            response.raise_for_status()
            data = await response.json()
        values: dict[dt.date, dict[int, list[float]]] = {}
        for seconds, price in zip(data["unix_seconds"], data["price"], strict=True):
            if price is not None:
                local = dt_utils.as_local(dt.datetime.fromtimestamp(seconds, dt.UTC))
                values.setdefault(local.date(), {}).setdefault(local.hour, []).append(
                    float(price)
                )
    except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError) as err:
        _LOGGER.warning("No Energy-Charts prices for the price estimate: %s", err)
        return None
    return {
        day: {hour: mean(v) for hour, v in hours.items()}
        for day, hours in values.items()
    }


def _hourly(prices: dict[dt.datetime, float]) -> dict[dt.date, dict[int, float]]:
    """Return the mean price per local date and hour."""
    values: dict[dt.date, dict[int, list[float]]] = {}
    for start, price in prices.items():
        values.setdefault(start.date(), {}).setdefault(start.hour, []).append(price)
    return {
        day: {hour: mean(v) for hour, v in hours.items()}
        for day, hours in values.items()
    }


async def _estimated_prices(
    hass: HomeAssistant, prices: dict[dt.datetime, float], now: dt.datetime
) -> dict[dt.datetime, float]:
    """Return estimated spot prices per quarter of tomorrow while it is unpublished.

    From the recorded prices (`_record_prices`), the Nord Pool sensor's history and
    Energy-Charts (price_estimate.py), the last two scaled to SEK/MWh excl. VAT by
    the known prices of the last two days, with the wind forecast
    (`_wind_forecast`) if Open-Meteo answers. Calculated once per day.
    """
    tomorrow = now.date() + dt.timedelta(days=1)
    if any(start.date() == tomorrow for start in prices):
        return {}
    cached = hass.data[DOMAIN].get("price_estimate")
    if cached and cached["day"] == tomorrow:
        return cached["prices"]
    entity_id = hass.data[DOMAIN]["config"].get("nordpool_entity_id")
    if entity_id is None:
        return {}
    start = dt_utils.start_of_local_day() - dt.timedelta(days=PRICE_HISTORY_DAYS)
    known = _hourly(prices)
    # Per day the plan's own copy, else the sensor's history, else Energy-Charts;
    # the sensor's unit, VAT and rounding and Energy-Charts' EUR are scaled to the
    # spot prices by the days both have
    sources = {"stored": await _stored_prices(hass)}
    others = {
        "recorder": await _price_history(hass, entity_id, start, now),
        "energy_charts": await _energy_charts_history(
            hass, entity_id.split("_")[2].upper(), start.date(), now.date()
        ),
    }
    for name, history in others.items():
        scale = scale_to(history, known, MIN_SCALE_HOURS) if history else None
        if history and scale is not None:
            sources[name] = {
                day: {hour: price * scale for hour, price in hours.items()}
                for day, hours in history.items()
            }
    if not any(set(history) - set(known) for history in sources.values()):
        return {}
    wind = await _wind_forecast(hass, tomorrow)
    hourly = await hass.async_add_executor_job(
        functools.partial(
            estimate,
            merge(known, *sources.values()),
            tomorrow,
            # The prices after midnight start near tonight's
            prices[max(prices)],
            wind,
        )
    )
    if hourly is None:
        return {}
    fallback = mean(hourly.values())
    start = dt_utils.start_of_local_day(tomorrow)
    estimated = {
        quarter: hourly.get(quarter.hour, fallback)
        for quarter in quarter_starts(start, start + dt.timedelta(days=1))
    }
    hass.data[DOMAIN]["price_estimate"] = {
        "day": tomorrow,
        "prices": estimated,
        "wind": wind is not None,
        # Days each source has (the merge takes them in this order)
        "sources": {name: len(history) for name, history in sources.items()},
    }
    _LOGGER.info(
        "Price estimate for %s: days per source %s, wind %s",
        tomorrow,
        hass.data[DOMAIN]["price_estimate"]["sources"],
        wind is not None,
    )
    return estimated


def _battery(hass: HomeAssistant) -> Battery | None:
    """Battery limits from the settings and the live SOC and voltage."""
    config = hass.data[DOMAIN]["config"]
    soc = _state_float(
        hass, config.get("battery_soc_sensor", DEFAULT_BATTERY_SOC_SENSOR)
    )
    voltage = _state_float(
        hass, config.get("battery_voltage_sensor", DEFAULT_BATTERY_VOLTAGE_SENSOR)
    )
    if soc is None or not voltage:
        return None
    capacity = float(config.get("battery_capacity", 0)) / 1000
    return Battery(
        min_kwh=capacity * float(config.get("battery_shutdown_soc", 10)) / 100,
        max_kwh=capacity * float(config.get("battery_max_soc", 90)) / 100,
        soc_kwh=capacity * soc / 100,
        max_grid_charge_kw=float(config.get("max_charge_current", 0)) * voltage / 1000,
        max_power_kw=float(config.get("max_discharge_current", 0)) * voltage / 1000,
        efficiency=float(config.get("price_peak_efficiency_factor", 90)) / 100,
        wear_cost=float(config.get("battery_wear_cost", 0)) / 100,
        reserve_kwh=capacity * float(config.get("battery_reserve_soc", 0)) / 100,
    )


def ev_plugged(hass: HomeAssistant) -> bool:
    """Return whether the car is plugged in (False without a charger status)."""
    status_id = hass.data[DOMAIN]["config"].get("ev_charger_status_sensor")
    state = hass.states.get(status_id) if status_id else None
    return ev.is_plugged(state.state if state is not None else None)


def _vehicle(hass: HomeAssistant, starts) -> tuple[Vehicle | None, dict | None]:
    """Return the car to plan with the battery, and what the plan publishes on it.

    Only while it is plugged in. The need is from the car's SOC and charge limit when
    both are known. Otherwise it is the largest recent charging night minus what this
    session has delivered (see planner/ev.py), or for a trip (the departure comes
    before the next working morning) a full battery, as the car stops at its own
    charge limit. Without either, or once the session has taken more than the
    history's need, the car is not planned and the action falls back to charging, so
    a car that is not full is never paused.
    """
    if not starts or not ev_plugged(hass):
        return None, None
    config = hass.data[DOMAIN]["config"]
    capacity = float(config.get("ev_battery_capacity", 75))
    ready = config.get("ev_ready_time") or dt.time(6, 30)
    departure = config.get("ev_departure")
    target = ev.ready_time(starts[0], ready, departure)
    soc = _state_float(hass, config.get("ev_soc_sensor") or "")
    limit = _state_float(hass, config.get("ev_charge_limit_sensor") or "")
    if soc is not None and limit is not None:
        need = ev.need_from_soc(soc, limit, capacity)
        source = "soc"
    else:
        delivered = _state_float(hass, config.get("ev_session_energy_sensor") or "")
        if target == departure:
            need = ev.need_from_soc(0, 100, capacity)
            source = "trip"
        else:
            need = (hass.data[DOMAIN].get("forecast") or {}).get("ev_need_kwh")
            if need is None:
                return None, None
            source = "history"
        need -= delivered or 0.0
        if need <= 0:
            return None, None
    max_kw = float(config.get("ev_charger_max_power", 11000)) / 1000
    min_kw = float(config.get("ev_charger_min_power", 0)) / 1000
    vehicle = Vehicle(
        powers_kw=charger_powers(min_kw, max_kw) if min_kw > 0 else (max_kw,),
        need_kwh=need,
        plugged=[True] * len(starts),
        deadline=ev.deadline_index(starts, ready, departure),
    )
    info = {
        "need_kwh": round(need, 2),
        "need_source": source,
        "ready": target.isoformat(),
    }
    return vehicle, info


def _forced(hass: HomeAssistant, starts) -> list[str | None]:
    """Return the mode of the manual slot covering each quarter (None: no slot).

    Only with the dynamic planner, when the plan is what runs: otherwise the plan is a
    preview, and a long manual slot (e.g. self-use for weeks) would make it useless.
    """
    if hass.data[DOMAIN]["config"].get("planner_state") != "dynamic":
        return [None] * len(starts)
    forced: list[str | None] = []
    for start in starts:
        mode = None
        for slot in hass.data[DOMAIN].get("manual_slots", []):
            if slot["start"] <= start < slot["end"]:
                mode = SLOT_MODES.get(slot["state"], mode)
        forced.append(mode)
    return forced


def _planned_mode(hass: HomeAssistant, moment: dt.datetime) -> str | None:
    """Return the mode the current plan has for the quarter containing `moment`."""
    plan = hass.data[DOMAIN].get("plan") or {}
    for start, mode in zip(plan.get("starts", []), plan.get("modes", []), strict=True):
        begin = dt.datetime.fromisoformat(start)
        if begin <= moment < begin + QUARTER:
            return mode
    return None


def expected_soc(hass: HomeAssistant, moment: dt.datetime) -> float | None:
    """Return the SOC % the plan expects at `moment`, None if it does not cover it."""
    plan = hass.data[DOMAIN].get("plan") or {}
    previous = None
    for start, soc in zip(plan.get("starts", []), plan.get("soc", []), strict=True):
        begin = dt.datetime.fromisoformat(start)
        if begin <= moment < begin + QUARTER:
            if previous is None:
                return soc
            share = (moment - begin) / QUARTER
            return previous + (soc - previous) * share
        previous = soc
    return None


def current_action(hass: HomeAssistant) -> dict:
    """Return the action now, from slot 1 and the battery plan."""
    config = hass.data[DOMAIN]["config"]
    values = hass.data[DOMAIN]["values"]
    slot = None
    if values.get("slot_1_date_time_start") is not None:
        slot = Slot(
            state=values.get("slot_1_state", "off"),
            active=bool(values.get("slot_1_active")),
            start=values.get("slot_1_date_time_start"),
            end=values.get("slot_2_date_time_start"),
            soc=float(values.get("slot_1_soc") or 0),
        )
    soc = _state_float(
        hass, config.get("battery_soc_sensor", DEFAULT_BATTERY_SOC_SENSOR)
    )
    voltage = _state_float(
        hass, config.get("battery_voltage_sensor", DEFAULT_BATTERY_VOLTAGE_SENSOR)
    )
    battery = BatteryState(
        soc=soc or 0.0,
        voltage=voltage or 0.0,
        capacity_kwh=float(config.get("battery_capacity", 0)) / 1000,
        min_soc=float(config.get("battery_shutdown_soc", 10)),
        max_soc=float(config.get("battery_max_soc", 90)),
        max_charge_a=float(config.get("max_charge_current", 0)),
        max_discharge_a=float(config.get("max_discharge_current", 0)),
        reserve_soc=float(config.get("battery_reserve_soc", 0)),
    )
    return resolve(slot, hass.data[DOMAIN].get("plan"), dt_utils.now(), battery)


def write_plan_sensors(hass: HomeAssistant) -> None:
    """Update the plan and action sensors."""
    for sensor in hass.data[DOMAIN].get(PLAN_SENSORS, []):
        if hasattr(sensor, "resolve"):
            sensor.resolve()
        sensor.async_write_ha_state()


async def async_request_plan(hass: HomeAssistant, reason: str) -> None:
    """Recalculate the plan now, or once more after a running calculation."""
    data = hass.data[DOMAIN]
    lock = data.setdefault("plan_lock", asyncio.Lock())
    if lock.locked():
        data["plan_pending"] = reason
        return
    async with lock:
        while reason:
            data["plan_pending"] = None
            _LOGGER.debug("Updating the battery plan: %s", reason)
            try:
                await async_update_plan(hass)
            except Exception:
                _LOGGER.exception("Failed to update the battery plan")
            reason = data.get("plan_pending")


def _peak_label(tariff, key: str) -> dict:
    index, month = key.split(":")
    charge = tariff.power_charges[int(index)]
    return {
        "month": month,
        "hours": [charge.period.start_hour, charge.period.end_hour],
        "kr_per_kw": charge.kr_per_kw,
    }


async def async_update_plan(hass: HomeAssistant) -> None:
    """Optimize the battery from the current quarter to the last known price."""
    prices = await _prices(hass)
    await _record_prices(hass, prices)
    # Also read by the heat pump sensors (heating.py)
    hass.data[DOMAIN]["prices"] = prices
    forecast = hass.data[DOMAIN].get("forecast")
    battery = _battery(hass)
    if not forecast or battery is None or battery.max_kwh <= battery.min_kwh:
        _LOGGER.info("No forecast or battery state, skipping the battery plan")
        return
    now = dt_utils.now()
    current = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    # Until tomorrow's prices are published the plan uses an estimate, so it can see
    # whether energy is worth keeping past midnight
    estimated = await _estimated_prices(hass, prices, now)

    starts, price_list, load, pv, temperatures = [], [], [], [], []
    is_estimate = []
    for index, start_text in enumerate(forecast["starts"]):
        start = dt_utils.as_local(dt.datetime.fromisoformat(start_text))
        if start < current:
            continue
        base = forecast["load"][index]
        price = prices.get(start, estimated.get(start))
        if price is None or base is None:
            break
        starts.append(start)
        price_list.append(price)
        is_estimate.append(start not in prices)
        load.append(base + forecast["planned"][index] + forecast["reserve"][index])
        pv.append(forecast["pv"][index] if forecast["pv"] else 0.0)
        temperatures.append(
            forecast["temperature"][index] if forecast.get("temperature") else None
        )
    if not starts:
        _LOGGER.info("No prices for the forecast, skipping the battery plan")
        return

    config = hass.data[DOMAIN]["config"]
    tariff = get_tariff(hass)
    limit = Limit(
        float(config.get("grid_import_limit", 0)),
        int(config.get("grid_import_limit_start", 6)),
        int(config.get("grid_import_limit_end", 23)),
        fuse_power(float(config.get("main_fuse", 0) or 0)),
    )
    vehicle, car = _vehicle(hass, starts)
    month_start = dt_utils.start_of_local_day(now.date().replace(day=1))
    history = await _hourly_import(
        hass,
        config.get("grid_import_sensor", DEFAULT_GRID_IMPORT_SENSOR),
        month_start,
        now,
    )
    run = functools.partial(
        optimize,
        starts,
        price_list,
        battery=battery,
        tariff=tariff,
        limit=limit,
        history=history,
        forced=_forced(hass, starts),
        current_mode=_planned_mode(hass, now),
        vehicle=vehicle,
    )
    # The heat pump runs where its heat is cheaper than district heating at what more
    # load costs in this plan (Plan.marginal); then the battery is planned with it.
    # Its load is exempt from the import limit, only the power charges count
    heat = plan_inputs(hass, starts, temperatures)
    plan = await hass.async_add_executor_job(
        functools.partial(run, load=load, pv=pv, probe_kwh=heat.probe_kwh)
    )
    marginal = [round(m * 100, 1) for m in plan.marginal]
    heat_pump = (
        schedule(marginal, heat.cops, heat.district, heat.heating, heat.margin)
        if heat.probe_kwh > 0
        else [False] * len(starts)
    )
    if any(heat_pump):
        extra = [
            kwh if on else 0.0 for kwh, on in zip(heat.draws, heat_pump, strict=True)
        ]
        plan = await hass.async_add_executor_job(
            functools.partial(
                run,
                load=[a + b for a, b in zip(load, extra, strict=True)],
                pv=pv,
                exempt=extra,
            )
        )

    capacity = float(config.get("battery_capacity", 0)) / 1000
    hass.data[DOMAIN]["plan"] = {
        "updated": now.isoformat(),
        "starts": [s.isoformat() for s in plan.starts],
        "modes": plan.modes,
        "soc": [round(kwh / capacity * 100, 1) for kwh in plan.soc],
        "grid_import": [round(kwh / QUARTER_HOURS, 2) for kwh in plan.grid_import],
        "grid_export": [round(kwh / QUARTER_HOURS, 2) for kwh in plan.grid_export],
        "targets": [None if t is None else round(t, 2) for t in plan.targets],
        # Power charge level (kW): the import target below the reserve SOC
        "peak_levels": [None if t is None else round(t, 2) for t in plan.peak_levels],
        # Spot price incl. VAT (öre/kWh), and whether it is an estimate
        "prices": [round(p * 1.25 / 10, 2) for p in price_list],
        "estimated": is_estimate,
        # Heat pump: on per quarter, what more load costs (öre/kWh), its COP and draw
        # (kW while heating)
        "heat_pump": heat_pump,
        "marginal": marginal,
        "temperature": [None if t is None else round(t, 1) for t in heat.temperatures],
        "heat_pump_cop": [None if c is None else round(c, 2) for c in heat.cops],
        "heat_pump_power": [round(kwh / QUARTER_HOURS, 3) for kwh in heat.draws],
        # The car while plugged in: charging power per quarter (kW), the need and
        # what the plan can not deliver by the ready time (None: not planned)
        "ev": None
        if car is None
        else {
            **car,
            "power": [round(kwh / QUARTER_HOURS, 2) for kwh in plan.ev_kwh],
            "planned_kwh": round(sum(plan.ev_kwh), 2),
            "missing_kwh": round(plan.ev_missing_kwh, 2),
        },
        "mode": plan.modes[0],
        "target_kw": None if plan.targets[0] is None else round(plan.targets[0], 2),
        "energy_cost": round(plan.energy_cost, 2),
        "wear_cost": round(plan.wear_cost, 2),
        "power_cost": round(plan.power_cost, 2),
        "end_value": round(plan.end_value, 2),
        "limit_excess_kwh": round(plan.limit_excess_kwh, 2),
        "fuse_excess_kwh": round(plan.fuse_excess_kwh, 2),
        "peaks": [
            {
                **_peak_label(tariff, key),
                "reached_kw": round(before, 2),
                "planned_kw": round(after, 2),
            }
            for key, (before, after) in plan.peaks.items()
        ],
    }
    _LOGGER.info(
        "Battery plan until %s: %s now, energy %.2f SEK, wear %.2f SEK, "
        "power charge +%.2f SEK, heat pump on %d of %d quarters, %d estimated prices",
        starts[-1].isoformat(),
        plan.modes[0],
        plan.energy_cost,
        plan.wear_cost,
        plan.power_cost,
        sum(heat_pump),
        len(heat_pump),
        sum(is_estimate),
    )
    write_plan_sensors(hass)
