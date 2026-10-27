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

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

from ..const import DOMAIN, PLAN_SENSORS
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
from .plan import EvPlan, Plan
from .prices import async_update_prices
from .timeline import Timeline, quarter_start
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
    now = dt_utils.now()
    # Also when the plan can not be made: the heat pump sensors read the prices
    await _prune_manual_slots(hass, now)
    spot = await async_update_prices(hass, now)
    forecast: Forecast | None = hass.data[DOMAIN].get("forecast")
    battery = _battery(hass)
    if not forecast or battery is None or battery.max_kwh <= battery.min_kwh:
        _LOGGER.info("No forecast or battery state, skipping the battery plan")
        return
    current = quarter_start(now)
    # The day after the published prices is estimated, so the plan can see whether
    # energy is worth keeping past the coming night
    prices, estimated = spot.published, spot.estimated

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
