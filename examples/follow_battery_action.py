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
# When the action has an import target (the battery plan's `charge` and `hold`), the
# inverter's peak shaving does the work: grid import up to the max usable grid power
# charges the battery up to the baseline SOC, and the battery covers the load above
# it. Peak shaving only works while no time-of-use window is active, so the windows
# are turned off first. A manual `charge` or `pause` slot has no import target and
# uses the time-of-use windows instead. The "Peak +/-" automations (slot 1) should be
# off while this runs, peak shaving replaces them.

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
PEAK_SHAVING = "switch.solis_s6_eh1p_peak_shaving_mode"
PEAK_GRID_POWER = "number.solis_s6_eh1p_peak_max_usable_grid_power"  # W
PEAK_BASELINE_SOC = "number.solis_s6_eh1p_peak_baseline_soc"  # %

BATTERY_SOC = "sensor.solis_s6_solis_battery_soc"
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
    """Set a number entity, within its range, if it has another value."""
    state = hass.states.get(entity_id)
    if state is not None:
        value = max(value, state.attributes.get("min", value))
        value = min(value, state.attributes.get("max", value))
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

discharging = get_state(DISCHARGE_END)[:5] != OFF
battery_soc = get_float(BATTERY_SOC)

charge = None  # (current, soc) for the charge window, None: off
discharge = None  # (current, soc) for the discharge window, None: off
peak = None  # (max grid power W, baseline soc) for peak shaving, None: off
target = attributes.get("import_target_kw")
grid_limit = None if target is None else int(round(target * 10)) * 100  # W

if state == "charge" and soc is not None:
    if grid_limit is not None:
        # Charge from PV and the grid up to the import target
        peak = (grid_limit, soc)
    else:
        charge = (current, soc)
elif state == "pause":
    if grid_limit is not None:
        # Keep the battery, it only covers the load above the import target. The
        # baseline is not raised above the battery's SOC, so it does not charge from
        # the grid; it is only lowered when it is above it.
        baseline = int(get_float(PEAK_BASELINE_SOC, 100.0))
        if baseline > battery_soc:
            baseline = int(battery_soc)
        peak = (grid_limit, baseline)
    else:
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

# Turn off what is not used first, so two modes are never active together
if charge is None:
    set_time(CHARGE_START, OFF)
    set_time(CHARGE_END, OFF)
if discharge is None:
    set_time(DISCHARGE_START, OFF)
    set_time(DISCHARGE_END, OFF)
if peak is None:
    set_switch(PEAK_SHAVING, False)

if charge is not None:
    set_number(CHARGE_CURRENT, charge[0])
    set_number(CHARGE_SOC, charge[1])
    set_time(CHARGE_START, OFF)
    set_time(CHARGE_END, end)
if discharge is not None:
    set_number(DISCHARGE_CURRENT, discharge[0])
    set_number(DISCHARGE_SOC, discharge[1])
    set_time(DISCHARGE_START, OFF)
    set_time(DISCHARGE_END, end)
if peak is not None:
    set_number(PEAK_GRID_POWER, peak[0])
    set_number(PEAK_BASELINE_SOC, peak[1])
    set_switch(PEAK_SHAVING, True)

# Curtail the PV surplus instead of exporting it (negative export price)
set_switch(FEED_IN_LIMIT, state == "discard-excess")
