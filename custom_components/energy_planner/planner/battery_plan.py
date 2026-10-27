"""Battery plan: runs the optimizer on the forecast, prices and tariff.

The plan (a `plan.Plan`) is published in `hass.data[DOMAIN]["plan"]` (shown by
`sensor.energy_planner_battery_plan`). It controls nothing by itself:
`sensor.energy_planner_battery_action` follows it while
`switch.energy_planner_enabled` is on, and manual slots override it (see
battery_action.py); an inverter script follows that sensor.
"""

import asyncio
import dataclasses
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

from ..const import DOMAIN, PLAN_SENSORS
from ..store import async_load_from_store, async_save_to_store
from . import manual
from .battery_action import BatteryState, resolve
from . import ev, settings
from .forecast import Forecast
from .battery_optimizer import (
    QUARTER_HOURS,
    Battery,
    Limit,
    Vehicle,
    charger_powers,
    optimize,
)
from .heat_pump import schedule
from .heating import plan_inputs
from .nordpool_utils import fetch_nordpool_data
from .plan import EvPlan, Plan
from .timeline import Timeline, quarter_start
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

# Price history for the estimate of the first day without published prices
PRICE_HISTORY_DAYS = 400
MIN_SCALE_HOURS = 4
# The estimate covers the day after the published prices, at most the day after
# tomorrow (lead 2). One more day ahead saved 12-40 kr/year (backtest Oct 2026)
ESTIMATE_MAX_LEAD = 2
# Day-before wind forecasts for the estimate's wind correction (price_estimate.py)
WIND_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
WIND_TIMEOUT = aiohttp.ClientTimeout(total=60)
# The plan's own copy of each day's prices (`_record_prices`): the recorder's history
# of the Nord Pool sensor loses the afternoon when its attributes pass 16 KB (from
# about 13:00, when tomorrow's prices arrive; SEK/kWh sensors, Oct 2026)
PRICE_STORE = "price_history"
# Complete day-ahead prices per bidding zone (EUR/MWh) to fill the gaps
ENERGY_CHARTS_URL = "https://api.energy-charts.info/price"


async def _hourly_import(hass: HomeAssistant, statistic_id: str, start, end):
    """Grid import per hour start (UTC) in kWh (= the hourly mean kW).

    UTC keys: local ones would make the two hours when DST ends one.
    """
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


async def _prices(hass: HomeAssistant) -> dict[dt.datetime, float]:
    """Spot prices (SEK/MWh excl. VAT) per quarter start in UTC, yesterday to tomorrow.

    UTC keys: local times in the hour repeated when DST ends are equal (and hash
    alike) in one zone, so the two 02:xx hours would share their prices.
    """
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
        # A time without an offset is in HA's time zone (as_utc), as before
        prices[quarter_start(dt_utils.as_utc(start))] = float(value["value"])
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


