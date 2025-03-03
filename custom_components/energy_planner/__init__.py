import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback, Event, EventStateChangedData
from homeassistant.const import Platform
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.NUMBER, Platform.SWITCH, Platform.TIME, Platform.DATETIME]


async def async_setup(hass, config):
    """Set up the Energy Planner component."""
    hass.data[DOMAIN] = {
        "values": []
    }

    hass.helpers.discovery.load_platform('sensor', DOMAIN, {}, config)

    # Register the configuration flow.
    @callback
    def set_state_service(call: ServiceCall) -> None:
        """Service to send a message."""
        start_time = call.data.get("start_time")
        end_time = call.data.get("end_time")
        start_date = call.data.get("start_date")
        state = call.data.get("state")
        hass.states.set(f"{DOMAIN}.start_time", start_time)
        hass.states.set(f"{DOMAIN}.end_time", end_time)
        hass.states.set(f"{DOMAIN}.start_date", start_date)
        hass.states.set(f"{DOMAIN}.state", state)
        _LOGGER.info("Received data: %s", call.data)

    @callback
    def run_planner_service(call: ServiceCall) -> None:
        _LOGGER.info("Received planning data: %s", call.data)

    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, 'set_state', set_state_service)
    hass.services.async_register(DOMAIN, 'run_planner', run_planner_service)
    # Return boolean to indicate that initialization was successful.
    return True


async def state_automation_listener(event: Event[EventStateChangedData]):
    """Handle state change event."""
    _LOGGER.debug(f"state_automation_listener: {event.data}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Modbus from a config entry."""
    # Set up the platforms associated with this integration
    for component in PLATFORMS:
        hass.async_create_task(hass.config_entries.async_forward_entry_setup(entry, component))
        _LOGGER.debug(f"async_setup_entry: loading: {component}")
        await asyncio.sleep(1)
    await asyncio.sleep(20)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Modbus config entry."""
    _LOGGER.debug('init async_unload_entry')
    # Unload platforms associated with this integration
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, DOMAIN)

    return unload_ok
