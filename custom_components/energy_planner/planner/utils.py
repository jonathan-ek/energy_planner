from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils

from ..const import DOMAIN, DATE_TIME_ENTITIES, TIME_ENTITIES, SELECT_ENTITIES, SWITCH_ENTITIES, NUMBER_ENTITIES

async def update_entities(hass: HomeAssistant, values=True, config=False):
    for entity in hass.data[DOMAIN][DATE_TIME_ENTITIES]:
        if (values and entity.data_store == 'values') or (config and entity.data_store == 'config'):
            entity.update()
    for entity in hass.data[DOMAIN][TIME_ENTITIES]:
        if (values and entity.data_store == 'values') or (config and entity.data_store == 'config'):
            entity.update()
    for entity in hass.data[DOMAIN][SELECT_ENTITIES]:
        if (values and entity.data_store == 'values') or (config and entity.data_store == 'config'):
            entity.update()
    for entity in hass.data[DOMAIN][SWITCH_ENTITIES]:
        if (values and entity.data_store == 'values') or (config and entity.data_store == 'config'):
            entity.update()
    for entity in hass.data[DOMAIN][NUMBER_ENTITIES]:
        if (values and entity.data_store == 'values') or (config and entity.data_store == 'config'):
            entity.update()


async def clear_passed_slots(hass: HomeAssistant):
    # check if slot 1 has passed
    now = dt_utils.now()
    next_slot_start = hass.data[DOMAIN]['values'].get('slot_2_date_time_start')
    if next_slot_start is None:
        return
    if type(next_slot_start) == str:
        next_slot_start = dt_utils.parse_datetime(next_slot_start)
    if now > next_slot_start:
        # shift all slots one step back
        for i in range(2, 50):
            hass.data[DOMAIN]['values'][f"slot_{i-1}_date_time_start"] = hass.data[DOMAIN]['values'].get(f"slot_{i}_date_time_start")
            hass.data[DOMAIN]['values'][f"slot_{i-1}_active"] = hass.data[DOMAIN]['values'].get(f"slot_{i}_active")
            hass.data[DOMAIN]['values'][f"slot_{i-1}_state"] = hass.data[DOMAIN]['values'].get(f"slot_{i}_state")
        await update_entities(hass)
        await hass.data[DOMAIN]['save']()
