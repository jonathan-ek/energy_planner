"""The battery plan from its inputs, without Home Assistant dependencies.

battery_plan.py gathers a `PlanInputs` (the forecast, the prices, the battery's,
car's and heat pump's settings and readings, this month's import, the manual slots
and the last plan) and runs `build_plan` in the executor; the result is the
`plan.Plan` it stores.

Times are UTC instants on the forecast's `Timeline` (timeline.py), so the hour
repeated when DST ends has its own quarters and prices. The optimizer works in kWh
per quarter and SEK; the plan publishes kW, SOC % and öre.
"""

from collections.abc import Mapping, Sequence
import dataclasses
import datetime as dt
import functools
from typing import Any

from . import ev, heat_pump, manual, settings
from .battery_optimizer import (
    QUARTER_HOURS,
    Battery,
    Limit,
    Vehicle,
    charger_powers,
    optimize,
)
from .forecast import Forecast
from .plan import EvPlan, Plan
from .tariff import Tariff
from .timeline import Timeline, quarter_start


@dataclasses.dataclass(frozen=True)
class CarReadings:
    """The plugged-in car: its settings and what its sensors read now."""

    settings: settings.CarSettings
    soc: float | None  # %
    charge_limit: float | None  # %
    delivered_kwh: float | None  # this session
    # The SOC (%) entered for this session, and the session's energy then
    entered_soc: float | None = None
    entered_at_kwh: float | None = None


@dataclasses.dataclass(frozen=True)
class HeatPumpReadings:
    """The heat pump's settings and the live temperatures."""

    settings: settings.HeatPumpSettings
    outdoor: float | None  # °C
    indoor: float | None


@dataclasses.dataclass(frozen=True)
class PlanInputs:
    """What a battery plan is made from, gathered by battery_plan.py."""

    now: dt.datetime
    forecast: Forecast
    # Spot prices per quarter start (UTC), SEK/MWh excl. VAT (prices.SpotPrices)
    published: Mapping[dt.datetime, float]
    estimated: Mapping[dt.datetime, float]
    battery: settings.BatterySettings
    soc: float  # % now
    voltage: float  # V now
    tariff: Tariff
    limit: Limit
    # This month's grid import per hour start (UTC), kWh
    import_history: Mapping[dt.datetime, float]
    manual_slots: Sequence[manual.ManualSlot]
    last_plan: Plan | None
    car: CarReadings | None  # None: not set up or not plugged in
    heat_pump: HeatPumpReadings


@dataclasses.dataclass(frozen=True)
class HeatPumpQuarters:
    """What the battery plan needs to plan the heat pump, per quarter."""

    probe_kwh: float  # extra load per quarter to price, 0: not planned
    draws: list[float]  # kWh the heat pump uses per quarter while heating
    margin: float  # öre per kWh of heat
    temperatures: list[float | None]
    cops: list[float | None]
    district: list[float | None]
    heating: list[bool]


def soc_limits(battery: settings.BatterySettings) -> manual.SocLimits:
    """Return the battery's SOC settings that manual slot targets are limited to."""
    return manual.SocLimits(battery.shutdown_soc, battery.max_soc, battery.reserve_soc)


def battery(config: settings.BatterySettings, soc: float, voltage: float) -> Battery:
    """Return the battery's limits in kWh and kW from its settings, SOC and voltage."""
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


def heat_pump_quarters(
    readings: HeatPumpReadings,
    loss: float | None,
    starts: Sequence[dt.datetime],
    temperatures: Sequence[float | None],
) -> HeatPumpQuarters:
    """Return the heat pump inputs for the battery plan's quarters.

    `temperatures` is the forecast per quarter; missing ones use the live outdoor
    temperature. The draw is from the measured heat `loss` (kW/K) and the live
    indoor temperature, else the heat_pump_power setting (heat_pump.power_kw).
    """
    config = readings.settings
    temps = [readings.outdoor if t is None else t for t in temperatures]
    cop_curve = heat_pump.cop_curve(config.model)
    seasons = heat_pump.DISTRICT_HEATING_PRESETS.get(config.district_heating, ())
    draws = [
        heat_pump.power_kw(config.power_kw, loss, readings.indoor, t, cop_curve)
        * QUARTER_HOURS
        if cop_curve
        else 0.0
        for t in temps
    ]
    drawing = [kwh for kwh in draws if kwh > 0]
    return HeatPumpQuarters(
        # More load is priced at the mean draw
        probe_kwh=sum(drawing) / len(drawing) if drawing else 0.0,
        draws=draws,
        margin=config.margin_ore_per_kwh,
        temperatures=temps,
        cops=[None if t is None else heat_pump.cop(cop_curve, t) for t in temps],
        district=[heat_pump.district_heating_price(seasons, s) for s in starts],
        heating=[t is not None and t < config.heating_limit_c for t in temps],
    )


