"""The actions now: what the battery, the car and the heat pump should do.

Pure functions, no Home Assistant imports. `resolve` takes a `Situation` (the plan,
the manual slots, the settings and the readings at one moment) and returns every
action published together (`Actions`), so they agree on the time and on the plan.
`battery_plan.situation` gathers the situation from HA.

- battery: the manual slot covering now, else the plan (battery_action.py)
- car: the plan's car power, `charge` without a fresh plan (ev.py)
- heat pump: the plan, else the economy now (heat_pump.py)
- heat pump economy: the saving of heating with the heat pump now, worked out once
  for its sensors and the heat pump's fallback
- mode: the plan's mode now, for the plan sensor

A stale plan (plan.py) is followed by nothing: every action falls back, and the mode
is None. Two uses of an old plan are on purpose: the heat pump economy takes its
export (`heat_pump_economy`), and the plan sensor's attributes still show it.

A missing reading is None here; each device's rule says what it means. An action
that fails to resolve is None (its sensor unavailable) and does not take the others
with it; the caller logs `Actions.errors`.
"""

from collections.abc import Callable, Mapping, Sequence
import dataclasses
import datetime as dt
from typing import Any

from . import ev, heat_pump, manual
from .battery_action import resolve as battery_action
from .plan import Plan, follow
from .settings import BatterySettings, HeatPumpSettings
from .tariff import Tariff


@dataclasses.dataclass(frozen=True)
class Situation:
    """Everything the actions are resolved from at one moment."""

    now: dt.datetime
    plan: Plan | None
    manual_slots: Sequence[manual.ManualSlot]
    enabled: bool  # switch.energy_planner_enabled
    battery: BatterySettings
    heat_pump: HeatPumpSettings
    tariff: Tariff
    spot: float | None  # SEK/MWh now, None before the first fetch
    heat_pump_loss: float | None  # kW of heat per °C, measured by the forecast
    # Readings
    soc: float | None  # %
    voltage: float | None  # V
    car_status: str | None  # the charger's state, None without one
    outdoor: float | None  # °C
    indoor: float | None  # °C


@dataclasses.dataclass(frozen=True)
class Actions:
    """Every action at one moment, as the sensors publish them.

    An action is None when it failed to resolve (its error in `errors`).
    """

    plan: Plan | None  # resolved from, also when stale (the plan sensor shows it)
    battery: dict[str, Any] | None
    car: dict[str, Any] | None
    heat_pump: dict[str, Any] | None
    heat_pump_economy: dict[str, Any] | None
    mode: str | None  # None: no fresh plan covering now
    errors: Mapping[str, Exception] = dataclasses.field(default_factory=dict)


def heat_pump_economy(situation: Situation) -> dict[str, Any]:
    """Return the heat pump economy now (see heat_pump.evaluate).

    Without the inputs only the district heating price and temperature are given.
    """
    now = situation.now
    config = situation.heat_pump
    seasons = heat_pump.DISTRICT_HEATING_PRESETS.get(config.district_heating, ())
    district = heat_pump.district_heating_price(seasons, now)
    cop_curve = heat_pump.cop_curve(config.model)
    spot = situation.spot
    outdoor = situation.outdoor
    if district is None or outdoor is None or spot is None or not cop_curve:
        return {"district_heating_price": district, "outdoor_temperature": outdoor}
    # From the measured heat loss, else the heat_pump_power setting
    power = heat_pump.power_kw(
        config.power_kw, situation.heat_pump_loss, situation.indoor, outdoor, cop_curve
    )
    # The plan's export this quarter, even an old plan's: still the best guess at
    # the PV excess the heat pump can use
    plan = situation.plan
    export = (plan.value_at(now, plan.grid_export_kw) if plan else None) or 0.0
    tariff = situation.tariff
    result = heat_pump.evaluate(
        spot,
        tariff.buy_fee(now),
        tariff.sell_fee(now),
        outdoor,
        cop_curve,
        district,
        export / power if power > 0 else 0.0,
    )
    result["spot"] = round(spot / 10, 1)
    result["power"] = round(power, 3)
    return result


def resolve(situation: Situation) -> Actions:
    """Return every action at the situation's moment.

    Each is worked out on its own: one that fails is None, with its error in
    `errors`, and the others are kept. The heat pump decides without an economy
    that failed (no decision unless it follows the plan).
    """
    now = situation.now
    plan = situation.plan
    config = situation.heat_pump
    errors: dict[str, Exception] = {}

    def part(name: str, work: Callable[[], Any]) -> Any:
        try:
            return work()
        except Exception as err:
            # Kept for the caller to log; the other actions still resolve
            errors[name] = err
            return None

    def mode() -> str | None:
        # The same rule as each device's: no mode from a stale plan
        run, _ = follow(plan, now, lambda p: p.modes)
        return run.value if run is not None else None

    economy = part("heat_pump_economy", lambda: heat_pump_economy(situation))
    return Actions(
        plan=plan,
        battery=part(
            "battery",
            lambda: battery_action(
                manual.running(situation.manual_slots, now),
                plan,
                now,
                situation.battery,
                situation.soc,
                situation.voltage,
                enabled=situation.enabled,
            ),
        ),
        car=part(
            "car", lambda: ev.ev_action(plan, now, ev.is_plugged(situation.car_status))
        ),
        heat_pump=part(
            "heat_pump",
            lambda: heat_pump.action(
                plan,
                now,
                economy or {},
                config.margin_ore_per_kwh,
                config.heating_limit_c,
            ),
        ),
        heat_pump_economy=economy,
        mode=part("mode", mode),
        errors=errors,
    )
