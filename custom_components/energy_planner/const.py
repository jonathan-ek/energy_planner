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

# Tariff options besides the presets in planner/tariff.py: the flat network_cost /
# network_compensation settings, or a custom tariff entered as YAML
TARIFF_FLAT = "flat"
TARIFF_CUSTOM = "custom"

# Statistics used by the forecast, can be overridden in the config store
DEFAULT_FORECAST_LOAD_SENSOR = "sensor.solis_s6_solis_household_load_power"
DEFAULT_FORECAST_PV_SENSOR = "sensor.solis_s6_solis_total_pv_power"
DEFAULT_FORECAST_EV_SENSOR = "sensor.ehwuhqtp_effekt"
# Local calendar with planned loads (sauna, laundry, EV charging, ...)
DEFAULT_FORECAST_CALENDAR = "calendar.energiplan"

# Inputs of the battery plan, can be overridden in the config store
DEFAULT_BATTERY_SOC_SENSOR = "sensor.solis_s6_solis_battery_soc"
DEFAULT_BATTERY_VOLTAGE_SENSOR = "sensor.solis_s6_solis_battery_voltage"
# Energy counter of the grid import (here the electricity meter's HAN port); its
# hourly changes this month give the power peaks already paid for
DEFAULT_GRID_IMPORT_SENSOR = "sensor.matarstallning_aktiv_energi_uttag"
PLAN_SENSORS = "plan_sensors"

# Heat pump economy (planner/heat_pump.py), can be overridden in the config store
DEFAULT_OUTDOOR_TEMPERATURE_SENSOR = "sensor.gw1100a_outdoor_temperature"
DEFAULT_HEAT_PUMP_MODEL = "msz_ap42"
DEFAULT_DISTRICT_HEATING = "tekniska_verken_2026"
# Energy counter of the heat pump: removed from the load history, the battery plan adds
# the heat pump where it plans it
DEFAULT_HEAT_PUMP_ENERGY_SENSOR = "sensor.hallen_energy"
