import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy

from .const import DOMAIN, FORECAST_SENSORS, PLAN_SENSORS
from .planner.battery_plan import current_action
from .planner.heating import heat_pump_action, heat_pump_economy

_LOGGER = logging.getLogger(__name__)

# key: (name, state key in the forecast, per-quarter series in the attributes)
SENSORS = {
    "load_forecast": (
        "Load forecast tomorrow",
        "load_tomorrow",
        ("load", "planned", "reserve", "yesterday_starts", "yesterday_load"),
    ),
    "pv_forecast": (
        "PV forecast tomorrow",
        "pv_tomorrow",
        ("pv", "yesterday_starts", "yesterday_pv"),
    ),
}
EXTRA_ATTRIBUTES = {
    "load_forecast": (
        "load_today_remaining",
        "base_tomorrow",
        "planned_tomorrow",
        "planned_events",
        "yesterday_load_kwh",
        "yesterday_load_actual_kwh",
        "heat_pump_loss",
        "heat_pump_loss_hours",
    ),
    "pv_forecast": (
        "pv_today_remaining",
        "pv_tomorrow_uncalibrated",
        "pv_calibration",
        "pv_source",
        "yesterday_pv_kwh",
        "yesterday_pv_actual_kwh",
    ),
}

# key: (name, key in heat_pump_economy, unit, icon); the saving sensor has the details
HEAT_PUMP_SENSORS = {
    "heat_pump_cop": ("Heat pump COP", "cop", None, "mdi:heat-pump-outline"),
    "district_heating_price": (
        "District heating price",
        "district_heating_price",
        "öre/kWh",
        "mdi:radiator",
    ),
    "heat_pump_saving": (
        "Heat pump saving",
        "saving",
        "öre/kWh",
        "mdi:cash-plus",
    ),
}


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the sensor platform."""
    _LOGGER.info("Setting up sensor platform")
    sensors = [EnergyPlannerForecastSensor(hass, key) for key in SENSORS]
    hass.data[DOMAIN][FORECAST_SENSORS] = sensors
    plan_sensors = [
        EnergyPlannerPlanSensor(hass),
        EnergyPlannerActionSensor(hass),
        *(EnergyPlannerHeatPumpSensor(hass, key) for key in HEAT_PUMP_SENSORS),
        EnergyPlannerHeatPumpActionSensor(hass),
    ]
    hass.data[DOMAIN][PLAN_SENSORS] = plan_sensors
    async_add_devices([*sensors, *plan_sensors])
    return True


class EnergyPlannerForecastSensor(SensorEntity):
    """Forecast for tomorrow, with the per-quarter forecast in the attributes."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_has_entity_name = True
    _attr_should_poll = False
    # The per-quarter lists are large and change hourly, keep them out of the database
    _unrecorded_attributes = frozenset(
        {
            "starts",
            "load",
            "planned",
            "reserve",
            "pv",
            "planned_events",
            "yesterday_starts",
            "yesterday_load",
            "yesterday_pv",
        }
    )

    def __init__(self, hass, key):
        """Initialize the forecast sensor."""
        self._hass = hass
        self._key = key
        self._attr_name, self._state_key, self._series = SENSORS[key]
        self.entity_id = f"sensor.{DOMAIN}_{key}"
        self._attr_unique_id = f"{DOMAIN}_{key}"

    @property
    def _forecast(self):
        return self._hass.data[DOMAIN].get("forecast", {})

    @property
    def native_value(self):
        """Return tomorrow's total."""
        return self._forecast.get(self._state_key)

    @property
    def extra_state_attributes(self):
        """Return the per-quarter forecast."""
        forecast = self._forecast
        if not forecast:
            return None
        attributes = {"updated": forecast["updated"], "starts": forecast["starts"]}
        for key in (*self._series, *EXTRA_ATTRIBUTES[self._key]):
            attributes[key] = forecast.get(key)
        return attributes


