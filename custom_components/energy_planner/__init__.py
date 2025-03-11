import asyncio
import logging
import datetime as dt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback, Event, EventStateChangedData
from homeassistant.const import Platform
from homeassistant.helpers.event import async_track_utc_time_change, async_track_time_interval, async_call_later

from .const import DOMAIN, DATE_TIME_ENTITIES, NUMBER_ENTITIES, SWITCH_ENTITIES, SENSOR_ENTITIES, SELECT_ENTITIES, TIME_ENTITIES
from .planner import basic_planner
from .planner.utils import clear_passed_slots, update_entities
from .store import async_save_to_store, async_load_from_store
from .utils import tz_diff

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.DATETIME, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH, Platform.TIME]

async def async_setup_data_structure(hass: HomeAssistant):
    async def save():
        for data_store in ['values', 'config']:
            await async_save_to_store(hass, data_store, hass.data[DOMAIN][data_store])

    hass.data[DOMAIN] = {
        "values": {},
        "config": {},
        "add_slot_values": [],
        DATE_TIME_ENTITIES: {},
        TIME_ENTITIES: {},
        NUMBER_ENTITIES: {},
        SWITCH_ENTITIES: {},
        SENSOR_ENTITIES: {},
        SELECT_ENTITIES: {},
        'save': save
    }
    hass.data[DOMAIN]['values'] = await async_load_from_store(hass, 'values')
    hass.data[DOMAIN]['config'] = await async_load_from_store(hass, 'config')

async def async_setup(hass: HomeAssistant, config):
    """Set up the Energy Planner component."""
    if DOMAIN not in hass.data:
        await async_setup_data_structure(hass)

    @callback
    async def add_slot_service(call: ServiceCall) -> None:
        """Service to add a slot."""
        start_time = call.data.get("start_time")
        end_time = call.data.get("end_time")
        start_date = call.data.get("start_date")
        state = call.data.get("state")
        hass.data[DOMAIN]["add_slot_values"].append({
            "start_time": start_time,
            "end_time": end_time,
            "start_date": start_date,
            "state": state
        })
        await hass.data[DOMAIN]['save']()
        _LOGGER.info("Received data: %s", call.data)
        _LOGGER.info("Current values: %s", hass.data[DOMAIN]["values"])

    @callback
    async def run_planner(*args, **kwargs) -> None:
        _LOGGER.info("Run planner args, %s", args)
        _LOGGER.info("Run planner kwargs, %s", kwargs)
        planner_state = hass.data[DOMAIN]['config'].get("planner_state", "basic")
        if planner_state == "off":
            _LOGGER.info("Planner is off")
            return
        if planner_state == "basic":
            _LOGGER.info("Running basic planner")
            await basic_planner(hass)
            return
        if planner_state == "dynamic":
            _LOGGER.info("Running dynamic planner")
            return
        raise ValueError("Invalid planner state")

    @callback
    async def run_planner_service(call: ServiceCall) -> None:
        """Service to run the planner."""
        _LOGGER.info("Running planner: %s", config)
        _LOGGER.info("Received planning data: %s", call.data)
        await run_planner()


    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, 'add_slot', add_slot_service)
    hass.services.async_register(DOMAIN, 'run_planner', run_planner_service)

    update_schedule_timer = async_track_utc_time_change(
        hass,
        run_planner,
        hour=(13 + int(tz_diff('Europe/Stockholm', 'UTC'))) % 24,
        minute=31,
        second=15,
    )

    @callback
    async def check_schedule(*args, **kwargs):
        _LOGGER.info("Checking schedule args, %s", args)
        _LOGGER.info("Checking schedule kwargs, %s", kwargs)
        await clear_passed_slots(hass)


    check_schedule_timer = async_track_time_interval(
        hass,
        check_schedule,
        dt.timedelta(minutes=1),
    )

    @callback
    async def update_config_after_load(*args, **kwargs):
        hass.data[DOMAIN]['values'] = await async_load_from_store(hass, 'values')
        hass.data[DOMAIN]['config'] = {
            **(await async_load_from_store(hass, 'config')),
            **hass.data[DOMAIN]['config'],
        }
        await update_entities(hass, config=True)

    async_call_later(
        hass,
        60,
        update_config_after_load
    )
    await asyncio.sleep(30)
    # Return boolean to indicate that initialization was successful.
    return True


async def state_automation_listener(event: Event[EventStateChangedData]):
    """Handle state change event."""
    _LOGGER.debug(f"state_automation_listener: {event.data}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Modbus from a config entry."""
    # Set up the platforms associated with this integration
    if DOMAIN not in hass.data:
        await async_setup_data_structure(hass)
    hass.data[DOMAIN]["config"] = {
        "entry_id": entry.entry_id,
        "nordpool_entity_id": entry.data["nordpool_entity_id"]
    }
    hass.async_create_task(hass.config_entries.async_forward_entry_setups(entry, PLATFORMS))
    await asyncio.sleep(30)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Modbus config entry."""
    _LOGGER.debug('init async_unload_entry')
    # Unload platforms associated with this integration
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, DOMAIN)

    return unload_ok
