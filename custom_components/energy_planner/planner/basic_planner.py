import datetime as dt
import logging
from datetime import timezone as ts
from homeassistant.core import ServiceCall, HomeAssistant
from homeassistant.util import dt as dt_utils
from .nordpool_utils import join_result_for_correct_time, parse_json
from ..const import DATE_TIME_ENTITIES, DOMAIN, TIME_ENTITIES, SELECT_ENTITIES, SWITCH_ENTITIES, SENSOR_ENTITIES, NUMBER_ENTITIES

_LOGGER = logging.getLogger(__name__)
async def fetch_nordpool_data(hass: HomeAssistant, nordpool_currency: str, nordpool_area: str):
    now = dt_utils.now()
    yesterdays_yesterdays_values = await hass.services.async_call('nordpool', 'hourly', {
        "currency": nordpool_currency,
        "area": nordpool_area,
        "date": (now - dt.timedelta(days=2)).strftime("%Y-%m-%d")
    }, True, return_response=True)
    yesterdays_values = await hass.services.async_call('nordpool', 'hourly', {
        "currency": nordpool_currency,
        "area": nordpool_area,
        "date": (now - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    }, True, return_response=True)
    todays_values = await hass.services.async_call('nordpool', 'hourly', {
        "currency": nordpool_currency,
        "area": nordpool_area,
        "date": now.strftime("%Y-%m-%d")
    }, True, return_response=True)
    tomorrows_values = await hass.services.async_call('nordpool', 'hourly', {
        "currency": nordpool_currency,
        "area": nordpool_area,
        "date": (now + dt.timedelta(days=1)).strftime("%Y-%m-%d")
    }, True, return_response=True)
    tomorrows_tomorrows_values = await hass.services.async_call('nordpool', 'hourly', {
        "currency": nordpool_currency,
        "area": nordpool_area,
        "date": (now + dt.timedelta(days=2)).strftime("%Y-%m-%d")
    }, True, return_response=True)
    yesterday = await join_result_for_correct_time([
        parse_json(yesterdays_yesterdays_values, nordpool_currency, areas=[nordpool_area]),
        parse_json(yesterdays_values, nordpool_currency, areas=[nordpool_area]),
        parse_json(todays_values, nordpool_currency, areas=[nordpool_area])
    ], now - dt.timedelta(days=1))
    today = await join_result_for_correct_time([
        parse_json(yesterdays_values, nordpool_currency, areas=[nordpool_area]),
        parse_json(todays_values, nordpool_currency, areas=[nordpool_area]),
        parse_json(tomorrows_values, nordpool_currency, areas=[nordpool_area])
    ], now)
    tomorrow = await join_result_for_correct_time([
        parse_json(todays_values, nordpool_currency, areas=[nordpool_area]),
        parse_json(tomorrows_values, nordpool_currency, areas=[nordpool_area]),
        parse_json(tomorrows_tomorrows_values, nordpool_currency, areas=[nordpool_area])
    ], now + dt.timedelta(days=1))
    return yesterday, today, tomorrow

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
    _LOGGER.info("Received planning data: %s", call.data)
    _LOGGER.info("Nordpool entity: %s", attributes)
    today = [(i, x, 0) for i, x in enumerate(attributes.get('today'))]
    tomorrow = [(i, x, 1) for i, x in enumerate(attributes.get('tomorrow'))]
    if today is None or tomorrow is None:
        raise ValueError("Attributes 'today' or 'tomorrow' not found in nordpool_state")
    tomorrow_valid = attributes.get('tomorrow_valid')
    if not tomorrow_valid:
        raise ValueError("Tomorrow's prices are not valid")
    yesterday, today, tomorrow = await fetch_nordpool_data(hass, nordpool_currency, nordpool_area)
    _LOGGER.info("Nordpool response: %s", today)

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