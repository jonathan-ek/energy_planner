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

from .const import DOMAIN, NUMBER_ENTITIES
from .planner import settings

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the number platform."""
    _LOGGER.info("Setting up number platform")
    numbers = [
        EnergyPlannerNumberEntity(
            hass,
            {
                # Round trip: AC in to AC out, see AGENTS.md "Battery plan"
                "id": "battery_efficiency",
                "name": "Battery efficiency",
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
                # Main fuse per phase (three phases): the plan keeps all import,
                # the car included, below what it carries, 0 = not limited
                "id": "main_fuse",
                "name": "Main fuse",
                "min_val": 0,
                "max_val": 100,
                "step": 1,
                "unit_of_measurement": UnitOfElectricCurrent.AMPERE,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                # Current per phase kept free below the main fuse by the fuse guard
                # (planner/fuse.py), for loads that start before it reacts
                "id": "fuse_margin",
                "name": "Fuse margin",
                "min_val": 0,
                "max_val": 20,
                "step": 1,
                "unit_of_measurement": UnitOfElectricCurrent.AMPERE,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "grid_import_limit_start",
                "name": "Grid import limit from",
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
                # The car charger's highest power, and its lowest (0: only on/off at
                # the highest, as the automation then only starts and stops it)
                "id": "ev_charger_max_power",
                "name": "Car charger max power",
                "min_val": 1000,
                "max_val": 22000,
                "step": 100,
                "unit_of_measurement": UnitOfPower.WATT,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                "id": "ev_charger_min_power",
                "name": "Car charger min power",
                "min_val": 0,
                "max_val": 22000,
                "step": 100,
                "unit_of_measurement": UnitOfPower.WATT,
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerNumberEntity(
            hass,
            {
                # Usable battery of the car, for the need from its SOC
                "id": "ev_battery_capacity",
                "name": "Car battery capacity",
                "min_val": 10,
                "max_val": 200,
                "step": 1,
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
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
        self._attr_native_value = (
            settings.default(self.id)
            if self.data_store == "config"
            else entity_definition.get("default", None)
        )
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

        store = self._hass.data[DOMAIN][self.data_store]
        # A setting not in the store shows its default, as the planner reads it
        value = (
            settings.get(store, self.id)
            if self.data_store == "config"
            else store.get(self.id, None)
        )
        self._attr_native_value = value
        self.schedule_update_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        self._attr_native_value = value
        self._hass.data[DOMAIN][self.data_store][self.id] = value
        await self._hass.data[DOMAIN]["save"]()
        self.schedule_update_ha_state()
