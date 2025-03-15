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
        for s in hass.data[DOMAIN]["manual_slots"]:
            end = s["end"]
            if type(end) is str:
                end = dt_utils.parse_datetime(s["end"])
            if end < now:
                hass.data[DOMAIN]["manual_slots"].remove(s)

        await update_entities(hass)
        await hass.data[DOMAIN]["save"]()
