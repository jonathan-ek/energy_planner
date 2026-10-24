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
#
# With DISPATCH (Solis Remote Dispatch, firmware where register 34502 reads 0xAA55,
# solis_modbus 4.2+), every mode uses the inverter's real-time dispatch instead
# (docs/solis_remote_dispatch_2026_10.md, "Modes through dispatch"), and the inverter
# stays in its own self-use with no time-of-use window, peak shaving and the feed-in
# limit off. Dispatch is kept in RAM, the other settings are written to flash, and
# peak shaving does not limit grid charging on the S6-EH3P (19 kW with a 7.8 kW max
# usable grid power), dispatch's import limit does:
#   charge          battery_charge, import limit = the import target
#   sell            battery_discharge down to the target
#   pause (hold)    grid_import at the import target without grid charging (the
#                   battery covers the load above it), battery_hold without a target
#                   or with one above HOLD_IMPORT_MAX;
#                   in daylight battery_charge with import limit 0 (grid_import
#                   curtails the PV), storing the PV surplus, and grid_import while
#                   the house is above the PV plus the target (peak shaving)
#   sell-excess     feed_in_priority while the PV covers the house, else self-use
#   discard-excess  self-use until the battery is full, then self_consumption with
#                   export limit 0 (curtailing while the battery has room is not
#                   supported: the plan empties it before negative prices)
#   discharge       no dispatch, the inverter's self-use
# The command is repeated every minute and has a failsafe of DISPATCH_FAILSAFE
# minutes: if this script stops (HA down, the automation off), the inverter returns
# to its own mode by itself.

# DO NOT COPY the following section, it is only used to prevent errors in the IDE
from homeassistant.core import HomeAssistant

hass: HomeAssistant = HomeAssistant(".")
logger = None
dt_util = None

# ---------------------------- Start of the actual code ----------------------------

ACTION = "sensor.energy_planner_battery_action"
# Keeps the busiest phase below the main fuse (planner/fuse.py): its grid_limit_kw
# caps the import target; not used when older than GUARD_MAX_AGE seconds
FUSE_GUARD = "sensor.energy_planner_fuse_guard"
GUARD_MAX_AGE = 120

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
# The most the inverter accepts as max usable grid power (W); it keeps the old
# value when sent more (the S6-EH3P 10K: 10000). None: the entity's max
PEAK_GRID_POWER_MAX = None
# Without DISPATCH the plan's `charge` uses peak shaving up to the import target.
# The S6-EH3P does not keep to the max usable grid power while charging (see the
# top), so with False it uses the charge window at the action's current instead (the
# energy the plan charges, spread over the run): the import is then the house plus
# that current, not capped by the target (e.g. a small main fuse without dispatch)
PEAK_SHAVING_CHARGE = True
# In `hold` the baseline is kept this far below the SOC: the S6-EH3P also charges
# from the grid at the baseline (29 -> 31 % at 8.9 kW with it at 29, Oct 2026)
HOLD_SOC_MARGIN = 3
# Inverters with an enable switch for time-of-use and each window (e.g. the
# S6-EH3P: switch.time_of_use, switch.solis_s6_solis_grid_connected_charging_period_6
# and ..._discharging_period_6); None where the times alone set the window (EH1P)
TIME_OF_USE = None
CHARGE_PERIOD = None
DISCHARGE_PERIOD = None

BATTERY_SOC = "sensor.solis_s6_solis_battery_soc"
PV_POWER = "sensor.solis_s6_solis_total_pv_power"  # W
HOUSE_POWER = "sensor.solis_s6_solis_household_load_power"  # W
BATTERY_POWER = "sensor.solis_s6_solis_battery_power"  # W, without a sign
BATTERY_DIRECTION = "sensor.solis_s6_solis_battery_current_direction"  # 1: discharging

