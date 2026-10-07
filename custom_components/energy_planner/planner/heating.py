"""HA glue for the heat pump economy (heat_pump.py), shown by the heat pump sensors."""

import datetime as dt

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

from ..const import (
    DEFAULT_DISTRICT_HEATING,
    DEFAULT_HEAT_PUMP_MODEL,
    DEFAULT_OUTDOOR_TEMPERATURE_SENSOR,
    DOMAIN,
)
from .battery_action import QUARTER
from .heat_pump import (
    COP_CURVES,
    DISTRICT_HEATING_PRESETS,
    district_heating_price,
    evaluate,
)
from .utils import get_tariff

# kW the heat pump typically draws: the planned export covers this much of it
HEAT_PUMP_INPUT_KW = 1.0


def _quarter(moment: dt.datetime) -> dt.datetime:
    return moment.replace(
        minute=moment.minute - moment.minute % 15, second=0, microsecond=0
    )


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
    """Return the heat pump economy now (see heat_pump.evaluate), or {}."""
    data = hass.data[DOMAIN]
    config = data["config"]
    now = dt_utils.now()
    seasons = DISTRICT_HEATING_PRESETS.get(
        config.get("district_heating", DEFAULT_DISTRICT_HEATING), ()
    )
    district = district_heating_price(seasons, now)
    curve = COP_CURVES.get(config.get("heat_pump_model", DEFAULT_HEAT_PUMP_MODEL), ())
    state = hass.states.get(
        config.get("outdoor_temperature_sensor", DEFAULT_OUTDOOR_TEMPERATURE_SENSOR)
    )
    spot = (data.get("prices") or {}).get(_quarter(now))
    try:
        outdoor = float(state.state) if state is not None else None
    except ValueError:
        outdoor = None
    if district is None or outdoor is None or spot is None or not curve:
        return {"district_heating_price": district, "outdoor_temperature": outdoor}
    tariff = get_tariff(hass)
    result = evaluate(
        spot,
        tariff.buy_fee(now),
        tariff.sell_fee(now),
        outdoor,
        curve,
        district,
        _planned_export(hass, now) / HEAT_PUMP_INPUT_KW,
    )
    result["spot"] = round(spot / 10, 1)
    return result
