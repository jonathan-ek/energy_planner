import logging
from homeassistant.core import ServiceCall, HomeAssistant
from .nordpool_utils import fetch_nordpool_data
from ..const import DATE_TIME_ENTITIES, DOMAIN, TIME_ENTITIES, SELECT_ENTITIES, SWITCH_ENTITIES, NUMBER_ENTITIES
from homeassistant.util import dt as dt_utils

_LOGGER = logging.getLogger(__name__)


async def reset(hass: HomeAssistant):
    _LOGGER.info("Resetting planner")
    for entity in hass.data[DOMAIN][DATE_TIME_ENTITIES]:
        if entity.data_store == 'values':
            hass.data[DOMAIN][entity.data_store][entity.id] = None
    for entity in hass.data[DOMAIN][TIME_ENTITIES]:
        if entity.data_store == 'values':
            hass.data[DOMAIN][entity.data_store][entity.id] = None
    for entity in hass.data[DOMAIN][SELECT_ENTITIES]:
        if entity.data_store == 'values':
            hass.data[DOMAIN][entity.data_store][entity.id] = 'off'
    for entity in hass.data[DOMAIN][SWITCH_ENTITIES]:
        if entity.data_store == 'values':
            hass.data[DOMAIN][entity.data_store][entity.id] = False
    for entity in hass.data[DOMAIN][NUMBER_ENTITIES]:
        if entity.data_store == 'values':
            hass.data[DOMAIN][entity.data_store][entity.id] = None


async def planner(hass: HomeAssistant, call: ServiceCall):
    nordpool_entity_id = hass.data[DOMAIN]['config'].get("nordpool_entity_id")
    nordpool_currency = str(nordpool_entity_id.split('_')[3]).upper()
    nordpool_area = str(nordpool_entity_id.split('_')[2]).upper()

    nordpool_state = hass.states.get(nordpool_entity_id)
    if nordpool_state is None:
        raise ValueError("Nordpool entity not found")
    attributes = nordpool_state.attributes
    _LOGGER.info("Running planner")

    tomorrow_valid = attributes.get('tomorrow_valid')
    now = dt_utils.now()
    date = now.strftime("%Y-%m-%d")
    yesterday, today, tomorrow = await fetch_nordpool_data(hass, nordpool_currency, nordpool_area, tomorrow_valid)

    earliest_charge = hass.data[DOMAIN]['config'].get("earliest_charge_time")
    earliest_discharge = hass.data[DOMAIN]['config'].get("earliest_discharge_time")
    nr_of_charge_hours = hass.data[DOMAIN]['config'].get("basic_nr_of_charge_hours")
    nr_of_discharge_hours = hass.data[DOMAIN]['config'].get("basic_nr_of_discharge_hours")
    _LOGGER.info("Basic planner settings: %s", {
        "earliest_charge": earliest_charge,
        "earliest_discharge": earliest_discharge,
        "nr_of_charge_hours": nr_of_charge_hours,
        "nr_of_discharge_hours": nr_of_discharge_hours
    })
    _LOGGER.info("Setting up basic planner")