def vehicle(
    car: CarReadings | None, history_need_kwh: float | None, quarters: Timeline
) -> tuple[Vehicle, EvPlan] | tuple[None, None]:
    """Return the car to plan with the battery, and its need as the plan publishes it.

    The `EvPlan`'s charging is filled in from the optimizer's result.

    Only while it is plugged in. The need is from the car's SOC to its charge limit
    (the limit sensor, else the `ev_charge_limit` setting) when the car reports its
    SOC. Otherwise from the SOC entered for this session, minus what the session has
    delivered since. Otherwise it is the largest recent charging night
    (`history_need_kwh`) minus what this session has delivered (see planner/ev.py),
    or for a trip (the departure comes before the next working morning) a full
    battery, as the car stops at its own charge limit. Without either, or once the
    session has taken more than the need, the car is not planned and the action
    falls back to charging, so a car that is not full is never paused.
    """
    if car is None or not len(quarters):
        return None, None
    config = car.settings
    capacity = config.battery_kwh
    ready = config.ready_time
    departure = config.departure
    target = ev.ready_time(quarters.local(0), ready, departure)
    limit = car.charge_limit if car.charge_limit is not None else config.charge_limit
    if car.soc is not None:
        need = ev.need_from_soc(car.soc, limit, capacity)
        source = "soc"
    elif car.entered_soc is not None:
        delivered = (car.delivered_kwh or 0.0) - (car.entered_at_kwh or 0.0)
        need = ev.need_from_soc(car.entered_soc, limit, capacity) - max(delivered, 0)
        source = "entered"
        if need <= 0:
            return None, None
    else:
        if target == departure:
            need = ev.need_from_soc(0, 100, capacity)
            source = "trip"
        else:
            if history_need_kwh is None:
                return None, None
            need = history_need_kwh
            source = "history"
        need -= car.delivered_kwh or 0.0
        if need <= 0:
            return None, None
    max_kw = config.charger_max_kw
    min_kw = config.charger_min_kw
    return Vehicle(
        powers_kw=charger_powers(min_kw, max_kw) if min_kw > 0 else (max_kw,),
        need_kwh=need,
        plugged=[True] * len(quarters),
        deadline=ev.deadline_index(quarters, ready, departure),
    ), EvPlan(
        need_kwh=round(need, 2),
        need_source=source,
        ready=target,
        power_kw=(),
        planned_kwh=0.0,
        missing_kwh=0.0,
    )


def _peak_label(tariff: Tariff, key: str) -> dict[str, Any]:
    index, month = key.split(":")
    charge = tariff.power_charges[int(index)]
    return {
        "month": month,
        "hours": [charge.period.start_hour, charge.period.end_hour],
        "kr_per_kw": charge.kr_per_kw,
    }


