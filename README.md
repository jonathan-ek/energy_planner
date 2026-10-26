# energy_planner
Energy planner for HA

# card
using HACS plugins:
- card-mod
- Vertical Stack In Card
- Multiple Entity Row

Copy the content of Schedule_card.yml to a new card in your lovelace dashboard.

# Manual add slot Form
- Add the following to your configuration.yaml
```yaml
homeassistant:
  packages:
    energy_planner_extras: !include energy_planner_extras.yaml
```
- Add the energy_planner_extras.yaml file to your configuration folder
# Upgrading from the slot planners

The basic, cheapest hours and price peak planners were removed (Oct 2026, see
`docs/adr/0001-one-planner-no-schedule.md`). To stay on them, pin the tag
`v-last-slot-planners` in HACS. After upgrading:

- The integration moves the stored settings itself: `planner_state` becomes
  `switch.energy_planner_enabled` (off only if the planner was off) and
  `price_peak_efficiency_factor` becomes `number.energy_planner_battery_efficiency`,
  keeping its value. A house that ran a removed planner gets the dynamic planner's
  schedule once Home Assistant has started. The removed entities are deleted from the
  entity registry.
- In dashboards, replace `select.energy_planner_planner_state` with
  `switch.energy_planner_enabled` and `number.energy_planner_price_peak_efficiency_factor`
  with `number.energy_planner_battery_efficiency`. Remove the earliest charge and
  discharge times and the basic, cheapest hours and price peak settings.
- After the restart, check that `sensor.energy_planner_battery_action` has a state.
