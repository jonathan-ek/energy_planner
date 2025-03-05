import datetime as dt
import logging

from homeassistant.core import ServiceCall, HomeAssistant

from ..const import DATE_TIME_ENTITIES, DOMAIN

_LOGGER = logging.getLogger(__name__)

def planner(hass: HomeAssistant, call: ServiceCall):
    nordpool_entity_id = hass.data[DOMAIN]['config'].get("nordpool_entity_id")
    res = hass.states.get(nordpool_entity_id)
    _LOGGER.info("Running planner")
    _LOGGER.info("Received planning data: %s", call.data)
    _LOGGER.info("Nordpool entity: %s", res)
    hass.data[DOMAIN]['values']["slot_1_date_time_start"] = dt.datetime.now()
    hass.data[DOMAIN][DATE_TIME_ENTITIES][0].update()
    _LOGGER.info("Setting up basic planner")