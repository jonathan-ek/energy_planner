# Description: This script is used to update the battery action based on
#              the energy planner slot 1 state and active status.

# DO NOT COPY the following section, it is only used to prevent errors in the IDE
from homeassistant.core import HomeAssistant

hass: HomeAssistant = HomeAssistant(".")
output = {}

# ---------------------------- Start of the actual code ----------------------------


def get_state(entity_id):
    """Get the state of an entity."""
    hass_state = hass.states.get(entity_id)
    if hass_state is None:
        raise ValueError(f"State for {entity_id} not found")
    return hass_state.state


def set_state(entity_id, value):
    """Set the state of an entity."""
    if hass.states.set is None:
        raise ValueError("hass.states.set is None")
    hass.states.set(entity_id, value)


if hass.services.call is None:
    raise ValueError("hass.services.call is None")

max_charge_current = 20
max_discharge_current = 20

charge_current = min(
    int(float(get_state("number.energy_planner_max_charge_current"))),
    max_charge_current,
)
discharge_current = min(
    int(float(get_state("number.energy_planner_max_discharge_current"))),
    max_discharge_current,
)

current_entity = "number.solis_s6_eh3p_grid_time_of_use_charge_battery_current_slot_6"
soc_entity = "number.solis_s6_eh3p_grid_time_of_use_charge_cut_off_soc_slot_6"
start_entity = "time.solis_s6_solis_grid_charging_charge_slot_6_start"
end_entity = "time.solis_s6_solis_grid_charging_charge_slot_6_end"

discharge_current_entity = (
    "number.solis_s6_eh3p_grid_time_of_use_discharge_battery_current_slot_6"
)
discharge_soc_entity = (
    "number.solis_s6_eh3p_grid_time_of_use_discharge_cut_off_soc_slot_6"
)
discharge_start_entity = "time.solis_s6_solis_grid_charging_discharge_slot_6_start"
discharge_end_entity = "time.solis_s6_solis_grid_charging_discharge_slot_6_end"

grid_feed_in_limit_entity = "switch.grid_feed_in_power_limit_switch"

battery_discharge_power_entity = (
    "sensor.solis_s6_solis_battery_discharge_power"  # positive
)
battery_charge_power_entity = "sensor.solis_s6_solis_battery_charge_power"  # positive
battery_soc_entity = "sensor.solis_s6_solis_battery_soc"  # positive
grid_power_entity = (
    "sensor.solis_s6_solis_meter_total_active_power"  # positive = sell, negative = buy
)
pv_power_entity = "sensor.solis_s6_solis_total_pv_power"  # positive
house_power_entity = "sensor.solis_s6_solis_household_load_power"  # positive

state = get_state("select.energy_planner_slot_1_state")
active = get_state("switch.energy_planner_slot_1_active")
start = get_state("datetime.energy_planner_slot_1_date_time_start")
soc = get_state("number.energy_planner_slot_1_soc")
end = get_state("datetime.energy_planner_slot_2_date_time_start")

output["start"] = str(start).split(" ")[1]
output["end"] = str(end).split(" ")[1]
output["active"] = active
output["state"] = state

new_soc = None
new_current = None
charge = False
discharge = False
disable_export = False

if active == "on":
    if str(state) == "off":
        pass
    elif str(state) == "charge":
        charge = True
        new_soc = int(float(soc))
        new_current = charge_current
        # Automatic pause until next slot after charge has finished
    elif str(state) == "pause":
        charge = True
        new_soc = int(float(soc))
        new_current = 0
        battery_power = float(get_state(battery_discharge_power_entity))
        grid_power = float(get_state(grid_power_entity))
        # grid_power > -100 => buying
        # battery_power < 400 => almost no usage of the battery
        if grid_power > -100 and battery_power < 400:
            # Fallback on self-use
            charge = False
    elif str(state) == "discharge":
        # self-use
        pass
    elif str(state) == "sell":
        discharge = True
        new_soc = int(float(soc))
        new_current = discharge_current
        battery_soc = float(get_state(battery_soc_entity))
        if battery_soc <= new_soc:
            # Fallback on self-use after the selling is done
            discharge = False
    elif str(state) == "sell-excess":
        discharge = True
        new_soc = int(float(soc))  # must be lower than current soc
        new_current = 0
        pv_power = float(get_state(pv_power_entity)) + 100
        house_power = float(get_state(house_power_entity))
        if pv_power < house_power:
            # Fallback on self-use if not enough electricity is produced.
            discharge = False
    elif str(state) == "discard-excess":
        disable_export = True
        # self-use + discard overproduction

if charge:
    hass.services.call(
        "time",
        "set_value",
        {"entity_id": start_entity, "time": str(start).split(" ")[1]},
        False,
    )
    hass.services.call(
        "time",
        "set_value",
        {"entity_id": end_entity, "time": str(end).split(" ")[1]},
        False,
    )
    hass.services.call(
        "number",
        "set_value",
        {"entity_id": current_entity, "value": new_current},
        False,
    )
    hass.services.call(
        "number", "set_value", {"entity_id": soc_entity, "value": new_soc}, False
    )
else:
    hass.services.call(
        "time", "set_value", {"entity_id": start_entity, "time": "00:00"}, False
    )
    hass.services.call(
        "time", "set_value", {"entity_id": end_entity, "time": "00:00"}, False
    )

if discharge:
    hass.services.call(
        "time",
        "set_value",
        {"entity_id": discharge_start_entity, "time": str(start).split(" ")[1]},
        False,
    )
    hass.services.call(
        "time",
        "set_value",
        {"entity_id": discharge_end_entity, "time": str(end).split(" ")[1]},
        False,
    )
    hass.services.call(
        "number",
        "set_value",
        {"entity_id": discharge_current_entity, "value": new_current},
        False,
    )
    hass.services.call(
        "number",
        "set_value",
        {"entity_id": discharge_soc_entity, "value": new_soc},
        False,
    )
else:
    hass.services.call(
        "time",
        "set_value",
        {"entity_id": discharge_start_entity, "time": "00:00"},
        False,
    )
    hass.services.call(
        "time", "set_value", {"entity_id": discharge_end_entity, "time": "00:00"}, False
    )

if disable_export:
    hass.services.call(
        "switch", "turn_on", {"entity_id": grid_feed_in_limit_entity}, False
    )
else:
    hass.services.call(
        "switch", "turn_off", {"entity_id": grid_feed_in_limit_entity}, False
    )
