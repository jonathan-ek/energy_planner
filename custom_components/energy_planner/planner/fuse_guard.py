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

import datetime as dt
import logging

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

from ..const import DOMAIN
from . import fuse, settings
from .battery_plan import ev_plugged
from .ev import ev_action

_LOGGER = logging.getLogger(__name__)

# Readings older than this are not used (the meter reports every 10-20 s)
MAX_AGE = dt.timedelta(seconds=120)


def _fresh(hass: HomeAssistant, entity_ids: list[str], now) -> bool:
    """Return whether any of the entities reported within `MAX_AGE`.

    MQTT sensors (the parents' HAN port) do not write an unchanged value, so a phase
    that stays at 0 looks old; the meter is alive while any of its sensors reports.
    """
    return any(
        (state := hass.states.get(entity_id)) is not None
        and now - state.last_reported <= MAX_AGE
        for entity_id in entity_ids
    )


def config_entities(config: dict) -> list[str]:
    """Return the entities whose changes update the guard.

    Also while `main_fuse` is 0: the listeners are set up once, and a fuse set later
    should get the live readings without a reload.
    """
    return [
        *(settings.get(config, "grid_phase_import_sensors") or []),
        *(settings.get(config, "grid_phase_export_sensors") or []),
        *(
            entity_id
            for key in ("inverter_ac_power_sensor", "inverter_grid_power_sensor")
            if (entity_id := settings.get(config, key))
        ),
        *(settings.get(config, "ev_phase_current_sensors") or []),
    ]


def _reading(
    hass: HomeAssistant, guard: settings.FuseSettings, now
) -> fuse.Reading | None:
    """Return the live reading, None if a phase is missing or the meter is silent."""
    imports = guard.phase_import_sensors
    exports = guard.phase_export_sensors
    if not _fresh(hass, [*imports, *exports], now):
        return None
    phase_kw = []
    for index, entity_id in enumerate(imports):
        # The meter as a whole is fresh (_fresh): an unchanged phase is not old
        kw = settings.reading(hass, entity_id, unit="kW")
        if kw is None:
            return None
        if index < len(exports):
            kw -= settings.reading(hass, exports[index], unit="kW") or 0.0
        phase_kw.append(kw)
    delivered = settings.reading(
        hass, guard.inverter_ac_power_sensor, unit="kW", max_age=MAX_AGE, now=now
    )
    currents = [
        settings.reading(hass, entity_id, unit="A", max_age=MAX_AGE, now=now) or 0.0
        for entity_id in guard.ev_phase_current_sensors
    ]
    return fuse.Reading(
        tuple(phase_kw),
        -(delivered or 0.0),
        max(currents, default=0.0),
        settings.reading(hass, guard.ev_charger_current_number),
    )


def update_guard(hass: HomeAssistant) -> None:
    """Recalculate the guard from the live readings (kept while a reading is missing).

    The grid limit and the charger's current are smoothed (`fuse.smooth`), so the
    inverter and the charger's cloud API only get real changes.
    """
    data = hass.data[DOMAIN]
    config = settings.fuse(data["config"])
    if config is None:
        data["fuse_guard"] = None
        return
    now = dt_utils.now()
    reading = _reading(hass, config, now)
    if reading is None:
        return
    # A car that has finished draws nothing: no room is kept for it
    status_id = config.ev_charger_status_sensor
    status = hass.states.get(status_id) if status_id else None
    finished = status is not None and status.state.lower().endswith("finished")
    guard = fuse.limits(
        reading,
        config.main_fuse_a,
        config.margin_a,
        # An 11 kW charger is 16 A
        round(config.charger_max_kw * 1000 / (len(reading.phase_kw) * fuse.VOLTAGE)),
        ev_wanted=not finished
        and ev_action(data.get("plan"), now, ev_plugged(hass))["state"] == "charge",
    )
    meter_kw = settings.reading(
        hass, config.inverter_grid_power_sensor, unit="kW", max_age=MAX_AGE, now=now
    )
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
    if config.ev_charger_current_number:
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
