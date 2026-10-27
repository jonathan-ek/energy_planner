"""Battery plan: gathers its inputs from HA and runs `planning.build_plan`.

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

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

from ..const import DOMAIN, PLAN_SENSORS
from . import ev, manual, planning, settings
from .battery_action import BatteryState, resolve
from .battery_optimizer import Limit
from .forecast import Forecast
from .prices import async_update_prices
from .tariff import fuse_power
from .utils import get_tariff

_LOGGER = logging.getLogger(__name__)


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


def ev_plugged(hass: HomeAssistant) -> bool:
    """Return whether the car is plugged in (False without a charger status)."""
    car = settings.car(hass.data[DOMAIN]["config"])
    state = hass.states.get(car.status_sensor) if car is not None else None
    return ev.is_plugged(state.state if state is not None else None)


def _car(hass: HomeAssistant) -> planning.CarReadings | None:
    """Return the plugged-in car's settings and readings, None if not planned."""
    car = settings.car(hass.data[DOMAIN]["config"])
    if car is None or not ev_plugged(hass):
        return None
    return planning.CarReadings(
        settings=car,
        soc=settings.reading(hass, car.soc_sensor),
        charge_limit=settings.reading(hass, car.charge_limit_sensor),
        delivered_kwh=settings.reading(hass, car.session_energy_sensor),
    )


def _heat_pump(hass: HomeAssistant) -> planning.HeatPumpReadings:
    """Return the heat pump's settings and the live temperatures."""
    config = settings.heat_pump(hass.data[DOMAIN]["config"])
    return planning.HeatPumpReadings(
        settings=config,
        outdoor=settings.reading(hass, config.outdoor_sensor),
        indoor=settings.reading(hass, config.indoor_sensor),
    )


def manual_slot_attributes(hass: HomeAssistant) -> list[dict]:
    """Return the manual slots as the plan sensor publishes them.

    Local times like the plan's, and the target SOC they run to (None: no target).
    """
    limits = planning.soc_limits(settings.battery(hass.data[DOMAIN]["config"]))
    return [
        {
            "start": dt_utils.as_local(slot.start).isoformat(),
            "end": dt_utils.as_local(slot.end).isoformat(),
            "state": slot.state,
            "soc": manual.target(slot, limits),
        }
        for slot in hass.data[DOMAIN].get("manual_slots", [])
    ]


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


async def async_update_plan(hass: HomeAssistant) -> None:
    """Optimize the battery from the current quarter to the last known price."""
    now = dt_utils.now()
    # Also when the plan can not be made: the heat pump sensors read the prices
    await _prune_manual_slots(hass, now)
    spot = await async_update_prices(hass, now)
    data = hass.data[DOMAIN]
    config = data["config"]
    forecast: Forecast | None = data.get("forecast")
    battery = settings.battery(config)
    soc = settings.reading(hass, battery.soc_sensor)
    voltage = settings.reading(hass, battery.voltage_sensor)
    if not forecast or soc is None or not voltage:
        _LOGGER.info("No forecast or battery state, skipping the battery plan")
        return
    month_start = dt_utils.start_of_local_day(now.date().replace(day=1))
    inputs = planning.PlanInputs(
        now=now,
        forecast=forecast,
        published=spot.published,
        estimated=spot.estimated,
        battery=battery,
        soc=soc,
        voltage=voltage,
        tariff=get_tariff(hass),
        limit=Limit(
            settings.number(config, "grid_import_limit"),
            int(settings.number(config, "grid_import_limit_start")),
            int(settings.number(config, "grid_import_limit_end")),
            fuse_power(settings.number(config, "main_fuse")),
        ),
        import_history=await _hourly_import(
            hass, settings.get(config, "grid_import_sensor"), month_start, now
        ),
        manual_slots=tuple(data.get("manual_slots", [])),
        last_plan=data.get("plan"),
        car=_car(hass),
        heat_pump=_heat_pump(hass),
    )
    plan = await hass.async_add_executor_job(
        functools.partial(planning.build_plan, inputs)
    )
    if plan is None:
        _LOGGER.info(
            "No prices for the forecast or no room in the battery, "
            "skipping the battery plan"
        )
        return
    plan = dataclasses.replace(plan, finished=dt_utils.now())
    data["plan"] = plan
    _LOGGER.info(
        "Battery plan until %s: %s now, energy %.2f SEK, wear %.2f SEK, "
        "power charge +%.2f SEK, heat pump on %d of %d quarters, %d estimated prices",
        plan.timeline.local(len(plan.timeline) - 1).isoformat(),
        plan.modes[0],
        plan.energy_cost,
        plan.wear_cost,
        plan.power_cost,
        sum(plan.heat_pump),
        len(plan.heat_pump),
        sum(plan.estimated),
    )
    write_plan_sensors(hass)