# Remote Dispatch, see the top; False: peak shaving and the time-of-use windows only
DISPATCH = False
DISPATCH_STATE = "sensor.follow_battery_dispatch"  # the last command, set here
DISPATCH_FAILSAFE = 3  # minutes
DISPATCH_REFRESH = 50  # s between repeats of an unchanged command
MAX_CHARGE_POWER = 10000  # W, the inverter and the BMS limit it further
MAX_DISCHARGE_POWER = 10000  # W, the most a `sell` asks for
# A hold's grid_import target above this discharged the battery instead, up to 9.5 kW
# to the grid with a 12.7 kW target on the S6-EH3P (Oct 2026; 0.5-7.3 kW worked), so a
# higher target (no power charge, only the fuse) uses battery_hold
HOLD_IMPORT_MAX = 10000  # W
SHAVE_MIN_POWER = 100  # W, the margin for peak shaving in a daylight hold
BATTERY_VOLTAGE = "sensor.solis_s6_solis_battery_voltage"  # V
# The lowest SOC `hold` lets the battery cover peaks down to (the plan's reserve may
# be used then, see AGENTS.md "Battery plan")
SHUTDOWN_SOC = "number.energy_planner_battery_shutdown_soc"
MAX_SOC = "number.energy_planner_battery_max_soc"  # `hold` stores PV up to it
SUN = "sun.sun"  # `hold` in daylight, see below
# `discard-excess` curtails from this SOC, until it is FULL_HYSTERESIS lower
FULL_SOC = 99
FULL_HYSTERESIS = 2

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
guard = hass.states.get(FUSE_GUARD)
guard_kw = guard.attributes.get("grid_limit_kw") if guard else None
checked = dt_util.parse_datetime(guard.attributes.get("updated") or "") if guard else None
if (
    grid_limit is not None
    and guard_kw is not None
    and checked is not None
    and (dt_util.now() - checked).total_seconds() < GUARD_MAX_AGE
):
    grid_limit = min(grid_limit, int(guard_kw * 10) * 100)

last_mode = None  # the running dispatch mode, for the hysteresis below
sent = hass.states.get(DISPATCH_STATE)
if sent is not None and sent.state == "on":
    last_mode = sent.attributes.get("mode")

dispatch = None  # the solis_dispatch call's data, None: not used
if DISPATCH and state == "charge" and soc is not None and grid_limit is not None:
    # Charge from PV and the grid with the import capped at the target; the inverter
    # stops at soc_max
    dispatch = {
        "mode": "battery_charge",
        "power_watts": MAX_CHARGE_POWER,
        "import_limit_watts": grid_limit,
        "allow_grid_charge": True,
        "soc_max": int(soc),
    }
elif DISPATCH and state == "charge" and soc is not None:
    # A manual charge slot: at its current, without an import limit
    power = MAX_CHARGE_POWER
    if current:
        power = min(current * get_float(BATTERY_VOLTAGE), power)
    dispatch = {
        "mode": "battery_charge",
        "power_watts": int(power / 100) * 100,
        "allow_grid_charge": True,
        "soc_max": int(soc),
    }
elif DISPATCH and state == "sell" and soc is not None:
    power = MAX_DISCHARGE_POWER
    if current:
        power = min(current * get_float(BATTERY_VOLTAGE), power)
    dispatch = {
        "mode": "battery_discharge",
        "power_watts": int(power / 100) * 100,
        "soc_min": int(soc),
    }
elif DISPATCH and state == "pause":
    if grid_limit is not None and get_state(SUN) == "above_horizon":
        # grid_import takes the house from the grid up to the target, then from the
        # PV, then from the battery, and curtails the PV it does not need (1.8 -> 0.1
        # kW with the house below the target, Oct 2026). battery_charge without import
        # keeps the battery and stores the PV surplus, but never discharges (4.3 kW
        # import with a 2 kW target). So grid_import only while the house is above
        # the PV plus the target, where it curtails nothing: from when it is, until
        # the battery no longer covers anything (the PV is then the part used)
        if last_mode == "grid_import":
            shaving = (
                get_state(BATTERY_DIRECTION) == "1"
                and get_float(BATTERY_POWER) > SHAVE_MIN_POWER
            )
        else:
            net = get_float(HOUSE_POWER) - get_float(PV_POWER)
            shaving = net > grid_limit + SHAVE_MIN_POWER
        if shaving and grid_limit <= HOLD_IMPORT_MAX:
            dispatch = {
                "mode": "grid_import",
                "power_watts": grid_limit,
                "allow_grid_charge": False,
                "soc_min": int(get_float(SHUTDOWN_SOC, 10.0)),
            }
        else:
            dispatch = {
                "mode": "battery_charge",
                "power_watts": MAX_CHARGE_POWER,
                "import_limit_watts": 0,
                "soc_max": int(get_float(MAX_SOC, 100.0)),
            }
    elif grid_limit is not None and grid_limit <= HOLD_IMPORT_MAX:
        # Import up to the target, the battery covers the load above it
        dispatch = {
            "mode": "grid_import",
            "power_watts": grid_limit,
            "allow_grid_charge": False,
            "soc_min": int(get_float(SHUTDOWN_SOC, 10.0)),
        }
    else:
        dispatch = {"mode": "battery_hold"}

surplus = get_float(PV_POWER) - get_float(HOUSE_POWER)
if DISPATCH and state == "sell-excess":
    # Export the PV surplus; self-use while the PV does not cover the house (in
    # feed_in_priority the grid would cover it instead of the battery)
    if surplus > (0 if last_mode == "feed_in_priority" else HYSTERESIS):
        dispatch = {"mode": "feed_in_priority"}
