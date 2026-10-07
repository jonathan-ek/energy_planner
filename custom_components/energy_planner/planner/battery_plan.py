"""Battery plan (dry run): runs the optimizer on the forecast, prices and tariff.

The plan is only published (`hass.data[DOMAIN]["plan"]`, shown by
`sensor.energy_planner_battery_plan`); nothing is written to the schedule yet.
"""

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
from .battery_optimizer import QUARTER_HOURS, Battery, Limit, optimize
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
    forecast = hass.data[DOMAIN].get("forecast")
    battery = _battery(hass)
    if not forecast or battery is None or battery.max_kwh <= battery.min_kwh:
        _LOGGER.info("No forecast or battery state, skipping the battery plan")
        return
    prices = await _prices(hass)
    now = dt_utils.now()
    current = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)

    starts, price_list, load, pv = [], [], [], []
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
    plan = await hass.async_add_executor_job(
        functools.partial(
            optimize, starts, price_list, load, pv, battery, tariff, limit, history
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
        "power charge +%.2f SEK",
        starts[-1].isoformat(),
        plan.modes[0],
        plan.energy_cost,
        plan.wear_cost,
        plan.power_cost,
    )
    for sensor in hass.data[DOMAIN].get(PLAN_SENSORS, []):
        sensor.async_write_ha_state()
