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

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

from ..const import (
    DEFAULT_BATTERY_SOC_SENSOR,
    DEFAULT_BATTERY_VOLTAGE_SENSOR,
    DEFAULT_GRID_IMPORT_SENSOR,
    DOMAIN,
    PLAN_SENSORS,
)
from .battery_action import QUARTER, SLOT_MODES, BatteryState, Slot, resolve
from .battery_optimizer import QUARTER_HOURS, Battery, Limit, optimize
from .heat_pump import schedule
from .heating import plan_inputs
from .nordpool_utils import fetch_nordpool_data
from .utils import get_tariff

_LOGGER = logging.getLogger(__name__)


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
    """Spot prices (SEK/MWh excl. VAT) per quarter start for today and tomorrow."""
    entity_id = hass.data[DOMAIN]["config"].get("nordpool_entity_id")
    state = hass.states.get(entity_id) if entity_id else None
    if entity_id is None or state is None:
        return {}
    currency = entity_id.split("_")[3].upper()
    area = entity_id.split("_")[2].upper()
    tomorrow_valid = state.attributes.get("tomorrow_valid")
    _, today, tomorrow = await fetch_nordpool_data(
        hass, currency, area, bool(tomorrow_valid)
    )
    prices = {}
    for value in [*(today or []), *(tomorrow or [])]:
        start = value["start"]
        if isinstance(start, str):
            start = dt.datetime.fromisoformat(start)
        prices[dt_utils.as_local(start)] = float(value["value"])
    return prices


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
    )


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
    # Also read by the heat pump sensors (heating.py)
    hass.data[DOMAIN]["prices"] = prices
    forecast = hass.data[DOMAIN].get("forecast")
    battery = _battery(hass)
    if not forecast or battery is None or battery.max_kwh <= battery.min_kwh:
        _LOGGER.info("No forecast or battery state, skipping the battery plan")
        return
    now = dt_utils.now()
    current = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)

    starts, price_list, load, pv, temperatures = [], [], [], [], []
    for index, start_text in enumerate(forecast["starts"]):
        start = dt_utils.as_local(dt.datetime.fromisoformat(start_text))
        if start < current:
            continue
        base = forecast["load"][index]
        if start not in prices or base is None:
            break
        starts.append(start)
        price_list.append(prices[start])
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
    )
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
        # Heat pump: on per quarter, what more load costs (öre/kWh), its COP and draw
        # (kW while heating)
        "heat_pump": heat_pump,
        "marginal": marginal,
        "temperature": [None if t is None else round(t, 1) for t in heat.temperatures],
        "heat_pump_cop": [None if c is None else round(c, 2) for c in heat.cops],
        "heat_pump_power": [round(kwh / QUARTER_HOURS, 3) for kwh in heat.draws],
        "mode": plan.modes[0],
        "target_kw": None if plan.targets[0] is None else round(plan.targets[0], 2),
        "energy_cost": round(plan.energy_cost, 2),
        "wear_cost": round(plan.wear_cost, 2),
        "power_cost": round(plan.power_cost, 2),
        "end_value": round(plan.end_value, 2),
        "limit_excess_kwh": round(plan.limit_excess_kwh, 2),
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
        "power charge +%.2f SEK, heat pump on %d of %d quarters",
        starts[-1].isoformat(),
        plan.modes[0],
        plan.energy_cost,
        plan.wear_cost,
        plan.power_cost,
        sum(heat_pump),
        len(heat_pump),
    )
    write_plan_sensors(hass)
