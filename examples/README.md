# Examples

Home Assistant scripts, automations and cards that consume what the integration
publishes. The integration only produces the schedule and the actions as entities;
these apply them to the devices. Entity ids are the owner's (Solis, Zaptec,
MELCloud), so check them before copying. `python_script`s go in
`config/python_scripts/` (needs `python_script:` in `configuration.yaml`).

## `battery/`

| File | What it does |
| --- | --- |
| `follow_battery_action.py` | Makes a Solis inverter follow `sensor.energy_planner_battery_action`, only writing settings that changed: peak shaving for the plan's import targets, time-of-use slot 6 for manual slots and `sell-excess`, the feed-in limit for `discard-excess`. With `DISPATCH = True`: `charge` and `sell` through Solis Remote Dispatch (needs register 34502 = 0xAA55) |
| `follow_battery_action_automation.yml` | Runs it every minute and on changes, with `input_boolean.follow_battery_action` as an on/off switch |
| `follow_battery_action_dispatch_automation.yml` | Runs the `DISPATCH = True` copy (`follow_battery_action_dispatch.py`); the plain one stays as the fallback |
| `solis_dispatch_test.yaml` | Two HA scripts that test Remote Dispatch step by step (grid import, charge, the import limit, the failsafe) and log every 30 s; results in `docs/solis_remote_dispatch_2026_10.md` |
| `solis_dispatch_mode_test.yaml` | Tests for the modes that still use time-of-use and peak shaving: `script.solis_dispatch_sun_test` (in sunshine: self-consumption, feed-in priority, the SOC window's end, an export limit of 0, charging with PV) and `script.solis_dispatch_hold_test` (without PV: hold as a grid import target without grid charging, `battery_hold` with an import limit, discharging at the window's end) |
| `update_battery_action.py`, `update_battery_action_automation.yml` | The older script, rewrites every setting from slot 1; replaced by `follow_battery_action.py` |

## `ev/`

| File | What it does |
| --- | --- |
| `follow_ev_action_automation.yml` | Starts and stops a Zaptec charger from `sensor.energy_planner_ev_action` and sets its current from the fuse guard (`sensor.energy_planner_fuse_guard`) |

## `heat_pump/`

| File | What it does |
| --- | --- |
| `follow_heat_pump_action_automation.yml` | Switches `climate.hallen` between `heat` and `off` from `sensor.energy_planner_heat_pump_action` |

## `comfort/`

| File | What it does |
| --- | --- |
| `heating_test_package.yaml` | HA package for the weekend heating step test (everything off, district heating only, the heat pump only); results in `docs/heating_step_test_2026_10.md` |

## `dashboard/`

| File | What it does |
| --- | --- |
| `schedule_card.yml` | Lovelace card with the 49 slots (generated) |
| `generate_schedule_card.py` | Writes `schedule_card.yml` (run it in this folder) |
| `helpers.yml` | Lovelace card with the battery and planner settings |
