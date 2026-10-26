"""Move stored data and entities from older versions of the integration.

The rule-based planners (basic, cheapest hours, price peak) and the schedule of 49
slots were removed in Oct 2026, see docs/adr/0001-one-planner-no-schedule.md. Their
settings are dropped from the config store and the slots from the values store,
`planner_state` became the `enabled` switch and the round-trip efficiency moved to
`battery_efficiency`. Their entities are removed from the entity registry, so they
do not linger as unavailable. Manual slots stay (manual.py).
"""

from collections.abc import MutableMapping
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# (platform, setting key) of the removed planners' entities
REMOVED_ENTITIES = (
    ("select", "planner_state"),
    ("select", "price_peak_planner_cheap_state"),
    ("select", "price_peak_planner_expensive_state"),
    ("select", "price_peak_planner_inbetween_state"),
    ("time", "earliest_charge_time"),
    ("time", "earliest_discharge_time"),
    ("number", "basic_nr_of_charge_hours"),
    ("number", "basic_nr_of_discharge_hours"),
    ("number", "cheapest_hours_nr_of_charge_hours"),
    ("number", "price_peak_nr_of_charge_hours"),
    ("number", "price_peak_nr_of_discharge_hours"),
    ("number", "price_peak_efficiency_factor"),
)
# The schedule's slots, each four entities
SLOT_COUNT = 49
SLOT_ENTITIES = tuple(
    (platform, f"slot_{i}_{key}")
    for i in range(1, SLOT_COUNT + 1)
    for platform, key in (
        ("datetime", "date_time_start"),
        ("select", "state"),
        ("number", "soc"),
        ("switch", "active"),
    )
)
# Old key -> new key, the value kept
RENAMED_SETTINGS = {"price_peak_efficiency_factor": "battery_efficiency"}


def migrate_config(config: MutableMapping[str, Any]) -> bool:
    """Move the stored settings of an older version to the current keys.

    Returns whether anything changed, so the store can be written.
    """
    before = dict(config)
    if "planner_state" in config:
        state = config.pop("planner_state")
        config.setdefault("enabled", state != "off")
    for old, new in RENAMED_SETTINGS.items():
        if old in config:
            value = config.pop(old)
            if config.get(new) is None:
                config[new] = value
    for _, key in REMOVED_ENTITIES:
        config.pop(key, None)
    return config != before


def migrate_values(values: MutableMapping[str, Any]) -> bool:
    """Drop the schedule's slots from the values store; return whether any were."""
    slots = [key for key in values if key.startswith("slot_")]
    for key in slots:
        del values[key]
    return bool(slots)


def remove_entities(hass: HomeAssistant) -> None:
    """Remove the registry entries of entities the integration no longer has."""
    registry = er.async_get(hass)
    for platform, key in (*REMOVED_ENTITIES, *SLOT_ENTITIES):
        entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{DOMAIN}_{key}")
        if entity_id is not None:
            _LOGGER.info("Removing %s, no longer used", entity_id)
            registry.async_remove(entity_id)
