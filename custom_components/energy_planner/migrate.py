"""Move stored data and entities from older versions of the integration.

The rule-based planners (basic, cheapest hours, price peak) were removed in Oct 2026,
see docs/adr/0001-one-planner-no-schedule.md. Their settings are dropped from the
config store, `planner_state` became the `enabled` switch and the round-trip
efficiency moved to `battery_efficiency`. Their entities are removed from the entity
registry, so they do not linger as unavailable.
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
# Old key -> new key, the value kept
RENAMED_SETTINGS = {"price_peak_efficiency_factor": "battery_efficiency"}


# What planner_state was for the rule-based planners: their slots must be replaced
RULE_BASED_PLANNERS = ("basic", "cheapest hours", "price peak")


def migrate_config(config: MutableMapping[str, Any]) -> bool:
    """Move the stored settings of an older version to the current keys.

    Returns whether a removed rule-based planner was selected: its slots are still in
    the schedule and must be replaced by the dynamic planner's.
    """
    replan = False
    if "planner_state" in config:
        state = config.pop("planner_state")
        config.setdefault("enabled", state != "off")
        replan = state in RULE_BASED_PLANNERS
    for old, new in RENAMED_SETTINGS.items():
        if old in config:
            value = config.pop(old)
            if config.get(new) is None:
                config[new] = value
    for _, key in REMOVED_ENTITIES:
        config.pop(key, None)
    return replan


def remove_entities(hass: HomeAssistant) -> None:
    """Remove the registry entries of entities the integration no longer has."""
    registry = er.async_get(hass)
    for platform, key in REMOVED_ENTITIES:
        entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{DOMAIN}_{key}")
        if entity_id is not None:
            _LOGGER.info("Removing %s, no longer used", entity_id)
            registry.async_remove(entity_id)
