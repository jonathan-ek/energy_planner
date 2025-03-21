import datetime
import logging

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_utils

from custom_components.energy_planner.const import DOMAIN, DATE_TIME_ENTITIES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the datetime platform."""
    _LOGGER.info("Setting up datetime platform")
    datetimes = [
        EnergyPlannerDateTimeEntity(
            hass,
            {
                "id": f"slot_{i}_date_time_start",
                "name": f"Slot {i} start",
                "enabled": True,
            },
        )
        for i in range(1, 50)
    ]

    hass.data[DOMAIN][DATE_TIME_ENTITIES] = datetimes
    async_add_devices(datetimes)
    for entity in datetimes:
        entity.update()
    # Return boolean to indicate that initialization was successful
    return True


class EnergyPlannerDateTimeEntity(DateTimeEntity):
    """Representation of a DateTime entity."""

    def __init__(self, hass, entity_definition):
        """Initialize the DateTime entity."""
        self._hass = hass
        self.id = entity_definition["id"]
        self.entity_id = f"datetime.{DOMAIN}_{self.id}"
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
        """Update data."""
        self._attr_available = True

        value = self._hass.data[DOMAIN][self.data_store].get(self.id, None)
        if type(value) is str:
            value = dt_utils.parse_datetime(value)
        self._attr_native_value = value
        self.schedule_update_ha_state()

    async def async_set_value(self, value: datetime.datetime) -> None:
        """Update the current value."""
        self._attr_native_value = value
        self._hass.data[DOMAIN][self.data_store][self.id] = value
        await self._hass.data[DOMAIN]["save"]()
        self.schedule_update_ha_state()

    def _stringify_state(self, available: bool) -> str:
        """Return the state as a string."""
        if self._attr_native_value is None:
            return "unknown"
        val = self._attr_native_value
        return f"{val.day}/{val.month} {val.strftime('%H:%M')}"
