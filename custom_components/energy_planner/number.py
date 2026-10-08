import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    PERCENTAGE,
    UnitOfTime,
)
from homeassistant.components.sensor.const import (
    SensorDeviceClass,
    SensorStateClass,
)

from .const import DOMAIN, SLOT_COUNT, NUMBER_ENTITIES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the number platform."""
    _LOGGER.info("Setting up number platform")
    numbers = [
        *[
            EnergyPlannerNumberEntity(
                hass,
                {
                    "id": f"slot_{i}_soc",
                    "name": f"Slot {i} soc",
                    "default": 50,
                    "min_val": 0,
                    "max_val": 100,
                    "step": 1,
                    "unit_of_measurement": PERCENTAGE,
                    "enabled": True,
                    "data_store": "values",
                },
            )
            for i in range(1, SLOT_COUNT + 1)
        ],
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "basic_nr_of_charge_hours",
                "name": "Number of charge hours",
                "default": 4,
                "min_val": 0,
                "max_val": 12,
                "step": 0.25,
                "unit_of_measurement": UnitOfTime.HOURS,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "basic_nr_of_discharge_hours",
                "name": "Number of discharge hours",
                "default": 12,
                "min_val": 0,
                "max_val": 24,
                "step": 0.25,
                "unit_of_measurement": UnitOfTime.HOURS,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "cheapest_hours_nr_of_charge_hours",
                "name": "Number of charge hours",
                "default": 2,
                "min_val": 0,
                "max_val": 24,
                "step": 0.25,
                "unit_of_measurement": UnitOfTime.HOURS,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "price_peak_nr_of_charge_hours",
                "name": "Number of charge hours",
                "default": 2,
                "min_val": 0,
                "max_val": 24,
                "step": 0.25,
                "unit_of_measurement": UnitOfTime.HOURS,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "price_peak_nr_of_discharge_hours",
                "name": "Number of discharge hours",
                "default": 2,
                "min_val": 0,
                "max_val": 24,
                "step": 0.25,
                "unit_of_measurement": UnitOfTime.HOURS,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "price_peak_efficiency_factor",
                "name": "Inverter efficiency factor (%)",
                "default": 85,
                "min_val": 0,
                "max_val": 100,
                "step": 1,
                "unit_of_measurement": PERCENTAGE,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "max_charge_current",
                "name": "Max charge current",
                "default": 16,
                "min_val": 0,
                "max_val": 50,
                "step": 1,
                "unit_of_measurement": UnitOfElectricCurrent.AMPERE,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "max_discharge_current",
                "name": "Max discharge current",
                "default": 16,
                "min_val": 0,
                "max_val": 50,
                "step": 1,
                "unit_of_measurement": UnitOfElectricCurrent.AMPERE,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "battery_capacity",
                "name": "Battery capacity",
                "default": 25600,
                "min_val": 0,
                "max_val": 1000000,
                "step": 1,
                "unit_of_measurement": UnitOfEnergy.WATT_HOUR,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "battery_shutdown_soc",
                "name": "Battery shutdown SOC",
                "default": 20,
                "min_val": 0,
                "max_val": 100,
                "step": 1,
                "unit_of_measurement": PERCENTAGE,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                # The battery plan does not discharge below it; the inverter may,
                # down to the shutdown SOC, to shave peaks from unexpected load.
                # At or below the shutdown SOC: off
                "id": "battery_reserve_soc",
                "name": "Battery reserve SOC",
                "default": 20,
                "min_val": 0,
                "max_val": 100,
                "step": 1,
                "unit_of_measurement": PERCENTAGE,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                # Extra energy to keep for unplanned weekend load, Sat/Sun 16-20
                "id": "forecast_weekend_reserve",
                "name": "Weekend reserve",
                "default": 4,
                "min_val": 0,
                "max_val": 20,
                "step": 0.5,
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "forecast_reserve_start",
                "name": "Weekend reserve from",
                "default": 18,
                "min_val": 0,
                "max_val": 23,
                "step": 1,
                "unit_of_measurement": UnitOfTime.HOURS,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "forecast_reserve_end",
                "name": "Weekend reserve until",
                "default": 22,
                "min_val": 1,
                "max_val": 24,
                "step": 1,
                "unit_of_measurement": UnitOfTime.HOURS,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "battery_max_soc",
                "name": "Battery max SOC",
                "default": 90,
                "min_val": 0,
                "max_val": 100,
                "step": 1,
                "unit_of_measurement": PERCENTAGE,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "network_compensation",
                "name": "Network compensation when selling",
                "default": 0,
                "min_val": 0,
                "max_val": 1000,
                "step": 0.1,
                "unit_of_measurement": "öre/kwh",
                "device_class": SensorDeviceClass.MONETARY,
                "state_class": SensorStateClass.TOTAL,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "network_cost",
                "name": "Network cost when buying",
                "default": 0,
                "min_val": 0,
                "max_val": 1000,
                "step": 0.1,
                "unit_of_measurement": "öre/kwh",
                "device_class": SensorDeviceClass.MONETARY,
                "state_class": SensorStateClass.TOTAL,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                # Battery wear per kWh taken out, so the plan only cycles the
                # battery when the price difference pays for it
                "id": "battery_wear_cost",
                "name": "Battery wear cost",
                "default": 20,
                "min_val": 0,
                "max_val": 200,
                "step": 1,
                "unit_of_measurement": "öre/kwh",
                "device_class": SensorDeviceClass.MONETARY,
                "state_class": SensorStateClass.TOTAL,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                # Highest hourly mean grid import the planner may cause, 0 = no limit
                "id": "grid_import_limit",
                "name": "Grid import limit",
                "default": 0,
                "min_val": 0,
                "max_val": 50,
                "step": 0.1,
                "unit_of_measurement": UnitOfPower.KILO_WATT,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "grid_import_limit_start",
                "name": "Grid import limit from",
                "default": 6,
                "min_val": 0,
                "max_val": 23,
                "step": 1,
                "unit_of_measurement": UnitOfTime.HOURS,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "grid_import_limit_end",
                "name": "Grid import limit until",
                "default": 23,
                "min_val": 0,
                "max_val": 24,
                "step": 1,
                "unit_of_measurement": UnitOfTime.HOURS,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                # What the heat pump draws while it heats, until the forecast has
                # measured it from the house load; the battery plan adds it to the load
                # when it runs. 0 = do not plan the heat pump
                "id": "heat_pump_power",
                "name": "Heat pump power",
                "default": 300,
                "min_val": 0,
                "max_val": 3000,
                "step": 50,
                "unit_of_measurement": UnitOfPower.WATT,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                # The heat pump only runs when its heat is at least this much cheaper
                # than district heating (öre per kWh of heat)
                "id": "heat_pump_margin",
                "name": "Heat pump margin",
                "default": 5,
                "min_val": 0,
                "max_val": 100,
                "step": 1,
                "unit_of_measurement": "öre/kwh",
                "device_class": SensorDeviceClass.MONETARY,
                "state_class": SensorStateClass.TOTAL,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                # Heat is only needed (and saves district heating) below this outdoor
                # temperature
                "id": "heat_pump_heating_limit",
                "name": "Heat pump heating limit",
                "default": 15,
                "min_val": -10,
                "max_val": 25,
                "step": 0.5,
                "unit_of_measurement": UnitOfTemperature.CELSIUS,
                "enabled": True,
                "data_store": "config",
            },
        ),
    ]

    hass.data[DOMAIN][NUMBER_ENTITIES] = numbers
    for number in numbers:
        if hass.data[DOMAIN][number.data_store].get(number.id) is None:
            hass.data[DOMAIN][number.data_store][number.id] = number.native_value
    async_add_devices(numbers)
    for number in numbers:
        number.update()

    # Return boolean to indicate that initialization was successful
    return True


class EnergyPlannerNumberEntity(NumberEntity):
    """Representation of a Number entity."""

    def __init__(self, hass, entity_definition):
        """Initialize the Number entity."""
        #
        # Visible Instance Attributes Outside Class
        self._hass = hass
        self.id = entity_definition["id"]
        # Hidden Inherited Instance Attributes
        self._attr_unique_id = "{}_{}".format(DOMAIN, self.id)
        self.entity_id = f"number.{DOMAIN}_{self.id}"
        self._attr_has_entity_name = True
        self._attr_name = entity_definition["name"]
        self.data_store = entity_definition.get("data_store", "values")
        self._attr_native_value = entity_definition.get("default", None)
        self._attr_assumed_state = entity_definition.get("assumed", False)
        self._attr_available = True
        self.is_added_to_hass = False
        self._attr_device_class = entity_definition.get("device_class", None)
        self._attr_state_class = entity_definition.get("state_class", None)
        self._attr_icon = entity_definition.get("icon", None)
        self._attr_mode = entity_definition.get("mode", NumberMode.AUTO)
        self._attr_native_unit_of_measurement = entity_definition.get(
            "unit_of_measurement", None
        )
        self._attr_native_min_value = entity_definition.get("min_val", None)
        self._attr_native_max_value = entity_definition.get("max_val", None)
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
        self._attr_native_value = value
        self.schedule_update_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        self._attr_native_value = value
        self._hass.data[DOMAIN][self.data_store][self.id] = value
        await self._hass.data[DOMAIN]["save"]()
        self.schedule_update_ha_state()
