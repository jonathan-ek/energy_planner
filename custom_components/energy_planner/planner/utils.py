import logging

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

import datetime as dt

from ..const import (
    DOMAIN,
    DATE_TIME_ENTITIES,
    TIME_ENTITIES,
    SELECT_ENTITIES,
    SWITCH_ENTITIES,
    NUMBER_ENTITIES,
)

_LOGGER = logging.getLogger(__name__)


async def store_disable_state(hass: HomeAssistant):
    """Store disable state."""
    _LOGGER.info("Resetting planner")
    if "tmp" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["tmp"] = {}
    hass.data[DOMAIN]["tmp"]["disable_state"] = []
    for i in range(1, 50):
        if (
            hass.data[DOMAIN]["values"][f"slot_{i}_state"] != "off"
            and not hass.data[DOMAIN]["values"][f"slot_{i}_active"]
        ):
            hass.data[DOMAIN]["tmp"]["disable_state"].append(
                {
                    "start": str(
                        hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"]
                    ),
                    "end": str(
                        hass.data[DOMAIN]["values"][f"slot_{i + 1}_date_time_start"]
                    ),
                    "state": hass.data[DOMAIN]["values"][f"slot_{i}_state"],
                    "active": hass.data[DOMAIN]["values"][f"slot_{i}_active"],
                    "soc": hass.data[DOMAIN]["values"][f"slot_{i}_soc"],
                }
            )


async def restore_disable_state(hass: HomeAssistant):
    """Restore disable state."""
    _LOGGER.info("Resetting planner")
    if "tmp" not in hass.data[DOMAIN]:
        return
    if "disable_state" not in hass.data[DOMAIN]["tmp"]:
        return
    for i in range(1, 49):
        for s in hass.data[DOMAIN]["tmp"]["disable_state"]:
            if (
                str(hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"])
                == s["start"]
                and str(hass.data[DOMAIN]["values"][f"slot_{i + 1}_date_time_start"])
                == s["end"]
            ):
                hass.data[DOMAIN]["values"][f"slot_{i}_active"] = False
    del hass.data[DOMAIN]["tmp"]["disable_state"]


async def reset(hass: HomeAssistant):
    """Reset planner."""
    _LOGGER.info("Resetting planner")
    for i in range(1, 50):
        hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"] = None
        hass.data[DOMAIN]["values"][f"slot_{i}_state"] = "off"
        hass.data[DOMAIN]["values"][f"slot_{i}_active"] = False
        hass.data[DOMAIN]["values"][f"slot_{i}_soc"] = 50


def parse_datetime(val, zone=None):
    """Parse datetime."""
    if zone is None:
        return dt_utils.parse_datetime(val) if type(val) is str else val
    tmp = dt.datetime.fromisoformat(val) if type(val) is str else val
    return tmp.astimezone(zone)


async def update_entities(hass: HomeAssistant, values=True, config=False):
    """Update entities."""
    for platform in [
        DATE_TIME_ENTITIES,
        TIME_ENTITIES,
        SELECT_ENTITIES,
        SWITCH_ENTITIES,
        NUMBER_ENTITIES,
    ]:
        for entity in hass.data[DOMAIN][platform]:
            if (values and entity.data_store == "values") or (
                config and entity.data_store == "config"
            ):
                entity.update()


async def clear_passed_slots(hass: HomeAssistant):
    """Clear passed slots."""
    now = dt_utils.now()
    next_slot_start = hass.data[DOMAIN]["values"].get("slot_2_date_time_start")
    if next_slot_start is None:
        return
    if type(next_slot_start) is str:
        next_slot_start = dt_utils.parse_datetime(next_slot_start)
    if now > next_slot_start:
        # shift all slots one step back
        for i in range(2, 50):
            hass.data[DOMAIN]["values"][f"slot_{i - 1}_date_time_start"] = hass.data[
                DOMAIN
            ]["values"].get(f"slot_{i}_date_time_start")
            hass.data[DOMAIN]["values"][f"slot_{i - 1}_active"] = hass.data[DOMAIN][
                "values"
            ].get(f"slot_{i}_active")
            hass.data[DOMAIN]["values"][f"slot_{i - 1}_state"] = hass.data[DOMAIN][
                "values"
            ].get(f"slot_{i}_state")
            hass.data[DOMAIN]["values"][f"slot_{i - 1}_soc"] = hass.data[DOMAIN][
                "values"
            ].get(f"slot_{i}_soc")
        for s in hass.data[DOMAIN]["manual_slots"]:
            end = s["end"]
            if type(end) is str:
                end = dt_utils.parse_datetime(s["end"])
            if end < now:
                hass.data[DOMAIN]["manual_slots"].remove(s)

        await update_entities(hass)
        await hass.data[DOMAIN]["save"]()