def build_plan(inputs: PlanInputs) -> Plan | None:
    """Plan the battery, the car and the heat pump from the current quarter on.

    None when there is nothing to plan: no price or forecast for the current
    quarter, or no room between the shutdown and the max SOC.

    1. Horizon: from the current quarter while the forecast knows load and PV
       (`Forecast.known_until`) and a price exists, published or estimated. Load =
       profile + planned + reserve.
    2. The manual slots are forced into their quarters, the car (while plugged in)
       is planned with the battery, and the mode the last plan has now is the
       optimizer's previous mode, so it only switches when that pays.
    3. A first run prices more load per quarter (its `marginal`); the heat pump
       is scheduled where its heat beats district heating at that price
       (heat_pump.schedule). If it runs, a second run plans the battery with its
       draw added and exempt from the import limit.
    4. The result in the plan's units: SOC %, kW, öre, rounded. `finished` is
       `now`; battery_plan.py sets when the plan became available.
    """
    forecast = inputs.forecast
    published, estimated = inputs.published, inputs.estimated
    quarters = forecast.timeline
    current = quarter_start(inputs.now)
    until = forecast.known_until(current)
    first = quarters.index_at(current)
    starts, prices, load, pv, temperatures, is_estimate = [], [], [], [], [], []
    for index in range(first or 0, len(quarters) if first is not None else 0):
        key = quarters.starts[index]
        price = published.get(key, estimated.get(key))
        if key >= until or price is None:
            break
        starts.append(quarters.local(index))
        prices.append(price)
        is_estimate.append(key not in published)
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
    store = battery(inputs.battery, inputs.soc, inputs.voltage)
    if not starts or store.max_kwh <= store.min_kwh:
        return None

    planned = Timeline.of(starts)
    car, ev_plan = vehicle(inputs.car, forecast.ev_need_kwh, planned)
    capacity = inputs.battery.capacity_kwh
    # Manual slots, planned around; also while the planner is off, as they still run
    forced, forced_soc = manual.forced(
        inputs.manual_slots, starts, soc_limits(inputs.battery)
    )
    last = inputs.last_plan
    run = functools.partial(
        optimize,
        starts,
        prices,
        battery=store,
        tariff=inputs.tariff,
        limit=inputs.limit,
        history=dict(inputs.import_history),
        forced=forced,
        forced_soc=[None if t is None else t / 100 * capacity for t in forced_soc],
        current_mode=last.value_at(inputs.now, last.modes) if last else None,
        vehicle=car,
    )
    heat = heat_pump_quarters(
        inputs.heat_pump, forecast.heat_pump_loss, starts, temperatures
    )
    result = run(load=load, pv=pv, probe_kwh=heat.probe_kwh)
    marginal = [round(m * 100, 1) for m in result.marginal]
    heat_pump_on = (
        heat_pump.schedule(
            marginal, heat.cops, heat.district, heat.heating, heat.margin
        )
        if heat.probe_kwh > 0
        else [False] * len(starts)
    )
    if any(heat_pump_on):
        extra = [
            kwh if on else 0.0 for kwh, on in zip(heat.draws, heat_pump_on, strict=True)
        ]
        result = run(
            load=[a + b for a, b in zip(load, extra, strict=True)], pv=pv, exempt=extra
        )

    return Plan(
        updated=inputs.now,
        finished=inputs.now,
        timeline=planned,
        modes=tuple(result.modes),
        soc_start=round(inputs.soc, 1),
        soc=tuple(round(kwh / capacity * 100, 1) for kwh in result.soc),
        grid_import_kw=tuple(
            round(kwh / QUARTER_HOURS, 2) for kwh in result.grid_import
        ),
        grid_export_kw=tuple(
            round(kwh / QUARTER_HOURS, 2) for kwh in result.grid_export
        ),
        targets=tuple(None if t is None else round(t, 2) for t in result.targets),
        peak_levels=tuple(
            None if t is None else round(t, 2) for t in result.peak_levels
        ),
        spot=tuple(prices),
        estimated=tuple(is_estimate),
        heat_pump=tuple(heat_pump_on),
        marginal=tuple(marginal),
        temperature=tuple(
            None if t is None else round(t, 1) for t in heat.temperatures
        ),
        heat_pump_cop=tuple(None if c is None else round(c, 2) for c in heat.cops),
        heat_pump_kw=tuple(round(kwh / QUARTER_HOURS, 3) for kwh in heat.draws),
        ev=None
        if ev_plan is None
        else dataclasses.replace(
            ev_plan,
            power_kw=tuple(round(kwh / QUARTER_HOURS, 2) for kwh in result.ev_kwh),
            planned_kwh=round(sum(result.ev_kwh), 2),
            missing_kwh=round(result.ev_missing_kwh, 2),
        ),
        energy_cost=round(result.energy_cost, 2),
        wear_cost=round(result.wear_cost, 2),
        power_cost=round(result.power_cost, 2),
        end_value=round(result.end_value, 2),
        limit_excess_kwh=round(result.limit_excess_kwh, 2),
        fuse_excess_kwh=round(result.fuse_excess_kwh, 2),
        peaks=tuple(
            {
                **_peak_label(inputs.tariff, key),
                "reached_kw": round(before, 2),
                "planned_kw": round(after, 2),
            }
            for key, (before, after) in result.peaks.items()
        ),
        manual=tuple(forced),
        manual_soc=tuple(forced_soc),
    )
