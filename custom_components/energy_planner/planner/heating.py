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
from . import heat_pump, settings
from .plan import QUARTER, Plan, quarter_start
from .utils import get_tariff


def _settings(hass: HomeAssistant) -> settings.HeatPumpSettings:
    return settings.heat_pump(hass.data[DOMAIN]["config"])


def _curve(config: settings.HeatPumpSettings) -> tuple[tuple[float, float], ...]:
    return heat_pump.COP_CURVES.get(config.model, ())


def _seasons(config: settings.HeatPumpSettings):
    return heat_pump.DISTRICT_HEATING_PRESETS.get(config.district_heating, ())


def curve(hass: HomeAssistant) -> tuple[tuple[float, float], ...]:
    """Return the heat pump's COP curve (heat_pump_model)."""
    return _curve(_settings(hass))


def _power_kw(
    hass: HomeAssistant,
    config: settings.HeatPumpSettings,
    cop_curve,
    indoor: float | None,
    outdoor: float | None,
) -> float:
    """Return what the heat pump draws while it heats in kW.

    From the heat loss the forecast measured, the live indoor and the outdoor
    temperature, else the heat_pump_power setting. A setting of 0 turns planning off.
    """
    setting = config.power_kw
    loss = (hass.data[DOMAIN].get("forecast") or {}).get("heat_pump_loss")
    if setting <= 0 or not loss or indoor is None or outdoor is None:
        return setting
    return heat_pump.draw(loss, indoor, outdoor, cop_curve)


def _spot(hass: HomeAssistant, now: dt.datetime) -> float | None:
    """Return the published spot price now (SEK/MWh excl. VAT), also without a plan.

    The prices are keyed in UTC, so the hour repeated when DST ends has its own.
    """
    return (hass.data[DOMAIN].get("spot_prices") or {}).get(quarter_start(now))


def heat_pump_economy(hass: HomeAssistant) -> dict:
    """Return the heat pump economy now (see heat_pump.evaluate).

    Without the inputs only the district heating price and temperature are given.
    """
    now = dt_utils.now()
    config = _settings(hass)
    district = heat_pump.district_heating_price(_seasons(config), now)
    cop_curve = _curve(config)
    spot = _spot(hass, now)
    outdoor = settings.reading(hass, config.outdoor_sensor)
    if district is None or outdoor is None or spot is None or not cop_curve:
        return {"district_heating_price": district, "outdoor_temperature": outdoor}
    tariff = get_tariff(hass)
    indoor = settings.reading(hass, config.indoor_sensor)
    power = _power_kw(hass, config, cop_curve, indoor, outdoor)
    # The plan's export this quarter, even an old plan's
    plan: Plan | None = hass.data[DOMAIN].get("plan")
    index = plan.index_at(now) if plan is not None else None
    export = (
        plan.grid_export_kw[index] if plan is not None and index is not None else 0.0
    )
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
    config = _settings(hass)
    live = settings.reading(hass, config.outdoor_sensor)
    indoor = settings.reading(hass, config.indoor_sensor)
    temps = [live if t is None else t for t in temperatures]
    cop_curve = _curve(config)
    seasons = _seasons(config)
    limit = config.heating_limit_c
    hours = QUARTER.total_seconds() / 3600
    draws = [
        _power_kw(hass, config, cop_curve, indoor, t) * hours if cop_curve else 0.0
        for t in temps
    ]
    heating = [t is not None and t < limit for t in temps]
    drawing = [kwh for kwh in draws if kwh > 0]
    return PlanInputs(
        # More load is priced at the mean draw
        probe_kwh=sum(drawing) / len(drawing) if drawing else 0.0,
        draws=draws,
        margin=config.margin_ore_per_kwh,
        temperatures=temps,
        cops=[None if t is None else heat_pump.cop(cop_curve, t) for t in temps],
        district=[heat_pump.district_heating_price(seasons, s) for s in starts],
        heating=heating,
    )


def heat_pump_action(hass: HomeAssistant) -> dict:
    """Return whether the heat pump should heat now (see heat_pump.action)."""
    config = _settings(hass)
    return heat_pump.action(
        hass.data[DOMAIN].get("plan"),
        dt_utils.now(),
        lambda: heat_pump_economy(hass),
        config.margin_ore_per_kwh,
        config.heating_limit_c,
    )
