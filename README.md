# energy_planner
Energy planner for HA

# Plan and actions

The integration plans the battery, an electric car and a heat pump per quarter
(`sensor.energy_planner_battery_plan`, with the plan in its attributes) and publishes
what each device should do now (`sensor.energy_planner_battery_action`, `_ev_action`,
`_heat_pump_action`). Scripts and automations in `examples/` apply them.

# Manual slot form
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
  keeping its value. The schedule of 49 slots is gone: the battery follows the plan,
  and manual slots (`energy_planner.add_slot`) are forced into it and rounded out to
  whole quarters. The removed entities are deleted from the entity registry.
- In dashboards, replace `select.energy_planner_planner_state` with
  `switch.energy_planner_enabled` and `number.energy_planner_price_peak_efficiency_factor`
  with `number.energy_planner_battery_efficiency`. Remove the earliest charge and
  discharge times, the basic, cheapest hours and price peak settings, and the
  schedule card (the `slot_*` entities). The plan chart shows the plan instead
  (`sensor.energy_planner_battery_plan`, whose `manual_slots` attribute lists the
  manual slots and `manual` marks their quarters), and the add-slot form in
  `examples/dashboard/helpers.yml` adds and clears them.
- In automations, remove triggers on the `slot_*` entities (the battery action
  sensor is enough) and calls to `energy_planner.run_planner` (the plan follows new
  prices by itself). `add_slot` no longer takes the state `off`; use
  `energy_planner.remove_manual_slot` or `clear_manual_slots`. In the
  `energy_planner_extras.yaml` package, the "Run Planner" button is gone.
- After the restart, check that `sensor.energy_planner_battery_action` has a state.
