import datetime as dt
import logging

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.energy_planner.const import DOMAIN, TIME_ENTITIES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass, config_entry: ConfigEntry, async_add_devices: AddEntitiesCallback
):
    """Set up the time platform."""
    _LOGGER.info("Setting up time platform")
    times = [
        EnergyPlannerTimeEntity(
            hass,
            {
                "id": "earliest_charge_time",
                "default": dt.time(22, 0),
                "name": "Earliest charge time",
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerTimeEntity(
            hass,
            {
                "id": "earliest_discharge_time",
                "default": dt.time(6, 0),
                "name": "Earliest discharge time",
                "enabled": True,
                "data_store": "config",
            },
        ),
    ]

    hass.data[DOMAIN][TIME_ENTITIES] = times

    for time in times:
        if hass.data[DOMAIN][time.data_store].get(time.id) is None:
            hass.data[DOMAIN][time.data_store][time.id] = time.native_value
    async_add_devices(times)
    for time in times:
        time.update()
    # Return boolean to indicate that initialization was successful
    return True


class EnergyPlannerTimeEntity(TimeEntity):
    """Representation of a Number entity."""

    def __init__(self, hass, entity_definition):
        """Initialize the Number entity."""
        #
        # Visible Instance Attributes Outside Class
        self._hass = hass
        self.id = entity_definition["id"]

        self.entity_id = f"time.{DOMAIN}_{self.id}"
        # Hidden Inherited Instance Attributes
        self._attr_unique_id = "{}_{}".format(DOMAIN, self.id)
        self._attr_has_entity_name = True
        self._attr_name = entity_definition["name"]
        self.data_store = entity_definition.get("data_store", "values")
        self._attr_native_value = entity_definition.get("default", None)
        self._attr_assumed_state = entity_definition.get("assumed", False)
        self._attr_available = True
        self.is_added_to_hass = False
        self._attr_device_class = entity_definition.get("device_class", None)
        self._attr_icon = entity_definition.get("icon", None)
        self._attr_native_step = entity_definition.get("step", 1.0)
        self._attr_should_poll = False
        self._attr_entity_registry_enabled_default = entity_definition.get(
            "enabled", False
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        self.is_added_to_hass = True

    def update(self):
        """Update Modbus data periodically."""
        self._attr_available = True

        value = self._hass.data[DOMAIN][self.data_store].get(self.id, None)
        if type(value) is str:
            value = dt.time.fromisoformat(value)
        self._attr_native_value = value
        self.schedule_update_ha_state()

    async def async_set_value(self, value: dt.time) -> None:
        """Update the current value."""
        self._attr_native_value = value
        self._hass.data[DOMAIN][self.data_store][self.id] = value
        await self._hass.data[DOMAIN]["save"]()
        self.schedule_update_ha_state()
