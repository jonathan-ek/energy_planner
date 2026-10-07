import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy

from .const import DOMAIN, FORECAST_SENSORS

_LOGGER = logging.getLogger(__name__)

# key: (name, state key in the forecast, per-quarter series in the attributes)
SENSORS = {
    "load_forecast": (
        "Load forecast tomorrow",
        "load_tomorrow",
        ("load", "planned", "reserve"),
    ),
    "pv_forecast": ("PV forecast tomorrow", "pv_tomorrow", ("pv",)),
}
EXTRA_ATTRIBUTES = {
    "load_forecast": (
        "load_today_remaining",
        "base_tomorrow",
        "planned_tomorrow",
        "planned_events",
    ),
    "pv_forecast": (
        "pv_today_remaining",
        "pv_tomorrow_uncalibrated",
        "pv_calibration",
        "pv_source",
    ),
}


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the sensor platform."""
    _LOGGER.info("Setting up sensor platform")
    sensors = [EnergyPlannerForecastSensor(hass, key) for key in SENSORS]
    hass.data[DOMAIN][FORECAST_SENSORS] = sensors
    async_add_devices(sensors)
    return True


class EnergyPlannerForecastSensor(SensorEntity):
    """Forecast for tomorrow, with the per-quarter forecast in the attributes."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_has_entity_name = True
    _attr_should_poll = False
    # The per-quarter lists are large and change hourly, keep them out of the database
    _unrecorded_attributes = frozenset(
        {"starts", "load", "planned", "reserve", "pv", "planned_events"}
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