elif DISPATCH and state == "discard-excess":
    # Self-use fills the battery; once it is full curtail the surplus, until the
    # house takes more than the PV or the SOC drops
    curtailing = last_mode == "self_consumption"
    full = battery_soc >= (FULL_SOC - FULL_HYSTERESIS if curtailing else FULL_SOC)
    if full and surplus > (0 if curtailing else HYSTERESIS):
        dispatch = {"mode": "self_consumption", "export_limit_watts": 0}

if dispatch is not None or DISPATCH:
    pass  # all the inverter's own modes off below
elif state == "charge" and soc is not None:
    if grid_limit is not None and PEAK_SHAVING_CHARGE:
        # Charge from PV and the grid up to the import target
        peak = (grid_limit, soc)
    else:
        charge = (current, soc)
elif state == "pause":
    if grid_limit is not None:
        # Keep the battery, it only covers the load above the import target. The
        # baseline is kept HOLD_SOC_MARGIN below the battery's SOC, so it does not
        # charge from the grid; it is only lowered, not raised.
        baseline = int(get_float(PEAK_BASELINE_SOC, 100.0))
        if baseline > battery_soc - HOLD_SOC_MARGIN:
            baseline = int(battery_soc) - HOLD_SOC_MARGIN
        peak = (grid_limit, baseline)
    else:
        charge = (0, soc if soc is not None else 100)
elif state == "sell" and soc is not None:
    # Self-use once the battery is down to the target
    if battery_soc > soc:
        discharge = (current, soc)
elif state == "sell-excess" and soc is not None:
    # Self-use while the PV does not cover the house
    if surplus > (0 if discharging else HYSTERESIS):
        discharge = (0, soc)

# Turn off what is not used first, so two modes are never active together
if dispatch is None and sent is not None and sent.state == "on":
    logger.info("Stopping remote dispatch")
    hass.services.call("solis_modbus", "solis_dispatch_stop", {}, False)
    hass.states.set(DISPATCH_STATE, "off", {"friendly_name": "Battery dispatch"})
if charge is None:
    if CHARGE_PERIOD:
        set_switch(CHARGE_PERIOD, False)
    set_time(CHARGE_START, OFF)
    set_time(CHARGE_END, OFF)
if discharge is None:
    if DISCHARGE_PERIOD:
        set_switch(DISCHARGE_PERIOD, False)
    set_time(DISCHARGE_START, OFF)
    set_time(DISCHARGE_END, OFF)
if peak is None:
    set_switch(PEAK_SHAVING, False)
if TIME_OF_USE:
    set_switch(TIME_OF_USE, charge is not None or discharge is not None)

if charge is not None:
    set_number(CHARGE_CURRENT, charge[0])
    set_number(CHARGE_SOC, charge[1])
    set_time(CHARGE_START, OFF)
    set_time(CHARGE_END, end)
    if CHARGE_PERIOD:
        set_switch(CHARGE_PERIOD, True)
if discharge is not None:
    set_number(DISCHARGE_CURRENT, discharge[0])
    set_number(DISCHARGE_SOC, discharge[1])
    set_time(DISCHARGE_START, OFF)
    set_time(DISCHARGE_END, end)
    if DISCHARGE_PERIOD:
        set_switch(DISCHARGE_PERIOD, True)
if peak is not None:
    grid_power = peak[0]
    if PEAK_GRID_POWER_MAX is not None:
        grid_power = min(grid_power, PEAK_GRID_POWER_MAX)
    set_number(PEAK_GRID_POWER, grid_power)
    set_number(PEAK_BASELINE_SOC, peak[1])
    set_switch(PEAK_SHAVING, True)

# Curtail the PV surplus instead of exporting it (negative export price)
set_switch(FEED_IN_LIMIT, state == "discard-excess" and not DISPATCH)

if dispatch is not None:
    dispatch["failsafe_minutes"] = DISPATCH_FAILSAFE
    command = str(dispatch)
    last = sent.attributes if sent is not None and sent.state == "on" else {}
    sent_at = dt_util.parse_datetime(last.get("sent") or "")
    age = (dt_util.now() - sent_at).total_seconds() if sent_at else None
    # Repeated so the failsafe does not end it, but not on every guard update
    if last.get("command") != command or age is None or age >= DISPATCH_REFRESH:
        if last.get("command") != command:
            logger.info("Remote dispatch %s", command)
        hass.services.call("solis_modbus", "solis_dispatch", dispatch, False)
        hass.states.set(
            DISPATCH_STATE,
            "on",
            {
                "friendly_name": "Battery dispatch",
                "command": command,
                "mode": dispatch["mode"],
                "sent": dt_util.now().isoformat(),
            },
        )
