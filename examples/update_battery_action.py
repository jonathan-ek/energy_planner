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

max_charge = min(
    int(float(get_state("number.energy_planner_max_charge_current"))),
    max_charge_current,
)
max_discharge = min(
    int(float(get_state("number.energy_planner_max_discharge_current"))),
    max_discharge_current,
)
current_entity = "number.solis_s6_eh3p_grid_time_of_use_charge_battery_current_slot_6"
soc_entity = "number.solis_s6_eh3p_grid_time_of_use_charge_cut_off_soc_slot_6"
start_entity = "time.solis_s6_solis_grid_charging_charge_slot_6_start"
end_entity = "time.solis_s6_solis_grid_charging_charge_slot_6_end"

discharge_current_entity = "number.solis_s6_eh3p_grid_time_of_use_discharge_battery_current_slot_6"
discharge_soc_entity = "number.solis_s6_eh3p_grid_time_of_use_discharge_cut_off_soc_slot_6"
discharge_start_entity = "time.solis_s6_solis_grid_discharging_charge_slot_6_start"
discharge_end_entity = "time.solis_s6_solis_grid_discharging_charge_slot_6_end"

grid_feed_in_limit_entity = "switch.grid_feed_in_power_limit_switch"

battery_power_entity = "sensor.solis_s6_solis_battery_discharge_power"
grid_power_entity = "sensor.solis_s6_solis_meter_total_active_power"

state = get_state("select.energy_planner_slot_1_state")
active = get_state("switch.energy_planner_slot_1_active")
start = get_state("datetime.energy_planner_slot_1_date_time_start")
soc = get_state("datetime.energy_planner_slot_1_soc")
end = get_state("datetime.energy_planner_slot_2_date_time_start")
output["start"] = str(start).split(" ")[1]
output["end"] = str(end).split(" ")[1]
output["active"] = active
output["state"] = state
if active == "on":
    if str(state) in ["charge", "pause"]:
        output["soc"] = 100
        hass.services.call(
            "number", "set_value", {"entity_id": soc_entity, "value": 100}, False
        )
        set_time = True
        if str(state) == "charge":
            output["current"] = charge_current
            hass.services.call(
                "number",
                "set_value",
                {"entity_id": current_entity, "value": charge_current},
                False,
            )
        elif str(state) == "pause":
            output["current"] = 0
            battery_power = float(get_state(battery_power_entity))
            grid_power = float(get_state(grid_power_entity))
            output["battery_power"] = battery_power
            output["grid_power"] = grid_power
            if grid_power > -100 and battery_power < 500:
                output["battery_power"] = battery_power
                output["grid_power"] = grid_power
                hass.services.call(
                    "time", "set_value", {"entity_id": start_entity, "time": "00:00"}, False
                )
                hass.services.call(
                    "time", "set_value", {"entity_id": end_entity, "time": "00:00"}, False
                )
                set_time = False
            else:
                hass.services.call(
                    "number", "set_value", {"entity_id": current_entity, "value": 0}, False
                )
        if set_time:
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
    elif str(state) in ["discharge", "sell", "sell-excess"]:
        hass.services.call(
            "time", "set_value", {"entity_id": start_entity, "time": "00:00"}, False
        )
        hass.services.call(
            "time", "set_value", {"entity_id": end_entity, "time": "00:00"}, False
        )
        if str(state) == "sell-excess":
            pass
        elif str(state) == "sell":
            pass
        elif str(state) == "discharge":
            pass
    else:
        hass.services.call(
            "time", "set_value", {"entity_id": start_entity, "time": "00:00"}, False
        )
        hass.services.call(
            "time", "set_value", {"entity_id": end_entity, "time": "00:00"}, False
        )
else:
    hass.services.call(
        "time", "set_value", {"entity_id": start_entity, "time": "00:00"}, False
    )
    hass.services.call(
        "time", "set_value", {"entity_id": end_entity, "time": "00:00"}, False
    )
