import datetime
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import RestoreSensor
from homeassistant.config_entries import ConfigEntry

from custom_components.energy_planner.const import DOMAIN, SELECT_ENTITIES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    _LOGGER.info("Setting up datetime platform")
    selects = [
        *[
            EnergyPlannerSelectEntity(hass, {
                "id": f"slot_{i}_state", "options": ["charge", "discharge", "pause", "off"], "default": "off",
                "name": f"Slot {i} state", "enabled": True})
            for i in range(1, 50)
        ],
        EnergyPlannerSelectEntity(hass, {
            "id": "planner_state", "options": ["basic", "dynamic", "off"], "default": "basic",
            "name": "Planner state", "enabled": True})
    ]

    hass.data[DOMAIN][SELECT_ENTITIES] = selects

    for select in selects:
        hass.data[DOMAIN]['values'][select.id] = select.native_value
    async_add_devices(selects, True)
    # Return boolean to indicate that initialization was successful
    return True


class EnergyPlannerSelectEntity(RestoreSensor, SelectEntity):
    """Representation of a Number entity."""

    def __init__(self, hass, entity_definition):
        """Initialize the Number entity."""
        #
        # Visible Instance Attributes Outside Class
        self._hass = hass
        self.id = entity_definition["id"]
        # Hidden Inherited Instance Attributes
        self._attr_unique_id = "{}_{}".format(DOMAIN, self.id)
        self._attr_has_entity_name = True
        self._attr_name = entity_definition["name"]
        self._attr_assumed_state = entity_definition.get("assumed", False)
        self._attr_options = entity_definition.get("options", [])
        self._attr_current_option = entity_definition.get("default", None)
        self._attr_available = True
        self.is_added_to_hass = False
        self._attr_device_class = entity_definition.get("device_class", None)
        self._attr_icon = entity_definition.get("icon", None)
        self._attr_native_step = entity_definition.get("step", 1.0)
        self._attr_should_poll = False
        self._attr_entity_registry_enabled_default = entity_definition.get("enabled", False)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.is_added_to_hass = True

    def update(self):
        """Update Modbus data periodically."""
        self._attr_available = True
        value = self._hass.data[DOMAIN]['values'].get(self.id, None)
        self._attr_native_value = value
        self._attr_current_option = value
        self.schedule_update_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Update the current value."""
        self._attr_current_option = option
        self._attr_native_value = option
        self._hass.data[DOMAIN]['values'][self.id] = option
        self.schedule_update_ha_state()