async def _wind_forecast(
    hass: HomeAssistant, day: dt.date, lead: int = 1
) -> Wind | None:
    """Return Open-Meteo's wind forecast made `lead` days before, None on failure.

    Wind speed at 100 m (km/h) per site in WIND_SITES, local date and hour, from
    WIND_FIT_DAYS + 1 days before `day` to `day`; one request for all sites. The
    estimate's wind correction is fitted on the past days of the same lead, so the
    day after tomorrow (lead 2) is corrected as a two days old forecast predicts.
    """
    sites = list(WIND_SITES)
    variable = f"wind_speed_100m_previous_day{lead}"
    params = {
        "latitude": ",".join(str(WIND_SITES[site][0]) for site in sites),
        "longitude": ",".join(str(WIND_SITES[site][1]) for site in sites),
        "hourly": variable,
        "start_date": (day - dt.timedelta(days=WIND_FIT_DAYS + 1)).isoformat(),
        "end_date": day.isoformat(),
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
            for time, speed in zip(hourly["time"], hourly[variable], strict=True):
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
        local = dt_utils.as_local(start)
        values.setdefault(local.date(), {}).setdefault(local.hour, []).append(price)
    return {
        day: {hour: mean(v) for hour, v in hours.items()}
        for day, hours in values.items()
    }


async def _estimated_prices(
    hass: HomeAssistant, prices: dict[dt.datetime, float], now: dt.datetime
) -> dict[dt.datetime, float]:
    """Return estimated spot prices per quarter (UTC) after the published ones.

    That is tomorrow until its prices are published (about 13:00), then the day after
    tomorrow, so the plan always sees a day past the night ahead. From the recorded
    prices (`_record_prices`), the Nord Pool sensor's history and Energy-Charts
    (price_estimate.py), the last two scaled to SEK/MWh excl. VAT by the known prices
    of the last two days, with the wind forecast made as many days before
    (`_wind_forecast`) if Open-Meteo answers. Calculated once per day and lead, so
    the evening's estimate of the day after tomorrow is made again at midnight with
    a fresher wind forecast.
    """
    if not prices:
        return {}
    day = max(dt_utils.as_local(max(prices)).date(), now.date()) + dt.timedelta(days=1)
    lead = (day - now.date()).days
    if lead > ESTIMATE_MAX_LEAD:
        return {}
    cached = hass.data[DOMAIN].get("price_estimate")
    if cached and cached["day"] == day and cached.get("lead") == lead:
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
    wind = await _wind_forecast(hass, day, lead)
    hourly = await hass.async_add_executor_job(
        functools.partial(
            estimate,
            merge(known, *sources.values()),
            day,
            # The prices after midnight start near tonight's
            prices[max(prices)],
            wind,
        )
    )
    if hourly is None:
        return {}
    fallback = mean(hourly.values())
    quarters = Timeline.between(
        dt_utils.start_of_local_day(day),
        dt_utils.start_of_local_day(day + dt.timedelta(days=1)),
        dt_utils.get_default_time_zone(),
    )
    estimated = {
        start: hourly.get(quarters.local_hour(index), fallback)
        for index, start in enumerate(quarters.starts)
    }
    hass.data[DOMAIN]["price_estimate"] = {
        "day": day,
        "lead": lead,
        "prices": estimated,
        "wind": wind is not None,
        # Days each source has (the merge takes them in this order)
        "sources": {name: len(history) for name, history in sources.items()},
    }
    _LOGGER.info(
        "Price estimate for %s: days per source %s, wind %s",
        day,
        hass.data[DOMAIN]["price_estimate"]["sources"],
        wind is not None,
    )
    return estimated


def _battery(hass: HomeAssistant) -> Battery | None:
    """Battery limits from the settings and the live SOC and voltage."""
    config = settings.battery(hass.data[DOMAIN]["config"])
    soc = settings.reading(hass, config.soc_sensor)
    voltage = settings.reading(hass, config.voltage_sensor)
    if soc is None or not voltage:
        return None
    capacity = config.capacity_kwh
    return Battery(
        min_kwh=capacity * config.shutdown_soc / 100,
        max_kwh=capacity * config.max_soc / 100,
        soc_kwh=capacity * soc / 100,
        max_grid_charge_kw=config.max_charge_a * voltage / 1000,
        max_power_kw=config.max_discharge_a * voltage / 1000,
        efficiency=config.efficiency,
        wear_cost=config.wear_sek_per_kwh,
        reserve_kwh=capacity * config.reserve_soc / 100,
    )


def ev_plugged(hass: HomeAssistant) -> bool:
    """Return whether the car is plugged in (False without a charger status)."""
    car = settings.car(hass.data[DOMAIN]["config"])
    state = hass.states.get(car.status_sensor) if car is not None else None
    return ev.is_plugged(state.state if state is not None else None)


def _vehicle(
    hass: HomeAssistant, quarters: Timeline
) -> tuple[Vehicle, EvPlan] | tuple[None, None]:
    """Return the car to plan with the battery, and its need as the plan publishes it.

    The `EvPlan`'s charging is filled in from the optimizer's result.

    Only while it is plugged in. The need is from the car's SOC and charge limit when
    both are known. Otherwise it is the largest recent charging night minus what this
    session has delivered (see planner/ev.py), or for a trip (the departure comes
    before the next working morning) a full battery, as the car stops at its own
    charge limit. Without either, or once the session has taken more than the
    history's need, the car is not planned and the action falls back to charging, so
    a car that is not full is never paused.
    """
    car = settings.car(hass.data[DOMAIN]["config"])
    if not len(quarters) or car is None or not ev_plugged(hass):
        return None, None
    capacity = car.battery_kwh
    ready = car.ready_time
    departure = car.departure
    target = ev.ready_time(quarters.local(0), ready, departure)
    soc = settings.reading(hass, car.soc_sensor)
    limit = settings.reading(hass, car.charge_limit_sensor)
    if soc is not None and limit is not None:
        need = ev.need_from_soc(soc, limit, capacity)
        source = "soc"
    else:
        delivered = settings.reading(hass, car.session_energy_sensor)
        if target == departure:
            need = ev.need_from_soc(0, 100, capacity)
            source = "trip"
        else:
            forecast = hass.data[DOMAIN].get("forecast")
            need = forecast.ev_need_kwh if forecast is not None else None
            if need is None:
                return None, None
            source = "history"
        need -= delivered or 0.0
        if need <= 0:
            return None, None
    max_kw = car.charger_max_kw
    min_kw = car.charger_min_kw
    vehicle = Vehicle(
        powers_kw=charger_powers(min_kw, max_kw) if min_kw > 0 else (max_kw,),
        need_kwh=need,
        plugged=[True] * len(quarters),
        deadline=ev.deadline_index(quarters, ready, departure),
    )
    return vehicle, EvPlan(
        need_kwh=round(need, 2),
        need_source=source,
        ready=target,
        power_kw=(),
        planned_kwh=0.0,
        missing_kwh=0.0,
    )


def manual_slot_attributes(hass: HomeAssistant) -> list[dict]:
    """Return the manual slots as the plan sensor publishes them.

    Local times like the plan's, and the target SOC they run to (None: no target).
    """
    limits = _soc_limits(hass.data[DOMAIN]["config"])
    return [
        {
            "start": dt_utils.as_local(slot.start).isoformat(),
            "end": dt_utils.as_local(slot.end).isoformat(),
            "state": slot.state,
            "soc": manual.target(slot, limits),
        }
        for slot in hass.data[DOMAIN].get("manual_slots", [])
    ]


def _soc_limits(config) -> manual.SocLimits:
    """Return the battery's SOC settings that manual slot targets are limited to."""
    battery = settings.battery(config)
    return manual.SocLimits(battery.shutdown_soc, battery.max_soc, battery.reserve_soc)


async def _prune_manual_slots(hass: HomeAssistant, now: dt.datetime) -> None:
    """Drop the manual slots that have ended, and store the rest."""
    slots = hass.data[DOMAIN].get("manual_slots", [])
    kept = manual.current(slots, now)
    if len(kept) != len(slots):
        hass.data[DOMAIN]["manual_slots"] = kept
        await hass.data[DOMAIN]["save"]()


def current_action(hass: HomeAssistant) -> dict:
    """Return the action now, from the manual slots and the battery plan."""
    data = hass.data[DOMAIN]
    config = settings.battery(data["config"])
    now = dt_utils.now()
    battery = BatteryState(
        soc=settings.reading(hass, config.soc_sensor),
        voltage=settings.reading(hass, config.voltage_sensor) or 0.0,
        capacity_kwh=config.capacity_kwh,
        min_soc=config.shutdown_soc,
        max_soc=config.max_soc,
        max_charge_a=config.max_charge_a,
        max_discharge_a=config.max_discharge_a,
        reserve_soc=config.reserve_soc,
    )
    return resolve(
        manual.running(data.get("manual_slots", []), now),
        data.get("plan"),
        now,
        battery,
        enabled=settings.get(data["config"], "enabled"),
    )


def car_action(hass: HomeAssistant, now: dt.datetime | None = None) -> dict:
    """Return whether the car should charge now (see ev.ev_action)."""
    return ev.ev_action(
        hass.data[DOMAIN].get("plan"), now or dt_utils.now(), ev_plugged(hass)
    )


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
        # The running calculation runs it next
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
    # The published prices, also without a plan: the heat pump sensors (heating.py)
    hass.data[DOMAIN]["spot_prices"] = prices
    # Also when the plan can not be made
    await _prune_manual_slots(hass, dt_utils.now())
    forecast: Forecast | None = hass.data[DOMAIN].get("forecast")
    battery = _battery(hass)
    if not forecast or battery is None or battery.max_kwh <= battery.min_kwh:
        _LOGGER.info("No forecast or battery state, skipping the battery plan")
        return
    now = dt_utils.now()
    current = quarter_start(now)
    # The day after the published prices is estimated, so the plan can see whether
    # energy is worth keeping past the coming night
    estimated = await _estimated_prices(hass, prices, now)

    # From the current quarter while the forecast knows load and PV and a price
    # (published or estimated) exists
    starts, price_list, load, pv, temperatures = [], [], [], [], []
    is_estimate = []
    quarters = forecast.timeline
    until = forecast.known_until(current)
    first = quarters.index_at(current)
    for index in range(first or 0, len(quarters) if first is not None else 0):
        key = quarters.starts[index]
        price = prices.get(key, estimated.get(key))
        if key >= until or price is None:
            break
        starts.append(quarters.local(index))
        price_list.append(price)
        is_estimate.append(key not in prices)
        # Known up to `until`: neither is None there
        load.append(
            (forecast.load[index] or 0.0)
            + forecast.planned[index]
            + forecast.reserve[index]
        )
        pv.append((forecast.pv[index] or 0.0) if forecast.pv is not None else 0.0)
        temperatures.append(
            forecast.temperature[index] if forecast.temperature is not None else None
        )
    if not starts:
        _LOGGER.info("No prices for the forecast, skipping the battery plan")
        return

    config = hass.data[DOMAIN]["config"]
    tariff = get_tariff(hass)
    limit = Limit(
        settings.number(config, "grid_import_limit"),
        int(settings.number(config, "grid_import_limit_start")),
        int(settings.number(config, "grid_import_limit_end")),
        fuse_power(settings.number(config, "main_fuse")),
    )
    planned = Timeline.of(starts)
    vehicle, car = _vehicle(hass, planned)
    capacity = settings.battery(config).capacity_kwh
    # Manual slots, planned around; also while the planner is off, as they still run
    forced, forced_soc = manual.forced(
        hass.data[DOMAIN].get("manual_slots", []), starts, _soc_limits(config)
    )
    # The mode the last plan has now, so the optimizer only switches when it pays
    last_plan: Plan | None = hass.data[DOMAIN].get("plan")
    month_start = dt_utils.start_of_local_day(now.date().replace(day=1))
    history = await _hourly_import(
        hass,
        settings.get(config, "grid_import_sensor"),
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
        forced=forced,
        forced_soc=[None if t is None else t / 100 * capacity for t in forced_soc],
        current_mode=last_plan.value_at(now, last_plan.modes) if last_plan else None,
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

    hass.data[DOMAIN]["plan"] = Plan(
        updated=now,
        finished=dt_utils.now(),
        timeline=planned,
        modes=tuple(plan.modes),
        soc_start=round(battery.soc_kwh / capacity * 100, 1),
        soc=tuple(round(kwh / capacity * 100, 1) for kwh in plan.soc),
        grid_import_kw=tuple(round(kwh / QUARTER_HOURS, 2) for kwh in plan.grid_import),
        grid_export_kw=tuple(round(kwh / QUARTER_HOURS, 2) for kwh in plan.grid_export),
        targets=tuple(None if t is None else round(t, 2) for t in plan.targets),
        peak_levels=tuple(None if t is None else round(t, 2) for t in plan.peak_levels),
        spot=tuple(price_list),
        estimated=tuple(is_estimate),
        heat_pump=tuple(heat_pump),
        marginal=tuple(marginal),
        temperature=tuple(
            None if t is None else round(t, 1) for t in heat.temperatures
        ),
        heat_pump_cop=tuple(None if c is None else round(c, 2) for c in heat.cops),
        heat_pump_kw=tuple(round(kwh / QUARTER_HOURS, 3) for kwh in heat.draws),
        ev=None
        if car is None
        else dataclasses.replace(
            car,
            power_kw=tuple(round(kwh / QUARTER_HOURS, 2) for kwh in plan.ev_kwh),
            planned_kwh=round(sum(plan.ev_kwh), 2),
            missing_kwh=round(plan.ev_missing_kwh, 2),
        ),
        energy_cost=round(plan.energy_cost, 2),
        wear_cost=round(plan.wear_cost, 2),
        power_cost=round(plan.power_cost, 2),
        end_value=round(plan.end_value, 2),
        limit_excess_kwh=round(plan.limit_excess_kwh, 2),
        fuse_excess_kwh=round(plan.fuse_excess_kwh, 2),
        peaks=tuple(
            {
                **_peak_label(tariff, key),
                "reached_kw": round(before, 2),
                "planned_kw": round(after, 2),
            }
            for key, (before, after) in plan.peaks.items()
        ),
        manual=tuple(forced),
        manual_soc=tuple(forced_soc),
    )
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
