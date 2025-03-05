import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.sensor import RestoreSensor
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfEnergy, PERCENTAGE, UnitOfTime

from custom_components.energy_planner.const import DOMAIN, NUMBER_ENTITIES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    _LOGGER.info("Setting up number platform")
    numbers = [

        EnergyPlannerNumberEntity(hass, {
            "id": "basic_nr_of_charge_hours",
            "name": "Number of charge hours", "default": 4,
            "min_val": 0, "max_val": 12, "step": 0.25,
            "unit_of_measurement": UnitOfTime.HOURS, "enabled": True}),
        EnergyPlannerNumberEntity(hass, {
            "id": "basic_nr_of_discharge_hours",
            "name": "Number of discharge hours", "default": 12,
            "min_val": 0, "max_val": 24, "step": 0.25,
            "unit_of_measurement": UnitOfTime.HOURS, "enabled": True}),

        EnergyPlannerNumberEntity(hass, {
            "id": "max_charge_current",
            "name": "Max charge current", "default": 16,
            "min_val": 0, "max_val": 50, "step": 1,
            "unit_of_measurement": UnitOfElectricCurrent.AMPERE, "enabled": True}),
        EnergyPlannerNumberEntity(hass, {
            "id": "battery_capacity",
            "name": "Battery capacity", "default": 25600,
            "min_val": 0, "max_val": 1000000, "step": 1,
            "unit_of_measurement": UnitOfEnergy.WATT_HOUR, "enabled": True}),
        EnergyPlannerNumberEntity(hass, {
            "id": "battery_shutdown_soc",
            "name": "Battery shutdown SOC", "default": 20,
            "min_val": 0, "max_val": 100, "step": 1,
            "unit_of_measurement": PERCENTAGE, "enabled": True}),
    ]

    hass.data[DOMAIN][NUMBER_ENTITIES] = numbers
    for number in numbers:
        hass.data[DOMAIN]['values'][number.id] = number.native_value

    async_add_devices(numbers, True)
    # Return boolean to indicate that initialization was successful
    return True


class EnergyPlannerNumberEntity(RestoreSensor, NumberEntity):
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
        self._attr_native_value = entity_definition.get("default", None)
        self._attr_assumed_state = entity_definition.get("assumed", False)
        self._attr_available = True
        self.is_added_to_hass = False
        self._attr_device_class = entity_definition.get("device_class", None)
        self._attr_icon = entity_definition.get("icon", None)
        self._attr_mode = entity_definition.get("mode", NumberMode.AUTO)
        self._attr_native_unit_of_measurement = entity_definition.get("unit_of_measurement", None)
        self._attr_native_min_value = entity_definition.get("min_val", None)
        self._attr_native_max_value = entity_definition.get("max_val", None)
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
        self.schedule_update_ha_state()
        self._attr_native_value = value

    def set_value(self, value: float) -> None:
        """Update the current value."""
        self._attr_native_value = value
        self._hass.data[DOMAIN]['values'][self.id] = value
        self.schedule_update_ha_state()
        self.async_write_ha_state()