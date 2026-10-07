# Description: python_script that makes a Solis inverter follow
#              sensor.energy_planner_battery_action (a manual slot, a planner slot or,
#              for an `auto` slot, the battery plan).
#
# Unlike update_battery_action.py it only writes a setting when the inverter has a
# different value, so running it every minute does not wear the inverter's memory or
# flood Modbus. It uses time-of-use slot 6 for charging and discharging, so slot 1
# stays free for the "Peak +/-" automations.
#
# The time windows start at 00:00 and end when the action ends (or 23:59), so they
# only change when the action does. Fallbacks to self-use have hysteresis, so the
# inverter does not switch back and forth around a threshold.
#
# Not done yet: `pause` (the plan's `hold`) keeps the battery but falls back to
# self-use while the grid import is above the import target; charging at the import
# target is approximated by the planned current. The inverter's peak shaving
# (number.solis_s6_eh1p_peak_max_usable_grid_power) may do both better.

# DO NOT COPY the following section, it is only used to prevent errors in the IDE
from homeassistant.core import HomeAssistant

hass: HomeAssistant = HomeAssistant(".")
logger = None
dt_util = None

# ---------------------------- Start of the actual code ----------------------------

ACTION = "sensor.energy_planner_battery_action"

CHARGE_CURRENT = "number.solis_s6_eh1p_grid_time_of_use_charge_battery_current_slot_6"
CHARGE_SOC = "number.solis_s6_eh1p_grid_time_of_use_charge_cut_off_soc_slot_6"
CHARGE_START = "time.solis_s6_eh1p_grid_time_of_use_charge_start_slot_6"
CHARGE_END = "time.solis_s6_eh1p_grid_time_of_use_charge_end_slot_6"
DISCHARGE_CURRENT = (
    "number.solis_s6_eh1p_grid_time_of_use_discharge_battery_current_slot_6"
)
DISCHARGE_SOC = "number.solis_s6_eh1p_grid_time_of_use_discharge_cut_off_soc_slot_6"
DISCHARGE_START = "time.solis_s6_eh1p_grid_time_of_use_discharge_start_slot_6"
DISCHARGE_END = "time.solis_s6_eh1p_grid_time_of_use_discharge_end_slot_6"
FEED_IN_LIMIT = "switch.solis_s6_eh1p_grid_feed_in_power_limit_switch"

BATTERY_SOC = "sensor.solis_s6_solis_battery_soc"
GRID_POWER = "sensor.solis_s6_solis_meter_total_active_power"  # + sell, - buy (W)
PV_POWER = "sensor.solis_s6_solis_total_pv_power"  # W
HOUSE_POWER = "sensor.solis_s6_solis_household_load_power"  # W

OFF = "00:00"
# W of margin before leaving or returning to a mode after a fallback
HYSTERESIS = 300


def get_state(entity_id):
    """Return an entity's state, raising if it does not exist."""
    state = hass.states.get(entity_id)
    if state is None:
        raise ValueError(f"State for {entity_id} not found")
    return state.state


def get_float(entity_id, default=0.0):
    """Return an entity's state as a float, or `default`."""
    try:
        return float(get_state(entity_id))
    except ValueError:
        return default


def set_number(entity_id, value):
    """Set a number entity if it has another value."""
    if abs(get_float(entity_id, -1000.0) - value) < 0.01:
        return
    logger.info("Setting %s to %s", entity_id, value)
    hass.services.call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, False
    )


def set_time(entity_id, value):
    """Set a time entity (HH:MM) if it has another value."""
    if str(get_state(entity_id))[:5] == value:
        return
    logger.info("Setting %s to %s", entity_id, value)
    hass.services.call(
        "time", "set_value", {"entity_id": entity_id, "time": value + ":00"}, False
    )


def set_switch(entity_id, on):
    """Turn a switch on or off if it is not already."""
    if (get_state(entity_id) == "on") == on:
        return
    logger.info("Turning %s %s", entity_id, "on" if on else "off")
    hass.services.call(
        "switch", "turn_on" if on else "turn_off", {"entity_id": entity_id}, False
    )


def window_end(until):
    """Return HH:MM for the end of the action, 23:59 if it ends after today."""
    now = dt_util.now()
    end = dt_util.parse_datetime(until) if until else None
    if end is None or dt_util.as_local(end).date() != now.date():
        return "23:59"
    end = dt_util.as_local(end)
    return "%02d:%02d" % (end.hour, end.minute)


action = hass.states.get(ACTION)
state = action.state if action else "discharge"
attributes = action.attributes if action else {}
soc = attributes.get("soc")
current = attributes.get("current_a") or 0
end = window_end(attributes.get("until"))

charging = get_state(CHARGE_END)[:5] != OFF
discharging = get_state(DISCHARGE_END)[:5] != OFF
grid_import = -get_float(GRID_POWER)
battery_soc = get_float(BATTERY_SOC)

charge = None  # (current, soc) for the charge window, None: off
discharge = None  # (current, soc) for the discharge window, None: off

if state == "charge" and soc is not None:
    charge = (current, soc)
elif state == "pause":
    # Keep the battery, but let it cover the load above the import target
    target = attributes.get("import_target_kw")
    limit = None if target is None else target * 1000
    if limit is None or grid_import < limit - (HYSTERESIS if not charging else 0):
        charge = (0, soc if soc is not None else 100)
elif state == "sell" and soc is not None:
    # Self-use once the battery is down to the target
    if battery_soc > soc:
        discharge = (current, soc)
elif state == "sell-excess" and soc is not None:
    # Self-use while the PV does not cover the house
    surplus = get_float(PV_POWER) - get_float(HOUSE_POWER)
    if surplus > (0 if discharging else HYSTERESIS):
        discharge = (0, soc)

if charge is not None:
    set_number(CHARGE_CURRENT, charge[0])
    set_number(CHARGE_SOC, charge[1])
    set_time(CHARGE_START, OFF)
    set_time(CHARGE_END, end)
else:
    set_time(CHARGE_START, OFF)
    set_time(CHARGE_END, OFF)

if discharge is not None:
    set_number(DISCHARGE_CURRENT, discharge[0])
    set_number(DISCHARGE_SOC, discharge[1])
    set_time(DISCHARGE_START, OFF)
    set_time(DISCHARGE_END, end)
else:
    set_time(DISCHARGE_START, OFF)
    set_time(DISCHARGE_END, OFF)

set_switch(FEED_IN_LIMIT, state == "discard-excess")