class EnergyPlannerPlanSensor(SensorEntity):
    """The battery plan (dry run): the mode now, the plan per quarter in attributes."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Battery plan"
    _unrecorded_attributes = frozenset(
        {
            "starts",
            "modes",
            "prices",
            "estimated",
            "soc",
            "grid_import",
            "grid_export",
            "targets",
            "heat_pump",
            "marginal",
            "temperature",
            "heat_pump_cop",
            "heat_pump_power",
        }
    )

    def __init__(self, hass):
        """Initialize the plan sensor."""
        self._hass = hass
        self.entity_id = f"sensor.{DOMAIN}_battery_plan"
        self._attr_unique_id = f"{DOMAIN}_battery_plan"

    @property
    def native_value(self):
        """Return the planned mode for the current quarter."""
        return self._hass.data[DOMAIN].get("plan", {}).get("mode")

    @property
    def extra_state_attributes(self):
        """Return the plan."""
        plan = self._hass.data[DOMAIN].get("plan")
        if not plan:
            return None
        return {key: value for key, value in plan.items() if key != "mode"}


class EnergyPlannerActionSensor(SensorEntity):
    """What the battery should do now: slot 1, or the battery plan for `auto`.

    The state is a slot state; the attributes say where it comes from and what the
    inverter needs (target SOC, current, import target, until). See
    planner/battery_action.py.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Battery action"
    _attr_icon = "mdi:battery-arrow-up-outline"

    def __init__(self, hass):
        """Initialize the action sensor."""
        self._hass = hass
        self.entity_id = f"sensor.{DOMAIN}_battery_action"
        self._attr_unique_id = f"{DOMAIN}_battery_action"
        self._action: dict = {}

    def resolve(self) -> None:
        """Resolve the action now; write_plan_sensors calls it before each write."""
        self._action = current_action(self._hass)

    async def async_added_to_hass(self) -> None:
        """Resolve the action when the sensor is added."""
        self.resolve()

    @property
    def native_value(self):
        """Return the slot state to run now."""
        return self._action.get("state")

    @property
    def extra_state_attributes(self):
        """Return the details of the action."""
        return {key: value for key, value in self._action.items() if key != "state"}


class EnergyPlannerHeatPumpSensor(SensorEntity):
    """Heat pump economy now: COP, district heating price, or the saving per kWh heat.

    The saving is district heating minus heat pump heat (öre per kWh of heat), so the
    heat pump pays when it is positive. See planner/heat_pump.py.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass, key):
        """Initialize the heat pump sensor."""
        self._hass = hass
        self._key = key
        name, self._value_key, unit, icon = HEAT_PUMP_SENSORS[key]
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self.entity_id = f"sensor.{DOMAIN}_{key}"
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._economy: dict = {}

    def resolve(self) -> None:
        """Recalculate; write_plan_sensors calls it before each write."""
        self._economy = heat_pump_economy(self._hass)

    async def async_added_to_hass(self) -> None:
        """Calculate when the sensor is added."""
        self.resolve()

    @property
    def native_value(self):
        """Return this sensor's value."""
        return self._economy.get(self._value_key)

    @property
    def extra_state_attributes(self):
        """Return the inputs and the other results (all on the saving sensor)."""
        if self._key == "heat_pump_saving":
            return {k: v for k, v in self._economy.items() if k != "saving"}
        if self._key == "heat_pump_cop":
            return {"outdoor_temperature": self._economy.get("outdoor_temperature")}
        return None


class EnergyPlannerHeatPumpActionSensor(SensorEntity):
    """Whether the heat pump should heat now (`on`/`off`).

    From the battery plan, or from the saving now when the plan is old (`source`).
    See planner/heating.py.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Heat pump action"
    _attr_icon = "mdi:heat-pump"

    def __init__(self, hass):
        """Initialize the heat pump action sensor."""
        self._hass = hass
        self.entity_id = f"sensor.{DOMAIN}_heat_pump_action"
        self._attr_unique_id = f"{DOMAIN}_heat_pump_action"
        self._action: dict = {}

    def resolve(self) -> None:
        """Resolve the action now; write_plan_sensors calls it before each write."""
        self._action = heat_pump_action(self._hass)

    async def async_added_to_hass(self) -> None:
        """Resolve the action when the sensor is added."""
        self.resolve()

    @property
    def native_value(self):
        """Return on or off."""
        return self._action.get("state")

    @property
    def extra_state_attributes(self):
        """Return where it comes from, until when, the electricity cost and COP."""
        return {key: value for key, value in self._action.items() if key != "state"}
