# Setting up the planner in another home

How the planner was installed in the owner's family's homes (Oct 2026). The defaults
are the owner's entity ids, so another home needs most of the steps below.

## 1. Integrations

| Integration | Use |
| --- | --- |
| `nordpool` (HACS, [custom-components/nordpool](https://github.com/custom-components/nordpool)) | Spot prices; needed for its `nordpool.hourly` service. HA's built-in Nord Pool integration does not work (no such service, other sensor names); the HACS one replaces it. Region, kWh, VAT on, additional costs 0 (the planner adds the grid fees). Prices in öre or SEK both work: the area and currency are read from the entity id. `nordpool_reload_automation.yml` reloads it when tomorrow's prices are late |
| Inverter (HACS, e.g. `solis_modbus`) | House load, PV, battery SOC and voltage, and the controls the battery script writes |
| Forecast.Solar (core) | One entry per panel plane. The PV forecast reads the planes, kWp and location from it and fetches Open-Meteo itself; disable the entry of a plane with faulty panels |
| Local calendar (core) | `calendar.energiplan`: planned loads (`bastu`, `tvätt`, `5 kWh`, ...) and away (`borta`), see `docs/forecast.md` |
| Car charger (optional) | Zaptec or Easee: status, session energy and a way to start, stop and set the current |

Install the integration from HACS, add it and pick the HACS Nord Pool sensor; in its
options choose the grid tariff (flat from `network_cost` / `network_compensation`, a
preset such as `tekniska_verken_alternativ` or `eon_20a`, or custom YAML, see
`planner/tariff.py`).

## 2. Settings

Every setting and its default is in `SETTINGS` in
`custom_components/energy_planner/planner/settings.py`.

- **Numbers, switches and selects** are entities (`number.energy_planner_*`, ...): set
  the battery (capacity in Wh, currents, shutdown / reserve / max SOC, efficiency),
  the grid (`network_cost` and `network_compensation` for a flat tariff, the import
  limit, `main_fuse`), the car and the heat pump (`heat_pump_power` 0 without one) from
  a dashboard. The round-trip efficiency can be measured from night charge and
  discharge runs, see `docs/battery-plan.md`.
- **Input entities** have no entity and default to the owner's ids. Stop HA, edit
  `data` in `.storage/energy_planner.config`, start HA again:

| Key | What to point it at |
| --- | --- |
| `forecast_load_sensor` | The house load (W, `state_class: measurement`) |
| `forecast_pv_sensor` | Total PV power (W); with a second inverter, a template summing both |
| `forecast_ev_sensor` | The car charger's power, taken out of the load history (an id that does not exist means no car) |
| `forecast_calendar` | The local calendar |
| `battery_soc_sensor`, `battery_voltage_sensor` | Battery SOC (%) and voltage (V) |
| `grid_import_sensor` | The grid import energy counter (kWh total, e.g. the HAN meter): this month's power peaks |
| `forecast_load_model`, `forecast_pv_calibration` | Selects: `temperature` for a house heated with electricity, `hourly` for panels shaded part of the day |
| `outdoor_temperature_sensor`, `indoor_temperature_sensor`, `heat_pump_climate`, `heat_pump_model`, `district_heating` | The heat pump (`docs/heat-pump.md`); the outdoor sensor is also used by the `temperature` load model |
| `ev_charger_status_sensor`, `ev_session_energy_sensor`, `ev_soc_sensor`, `ev_charge_limit_sensor` | The car (`docs/car.md`); without a status sensor the car is not planned |
| `grid_phase_import_sensors`, `grid_phase_export_sensors`, `inverter_ac_power_sensor`, `inverter_grid_power_sensor`, `ev_phase_current_sensors`, `ev_charger_current_number` | The main fuse guard (`docs/main-fuse.md`), optional |

The forecast needs hourly statistics of the load and PV sensors and works better
with a couple of weeks of history. After the restart, `sensor.energy_planner_battery_plan`
should have a plan and `sensor.energy_planner_battery_action` a state.

## 3. Applying the actions

The integration only publishes what to do; `examples/README.md` lists the scripts and
automations that do it:

- `battery/follow_battery_action.py` and its automation make a Solis inverter follow
  the battery action. Set the entity ids and constants at the top: `DISPATCH = True`
  where the firmware has Remote Dispatch (register 34502 = 0xAA55), and
  `PEAK_SHAVING_CHARGE = False` for an inverter whose peak shaving does not limit grid
  charging (the S6-EH3P without dispatch).
- `ev/follow_ev_action_automation.yml` starts and stops the charger,
  `heat_pump/follow_heat_pump_action_automation.yml` the heat pump.
- `peak_shaving_automations.yml`: the older Solis peak shaving, a fallback.
- `energy_planner_extras.yaml` (repo root): the add slot form, as a package.
- `dashboard/build_elpris_dashboard.py`: the mobile price and savings dashboard; add a
  profile for the home to `SITES` (its url and token file go in
  `elpris_sites.local.json`, which git ignores).

Frontend cards: apexcharts-card, card-mod, tabbed-card, sunsynk-power-flow-card and,
for `energi_dashboard.yaml`, lovelace-multiple-entity-row.
