import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfEnergy
from homeassistant.util import dt as dt_utils

from .const import DOMAIN, FORECAST_SENSORS, FUSE_GUARD_SENSOR, PLAN_SENSORS
from .planner.battery_plan import car_action, current_action
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
        "load_model",
        "heating_slope",
        "ev_need_kwh",
    ),
    "pv_forecast": (
        "pv_today_remaining",
        "pv_tomorrow_uncalibrated",
        "pv_calibration",
        "pv_calibration_model",
        "pv_hourly_calibration",
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

# key: (name, icon, resolve(hass) -> action). Each action has `state`, `source`,
# `until`, `reason` and `plan_updated` (planner/plan.py), and its own keys:
# - battery_action: what the inverter should do now, a slot state: slot 1, or the
#   battery plan for an `auto` slot, with the target SOC, current and import target
#   (planner/battery_action.py)
# - heat_pump_action: `on`/`off` from the battery plan, or from the saving now when
#   the plan is old (planner/heat_pump.py)
# - ev_action: `charge`/`wait` from the battery plan, `charge` without a fresh plan so
#   the car charges as without the planner, no state while unplugged; with the plan's
#   need (planner/ev.py)
ACTION_SENSORS = {
    "battery_action": (
        "Battery action",
        "mdi:battery-arrow-up-outline",
        current_action,
    ),
    "heat_pump_action": ("Heat pump action", "mdi:heat-pump", heat_pump_action),
    "ev_action": ("Car charging action", "mdi:ev-station", car_action),
}


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the sensor platform."""
    _LOGGER.info("Setting up sensor platform")
    sensors = [EnergyPlannerForecastSensor(hass, key) for key in SENSORS]
    hass.data[DOMAIN][FORECAST_SENSORS] = sensors
    plan_sensors = [
        EnergyPlannerPlanSensor(hass),
        *(EnergyPlannerHeatPumpSensor(hass, key) for key in HEAT_PUMP_SENSORS),
        *(EnergyPlannerActionSensor(hass, key) for key in ACTION_SENSORS),
    ]
    hass.data[DOMAIN][PLAN_SENSORS] = plan_sensors
    guard = EnergyPlannerFuseGuardSensor(hass)
    hass.data[DOMAIN][FUSE_GUARD_SENSOR] = guard
    async_add_devices([*sensors, *plan_sensors, guard])
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
            "peak_levels",
            "heat_pump",
            "marginal",
            "temperature",
            "heat_pump_cop",
            "heat_pump_power",
            "ev",
        }
    )

    def __init__(self, hass):
        """Initialize the plan sensor."""
        self._hass = hass
        self.entity_id = f"sensor.{DOMAIN}_battery_plan"
        self._attr_unique_id = f"{DOMAIN}_battery_plan"

    @property
    def native_value(self):
        """Return the planned mode for the current quarter, None if not covered."""
        plan = self._hass.data[DOMAIN].get("plan")
        return plan.value_at(dt_utils.now(), plan.modes) if plan is not None else None

    @property
    def extra_state_attributes(self):
        """Return the plan."""
        plan = self._hass.data[DOMAIN].get("plan")
        return plan.to_attributes() if plan is not None else None


class EnergyPlannerActionSensor(SensorEntity):
    """What a device should do now (`ACTION_SENSORS`): the state and its details."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass, key):
        """Initialize the action sensor."""
        self._hass = hass
        self._attr_name, self._attr_icon, self._resolve = ACTION_SENSORS[key]
        self.entity_id = f"sensor.{DOMAIN}_{key}"
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._action: dict = {}

    def resolve(self) -> None:
        """Resolve the action now; write_plan_sensors calls it before each write."""
        self._action = self._resolve(self._hass)

    async def async_added_to_hass(self) -> None:
        """Resolve the action when the sensor is added."""
        self.resolve()

    @property
    def native_value(self):
        """Return what to do now."""
        return self._action.get("state")

    @property
    def extra_state_attributes(self):
        """Return where it comes from, until when, and the device's details."""
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


class EnergyPlannerFuseGuardSensor(SensorEntity):
    """Room on the busiest phase below the main fuse (A), and the limits to apply.

    Attributes: `grid_limit_kw` (the inverter's max usable grid power) and
    `ev_current_a` (the charger's current), the currents per phase. No state without
    a fuse or phase sensors. See planner/fuse.py.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Fuse guard"
    _attr_icon = "mdi:fuse"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, hass):
        """Initialize the fuse guard sensor."""
        self._hass = hass
        self.entity_id = f"sensor.{DOMAIN}_fuse_guard"
        self._attr_unique_id = f"{DOMAIN}_fuse_guard"

    @property
    def native_value(self):
        """Return the room on the busiest phase (A, negative: over)."""
        guard = self._hass.data[DOMAIN].get("fuse_guard")
        return guard["room_a"] if guard else None

    @property
    def extra_state_attributes(self):
        """Return the limits and the currents per phase."""
        guard = self._hass.data[DOMAIN].get("fuse_guard")
        if not guard:
            return None
        return {key: value for key, value in guard.items() if key != "room_a"}
