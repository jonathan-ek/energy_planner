"""Battery plan: gathers its inputs from HA and runs `planning.build_plan`.

The plan (a `plan.Plan`) is published in `hass.data[DOMAIN]["plan"]` (shown by
`sensor.energy_planner_battery_plan`). It controls nothing by itself:
`sensor.energy_planner_battery_action` follows it while
`switch.energy_planner_enabled` is on, and manual slots override it (see
actions.py); an inverter script follows that sensor.
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

from ..const import DOMAIN, NUMBER_ENTITIES, PLAN_SENSORS
from . import ev, manual, planning, settings
from .actions import Actions, Situation, resolve
from .battery_optimizer import Limit
from .forecast import Forecast
from .prices import async_update_prices, spot_at
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


def car_status(hass: HomeAssistant) -> str | None:
    """Return the charger's state, None without a charger status sensor or state."""
    car = settings.car(hass.data[DOMAIN]["config"])
    state = hass.states.get(car.status_sensor) if car is not None else None
    return state.state if state is not None else None


def ev_plugged(hass: HomeAssistant) -> bool:
    """Return whether the car is plugged in (False without a charger status)."""
    return ev.is_plugged(car_status(hass))


def _car(hass: HomeAssistant) -> planning.CarReadings | None:
    """Return the plugged-in car's settings and readings, None if not planned."""
    config = hass.data[DOMAIN]["config"]
    car = settings.car(config)
    if car is None or not ev_plugged(hass):
        return None
    return planning.CarReadings(
        settings=car,
        soc=settings.reading(hass, car.soc_sensor),
        charge_limit=settings.reading(hass, car.charge_limit_sensor),
        delivered_kwh=settings.reading(hass, car.session_energy_sensor),
        entered_soc=settings.get(config, "ev_soc"),
        entered_at_kwh=settings.get(config, "ev_soc_session_kwh"),
    )


async def _store_ev_settings(hass: HomeAssistant, values: dict) -> None:
    """Store car settings and show them on their entities."""
    hass.data[DOMAIN]["config"].update(values)
    await hass.data[DOMAIN]["save"]()
    for entity in hass.data[DOMAIN].get(NUMBER_ENTITIES, []):
        if entity.id in values:
            entity.update()


async def async_set_ev_soc(
    hass: HomeAssistant, soc: float, charge_limit: float | None = None
) -> None:
    """Enter the car's SOC (%) for this session, and optionally its charge limit.

    Stored with the session's energy now, so what is delivered after it is taken
    off the need (planning.vehicle); entered before plugging in, the session starts
    at 0. The plan follows from the entities' state changes, and is requested here
    when the same SOC is entered again. Raises ValueError outside 0-100 %.
    """
    for value in (soc, charge_limit):
        if value is not None and not 0 <= value <= 100:
            raise ValueError(f"{value} is not a SOC (0-100 %)")
    config = hass.data[DOMAIN]["config"]
    car = settings.car(config)
    plugged = car is not None and ev_plugged(hass)
    session = settings.reading(hass, car.session_energy_sensor) if plugged else None
    unchanged = settings.get(config, "ev_soc") == soc
    values = {"ev_soc": float(soc), "ev_soc_session_kwh": session or 0.0}
    if charge_limit is not None:
        values["ev_charge_limit"] = float(charge_limit)
    await _store_ev_settings(hass, values)
    if unchanged:
        await async_request_plan(hass, f"car SOC {soc} % entered")


async def async_car_unplugged(hass: HomeAssistant) -> None:
    """Clear the SOC entered for the session that ended."""
    if settings.get(hass.data[DOMAIN]["config"], "ev_soc") is not None:
        await _store_ev_settings(hass, {"ev_soc": None, "ev_soc_session_kwh": None})


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


def situation(hass: HomeAssistant, now: dt.datetime) -> Situation:
    """Return the situation at `now` from the stored plan, settings and readings."""
    data = hass.data[DOMAIN]
    config = data["config"]
    battery = settings.battery(config)
    heat_pump = settings.heat_pump(config)
    forecast: Forecast | None = data.get("forecast")
    return Situation(
        now=now,
        plan=data.get("plan"),
        manual_slots=data.get("manual_slots", []),
        enabled=settings.get(config, "enabled"),
        battery=battery,
        heat_pump=heat_pump,
        tariff=get_tariff(hass),
        spot=spot_at(hass, now),
        heat_pump_loss=forecast.heat_pump_loss if forecast is not None else None,
        soc=settings.reading(hass, battery.soc_sensor),
        voltage=settings.reading(hass, battery.voltage_sensor),
        car_status=car_status(hass),
        outdoor=settings.reading(hass, heat_pump.outdoor_sensor),
        indoor=settings.reading(hass, heat_pump.indoor_sensor),
    )


def actions_now(hass: HomeAssistant) -> Actions | None:
    """Return the actions last resolved, None before the first or after a failure."""
    return hass.data[DOMAIN].get("actions")


def resolve_actions(hass: HomeAssistant, now: dt.datetime | None = None) -> None:
    """Resolve the actions at `now` (default: now) into `hass.data[DOMAIN]["actions"]`.

    An action that fails is None until it works again, its sensor unavailable, so
    its device does what it does without the planner rather than follow an old
    action; the situation failing makes them all None. Each failing part is logged
    once while it keeps failing (`actions_failing`).
    """
    data = hass.data[DOMAIN]
    moment = dt_utils.as_local(now) if now is not None else dt_utils.now()
    try:
        actions = resolve(situation(hass, moment))
        errors = dict(actions.errors)
    except Exception as err:
        actions, errors = None, {"situation": err}
    failing: set[str] = data.setdefault("actions_failing", set())
    for name, err in errors.items():
        if name not in failing:
            _LOGGER.error("Failed to resolve %s", name, exc_info=err)
    failing.clear()
    failing.update(errors)
    data["actions"] = actions


def write_plan_sensors(hass: HomeAssistant, now: dt.datetime | None = None) -> None:
    """Resolve the actions at `now` once, and write the plan and action sensors.

    Never raises: a sensor that fails to write is logged and the others are written.
    """
    resolve_actions(hass, now)
    for sensor in hass.data[DOMAIN].get(PLAN_SENSORS, []):
        # Not a disabled sensor (never added)
        if sensor.hass is None:
            continue
        try:
            sensor.async_write_ha_state()
        except Exception:
            _LOGGER.exception("Failed to write %s", sensor.entity_id)


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
    # Also when the plan can not be made
    await _prune_manual_slots(hass, dt_utils.now())
    data = hass.data[DOMAIN]
    config = data["config"]
    forecast: Forecast | None = data.get("forecast")
    battery = settings.battery(config)
    soc = settings.reading(hass, battery.soc_sensor)
    voltage = settings.reading(hass, battery.voltage_sensor)
    plannable = forecast is not None and soc is not None and bool(voltage)
    # The published prices also without a plan: the heat pump sensors read them
    spot = await async_update_prices(hass, dt_utils.now(), estimate=plannable)
    if forecast is None or soc is None or not voltage:
        _LOGGER.info("No forecast or battery state, skipping the battery plan")
        return
    # After the prices, whose first estimate of the day can take a while
    now = dt_utils.now()
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
