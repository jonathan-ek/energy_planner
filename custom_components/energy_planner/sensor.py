import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy

from .const import DOMAIN, FORECAST_SENSORS, PLAN_SENSORS
from .planner.battery_plan import current_action

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


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the sensor platform."""
    _LOGGER.info("Setting up sensor platform")
    sensors = [EnergyPlannerForecastSensor(hass, key) for key in SENSORS]
    hass.data[DOMAIN][FORECAST_SENSORS] = sensors
    plan_sensors = [EnergyPlannerPlanSensor(hass), EnergyPlannerActionSensor(hass)]
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
        {"starts", "modes", "soc", "grid_import", "grid_export", "targets"}
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
