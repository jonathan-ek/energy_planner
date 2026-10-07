"""Button to recalculate the battery plan."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN
from .planner import async_request_plan

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the button platform."""
    async_add_devices([EnergyPlannerUpdatePlanButton(hass)])
    return True


class EnergyPlannerUpdatePlanButton(ButtonEntity):
    """Recalculate the battery plan now, e.g. after changing a setting."""

    _attr_has_entity_name = True
    _attr_name = "Update battery plan"
    _attr_icon = "mdi:battery-sync"

    def __init__(self, hass):
        """Initialize the button."""
        self._hass = hass
        self.entity_id = f"button.{DOMAIN}_update_battery_plan"
        self._attr_unique_id = f"{DOMAIN}_update_battery_plan"

    async def async_press(self) -> None:
        """Recalculate the battery plan."""
        await async_request_plan(self._hass, "button")
