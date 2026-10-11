"""HA glue for the main fuse guard (fuse.py): live readings in, limits out.

Config store only (no defaults: without them there is no guard):
`grid_phase_import_sensors` and optionally `grid_phase_export_sensors` (one per
phase, W or kW, e.g. the HAN port's `sensor.l1_aktiv_effekt_uttag_momentan_effekt`),
`inverter_ac_power_sensor` (W or kW, positive while the inverter delivers power to
the house, e.g. Solis `ac_grid_port_power`), `inverter_grid_power_sensor` (W or kW,
the grid import as the inverter's own meter sees it, which its peak shaving limits;
at the owner's parents it reads about 1.3 kW less than the electricity meter, so the
grid limit is shifted by the difference), `ev_phase_current_sensors` (the
charger's current per phase, A) and `ev_charger_current_number` (its current
setting, A). The fuse is `number.energy_planner_main_fuse`, the margin
`number.energy_planner_fuse_margin`.

The result is in `hass.data[DOMAIN]["fuse_guard"]` (None without a guard), published
by `sensor.energy_planner_fuse_guard`; the inverter script and the car automation
apply it.
"""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

from ..const import DOMAIN
from . import fuse
from .battery_plan import ev_plugged
from .ev import ev_action

_LOGGER = logging.getLogger(__name__)

# Phase readings older than this are not used (the meter reports every 10-20 s)
MAX_AGE_SECONDS = 120


def _value(hass: HomeAssistant, entity_id: str, now, *, kilo: bool) -> float | None:
    """Return a sensor's value (kW when `kilo`, by its unit), None if missing or old."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    if (now - state.last_reported).total_seconds() > MAX_AGE_SECONDS:
        return None
    unit = state.attributes.get("unit_of_measurement")
    if kilo and unit == "W":
        return value / 1000
    if not kilo and unit == "kW":
        return value * 1000
    return value


def config_entities(config: dict) -> list[str]:
    """Return the entities whose changes update the guard."""
    return [
        *(config.get("grid_phase_import_sensors") or []),
        *(config.get("grid_phase_export_sensors") or []),
        *(
            config[key]
            for key in ("inverter_ac_power_sensor", "inverter_grid_power_sensor")
            if config.get(key)
        ),
        *(config.get("ev_phase_current_sensors") or []),
    ]


def _reading(hass: HomeAssistant, config: dict, now) -> fuse.Reading | None:
    """Return the live reading, None if a phase is missing."""
    imports = config.get("grid_phase_import_sensors") or []
    exports = config.get("grid_phase_export_sensors") or []
    phase_kw = []
    for index, entity_id in enumerate(imports):
        kw = _value(hass, entity_id, now, kilo=True)
        if kw is None:
            return None
        if index < len(exports):
            kw -= _value(hass, exports[index], now, kilo=True) or 0.0
        phase_kw.append(kw)
    inverter = config.get("inverter_ac_power_sensor")
    delivered = _value(hass, inverter, now, kilo=True) if inverter else None
    currents = [
        _value(hass, entity_id, now, kilo=False) or 0.0
        for entity_id in config.get("ev_phase_current_sensors") or []
    ]
    setting_id = config.get("ev_charger_current_number")
    setting = hass.states.get(setting_id) if setting_id else None
    try:
        setting_a = float(setting.state) if setting is not None else None
    except ValueError:
        setting_a = None
    return fuse.Reading(
        tuple(phase_kw),
        -(delivered or 0.0),
        max(currents, default=0.0),
        setting_a,
    )


def update_guard(hass: HomeAssistant) -> None:
    """Recalculate the guard from the live readings (kept while a reading is missing).

    The grid limit and the charger's current are smoothed (`fuse.smooth`), so the
    inverter and the charger's cloud API only get real changes.
    """
    data = hass.data[DOMAIN]
    config = data["config"]
    fuse_a = float(config.get("main_fuse") or 0)
    if fuse_a <= 0 or not config.get("grid_phase_import_sensors"):
        data["fuse_guard"] = None
        return
    now = dt_utils.now()
    reading = _reading(hass, config, now)
    if reading is None:
        return
    max_power = float(config.get("ev_charger_max_power") or 11000)
    guard = fuse.limits(
        reading,
        fuse_a,
        float(config.get("fuse_margin", 2) or 0),
        # An 11 kW charger is 16 A
        round(max_power / (len(reading.phase_kw) * fuse.VOLTAGE)),
        ev_wanted=ev_action(data.get("plan"), now, ev_plugged(hass))["state"]
        == "charge",
    )
    meter_id = config.get("inverter_grid_power_sensor")
    meter_kw = _value(hass, meter_id, now, kilo=True) if meter_id else None
    if meter_kw is not None:
        # The inverter limits its own meter's reading: shift by what it misses
        offset = sum(reading.phase_kw) - meter_kw
        guard["grid_limit_kw"] = max(guard["grid_limit_kw"] - offset, 0.0)
        guard["inverter_meter_offset_kw"] = round(offset, 2)
    smoothing = data.setdefault("fuse_guard_smoothing", {})
    smoothing["grid"] = fuse.smooth(
        smoothing.get("grid"),
        guard["grid_limit_kw"],
        now,
        fuse.GRID_STEP_KW,
        fuse.GRID_RAISE_INTERVAL,
        fuse.GRID_MARGIN_KW,
    )
    guard["grid_limit_kw"] = round(smoothing["grid"][0], 1)
    if config.get("ev_charger_current_number"):
        smoothing["ev"] = fuse.smooth(
            smoothing.get("ev"),
            guard["ev_current_a"],
            now,
            fuse.EV_STEP_A,
            fuse.EV_RAISE_INTERVAL,
        )
        guard["ev_current_a"] = int(smoothing["ev"][0])
    else:
        guard["ev_current_a"] = None
    guard["updated"] = now.isoformat()
    data["fuse_guard"] = guard
