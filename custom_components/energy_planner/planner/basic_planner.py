import datetime as dt
import logging

from homeassistant.core import ServiceCall, HomeAssistant

from .nordpool_utils import join_result_for_correct_time
from ..const import DATE_TIME_ENTITIES, DOMAIN, TIME_ENTITIES, SELECT_ENTITIES, SWITCH_ENTITIES, SENSOR_ENTITIES, NUMBER_ENTITIES

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
    nordpool_currency = nordpool_entity_id.split('_')[3]
    nordpool_area = nordpool_entity_id.split('_')[2]

    nordpool_state = hass.states.get(nordpool_entity_id)
    if nordpool_state is None:
        raise ValueError("Nordpool entity not found")
    attributes = nordpool_state.attributes
    _LOGGER.info("Running planner")
    _LOGGER.info("Received planning data: %s", call.data)
    _LOGGER.info("Nordpool entity: %s", attributes)
    today = [(i, x, 0) for i, x in enumerate(attributes.get('today'))]
    tomorrow = [(i, x, 1) for i, x in enumerate(attributes.get('tomorrow'))]
    if today is None or tomorrow is None:
        raise ValueError("Attributes 'today' or 'tomorrow' not found in nordpool_state")
    tomorrow_valid = attributes.get('tomorrow_valid')
    if not tomorrow_valid:
        raise ValueError("Tomorrow's prices are not valid")

    yesterdays_values = await hass.services.async_call('nordpool', 'hourly', {
        "currency": str(nordpool_currency).upper(),
        "area": str(nordpool_area).upper(),
        "date": (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    }, True, return_response=True)
    todays_values = await hass.services.async_call('nordpool', 'hourly', {
        "currency": str(nordpool_currency).upper(),
        "area": str(nordpool_area).upper(),
        "date": (dt.datetime.now()).strftime("%Y-%m-%d")
    }, True, return_response=True)
    tomorrows_values = await hass.services.async_call('nordpool', 'hourly', {
        "currency": str(nordpool_currency).upper(),
        "area": str(nordpool_area).upper(),
        "date": (dt.datetime.now() + dt.timedelta(days=1)).strftime("%Y-%m-%d")
    }, True, return_response=True)
    res = await join_result_for_correct_time([yesterdays_values, todays_values, tomorrows_values], dt.datetime.now())
    _LOGGER.info("Nordpool response: %s", res)

    yesterday = [(i, x['entryPerArea'][str(nordpool_area).upper()], -1) for i, x in enumerate(res['multiAreaEntries'])]
    _LOGGER.info("Yesterday: %s", yesterday)

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