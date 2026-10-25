"""HA glue for the heat pump (heat_pump.py).

Its economy now, the inputs for the battery plan, and what it should do now
(`sensor.energy_planner_heat_pump_action`).
"""

from collections.abc import Sequence
import dataclasses
import datetime as dt

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

from ..const import DOMAIN
from . import settings
from .battery_action import QUARTER, STALE_AFTER
from .heat_pump import (
    COP_CURVES,
    DISTRICT_HEATING_PRESETS,
    cop,
    district_heating_price,
    draw,
    evaluate,
)
from .utils import get_tariff


def _quarter(moment: dt.datetime) -> dt.datetime:
    return moment.replace(
        minute=moment.minute - moment.minute % 15, second=0, microsecond=0
    )


def _settings(hass: HomeAssistant) -> settings.HeatPumpSettings:
    return settings.heat_pump(hass.data[DOMAIN]["config"])


def curve(hass: HomeAssistant) -> tuple[tuple[float, float], ...]:
    """Return the heat pump's COP curve (heat_pump_model)."""
    return COP_CURVES.get(_settings(hass).model, ())


def _power_kw(hass: HomeAssistant, outdoor: float | None) -> float:
    """Return what the heat pump draws while it heats in kW.

    From the heat loss the forecast measured, the live indoor and the outdoor
    temperature, else the heat_pump_power setting. A setting of 0 turns planning off.
    """
    config = _settings(hass)
    setting = config.power_kw
    loss = (hass.data[DOMAIN].get("forecast") or {}).get("heat_pump_loss")
    indoor = settings.reading(hass, config.indoor_sensor)
    if setting <= 0 or not loss or indoor is None or outdoor is None:
        return setting
    return draw(loss, indoor, outdoor, curve(hass))


def _seasons(hass: HomeAssistant):
    return DISTRICT_HEATING_PRESETS.get(_settings(hass).district_heating, ())


def outdoor_temperature(hass: HomeAssistant) -> float | None:
    """Return the live outdoor temperature (°C)."""
    return settings.reading(hass, _settings(hass).outdoor_sensor)


def _planned_export(hass: HomeAssistant, moment: dt.datetime) -> float:
    """Return the battery plan's grid export (kW) in the quarter of `moment`."""
    plan = hass.data[DOMAIN].get("plan") or {}
    for start, export in zip(
        plan.get("starts", []), plan.get("grid_export", []), strict=False
    ):
        begin = dt.datetime.fromisoformat(start)
        if begin <= moment < begin + QUARTER:
            return export
    return 0.0


def heat_pump_economy(hass: HomeAssistant) -> dict:
    """Return the heat pump economy now (see heat_pump.evaluate).

    Without the inputs only the district heating price and temperature are given.
    """
    data = hass.data[DOMAIN]
    now = dt_utils.now()
    district = district_heating_price(_seasons(hass), now)
    cop_curve = curve(hass)
    spot = (data.get("prices") or {}).get(_quarter(now))
    outdoor = outdoor_temperature(hass)
    if district is None or outdoor is None or spot is None or not cop_curve:
        return {"district_heating_price": district, "outdoor_temperature": outdoor}
    tariff = get_tariff(hass)
    power = _power_kw(hass, outdoor)
    result = evaluate(
        spot,
        tariff.buy_fee(now),
        tariff.sell_fee(now),
        outdoor,
        cop_curve,
        district,
        _planned_export(hass, now) / power if power > 0 else 0.0,
    )
    result["spot"] = round(spot / 10, 1)
    result["power"] = round(power, 3)
    return result


@dataclasses.dataclass(frozen=True)
class PlanInputs:
    """What the battery plan needs to plan the heat pump, per quarter."""

    probe_kwh: float  # extra load per quarter to price, 0: not planned
    draws: list[float]  # kWh the heat pump uses per quarter while heating
    margin: float  # öre per kWh of heat
    temperatures: list[float | None]
    cops: list[float | None]
    district: list[float | None]
    heating: list[bool]


def plan_inputs(
    hass: HomeAssistant,
    starts: Sequence[dt.datetime],
    temperatures: Sequence[float | None],
) -> PlanInputs:
    """Return the heat pump inputs for the battery plan's quarters.

    `temperatures` is the forecast per quarter; missing ones use the live outdoor
    temperature.
    """
    live = outdoor_temperature(hass)
    temps = [live if t is None else t for t in temperatures]
    cop_curve = curve(hass)
    seasons = _seasons(hass)
    config = _settings(hass)
    limit = config.heating_limit_c
    hours = QUARTER.total_seconds() / 3600
    draws = [_power_kw(hass, t) * hours if cop_curve else 0.0 for t in temps]
    heating = [t is not None and t < limit for t in temps]
    drawing = [kwh for kwh in draws if kwh > 0]
    return PlanInputs(
        # More load is priced at the mean draw
        probe_kwh=sum(drawing) / len(drawing) if drawing else 0.0,
        draws=draws,
        margin=config.margin_ore_per_kwh,
        temperatures=temps,
        cops=[None if t is None else cop(cop_curve, t) for t in temps],
        district=[district_heating_price(seasons, s) for s in starts],
        heating=heating,
    )


def heat_pump_action(hass: HomeAssistant) -> dict:
    """Return whether the heat pump should heat now.

    From the battery plan (source `plan`) while it is fresh and covers now, otherwise
    from the economy now and the heating limit (source `fallback`); without prices or
    an outdoor temperature no decision (state None, source `none`).
    """
    now = dt_utils.now()
    plan = hass.data[DOMAIN].get("plan") or {}
    flags = plan.get("heat_pump")
    if flags and now - dt.datetime.fromisoformat(plan["updated"]) <= STALE_AFTER:
        starts = [dt.datetime.fromisoformat(s) for s in plan["starts"]]
        index = next((i for i, s in enumerate(starts) if s <= now < s + QUARTER), None)
        if index is not None:
            last = index
            while last + 1 < len(flags) and flags[last + 1] == flags[index]:
                last += 1
            # Without a heat pump (heat_pump_power 0) the plan prices no extra load
            marginal = plan.get("marginal") or []
            return {
                "state": "on" if flags[index] else "off",
                "source": "plan",
                "until": (starts[last] + QUARTER).isoformat(),
                "marginal": marginal[index] if index < len(marginal) else None,
                "cop": plan["heat_pump_cop"][index],
            }
    economy = heat_pump_economy(hass)
    outdoor = economy.get("outdoor_temperature")
    saving = economy.get("saving")
    if "cop" not in economy or outdoor is None:
        # No prices or temperature yet (e.g. just after a restart): no decision, so
        # the automation leaves the heat pump as it is
        return {
            "state": None,
            "source": "none",
            "until": None,
            "marginal": None,
            "cop": None,
        }
    config = _settings(hass)
    heat = (
        saving is not None
        and saving >= config.margin_ore_per_kwh
        and outdoor < config.heating_limit_c
    )
    return {
        "state": "on" if heat else "off",
        "source": "fallback",
        "until": None,
        "marginal": economy.get("electricity_price"),
        "cop": economy.get("cop"),
    }
