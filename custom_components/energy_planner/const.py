DOMAIN = "energy_planner"

NUMBER_ENTITIES = "number_entities"
SWITCH_ENTITIES = "switch_entities"
DATE_TIME_ENTITIES = "date_time_entities"
TIME_ENTITIES = "time_entities"
SELECT_ENTITIES = "select_entities"
FORECAST_SENSORS = "forecast_sensors"

VERSION_STORAGE = "1"

# Number of slots in the schedule, numbered 1..SLOT_COUNT
SLOT_COUNT = 49

# Statistics used by the forecast, can be overridden in the config store
DEFAULT_FORECAST_LOAD_SENSOR = "sensor.solis_s6_solis_household_load_power"
DEFAULT_FORECAST_PV_SENSOR = "sensor.solis_s6_solis_total_pv_power"
DEFAULT_FORECAST_EV_SENSOR = "sensor.ehwuhqtp_effekt"
# Local calendar with planned loads (sauna, laundry, EV charging, ...)
DEFAULT_FORECAST_CALENDAR = "calendar.energiplan"
