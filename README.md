# Energy planner

A Home Assistant custom integration that plans a home battery, an electric car and a
heat pump per quarter hour from Nord Pool spot prices, the grid tariff and forecasts
of the house's load and solar power. It works with any inverter and battery that
Home Assistant can read and control; it runs in four Swedish homes (all with Solis
hybrid inverters, which the example scripts are written for). It is a personal
project, not a polished product, so expect to adapt entity ids and scripts.

## What it does

- **Forecasts** the house load (from its own history, optionally corrected for the
  outdoor temperature) and the PV power (Open-Meteo per panel plane, calibrated on
  the actual production), plus planned loads from a calendar.
- **Plans the battery** with dynamic programming over the SOC: charge, hold, self-use,
  sell, sell or curtail the solar surplus, per quarter, with grid fees, monthly power
  charges, the main fuse, battery wear and its efficiency. Before tomorrow's prices
  are published it plans on an estimate (same-type days, a learned correction and the
  wind forecast).
- **Plans the car** with the battery: the energy it needs by the next morning or a
  trip, in the cheapest quarters.
- **Plans an air-to-air heat pump** against district heating, from its COP at the
  forecast outdoor temperature.
- **Guards the main fuse** per phase, splitting the room between the car charger and
  the battery's grid charging.
- **Manual slots** override the plan (charge, sell, pause, ... from a time to a time).

The integration only *publishes* the plan and what each device should do now:
`sensor.energy_planner_battery_plan` (the plan in its attributes),
`sensor.energy_planner_battery_action`, `_ev_action`, `_heat_pump_action` and
`_fuse_guard`. Scripts and automations in `examples/` apply them to the devices.
The battery action is a mode (charge, pause, discharge, sell, sell or discard the
solar surplus) with an import target in kW, a target SOC and a current, so the
inverter needs to charge from the grid, export, and ideally cap the grid import
(peak shaving); with another brand, write a script that maps the action onto its
controls.

## Requirements

- Home Assistant 2026.9 or later.
- The [Nord Pool custom integration](https://github.com/custom-components/nordpool)
  from HACS, not the Nord Pool integration built into Home Assistant. The planner
  fetches prices with the HACS integration's `nordpool.hourly` service and reads the
  area and currency from its sensor's entity id (`sensor.nordpool_kwh_se3_sek_…`);
  the built-in one has neither. Both use the domain `nordpool`, and the HACS one
  replaces the built-in one when installed.
- Sensors for the house load, PV power, battery SOC and voltage, with history in the
  recorder. Forecast.Solar entries for the panel planes (for their tilt, direction,
  kWp and location).
- Something that applies the actions. `examples/battery/follow_battery_action.py` is
  written for Solis inverters (peak shaving, time-of-use and Remote Dispatch); the
  car and heat pump automations for Zaptec and MELCloud.

## Installation

1. Add this repository to HACS as a custom integration repository and install
   "Energy planner", then restart Home Assistant.
2. Add the integration and pick the HACS Nord Pool integration's sensor; choose the
   grid tariff in its options.
3. Point the settings at your entities and set up the scripts:
   [`examples/new_home_setup.md`](examples/new_home_setup.md).

## Documentation

- [`examples/README.md`](examples/README.md): the scripts, automations and dashboards.
- [`docs/`](docs/README.md): how each part works and the backtests behind it
  (forecast, battery plan, car, heat pump, main fuse), and the decisions.
- [`GLOSSARY.md`](GLOSSARY.md): the domain terms.
- [`AGENTS.md`](AGENTS.md): development: commands, checks and conventions.

## Development

```bash
make install   # uv sync
make check     # ruff, ty and the tests (scripts/check.sh)
make hooks     # pre-commit and commit message hooks
```

## Manual slot form
- Add the following to your configuration.yaml
```yaml
homeassistant:
  packages:
    energy_planner_extras: !include energy_planner_extras.yaml
```
- Add the energy_planner_extras.yaml file to your configuration folder

## Upgrading from the slot planners

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
