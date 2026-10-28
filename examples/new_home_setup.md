# Setting up the planner in another home

Notes from the owner's HA (2026-10-09), for installing the planner somewhere else.

## Integrations (HACS unless noted)

| Integration | Owner's version | Use |
| --- | --- | --- |
| `nordpool` | 0.0.18 | Spot prices. Region, kWh, price in cents, VAT on, additional costs 0 (see `nordpool_reload_automation.yml`) |
| `solis_modbus` | 4.2.6 | The owner's Solis S6 inverter: load, PV, SOC, battery voltage, peak shaving |
| `easee` | 0.9.74 | The owner's charger (`sensor.ehwuhqtp_effekt`, taken out of the load history) |
| Forecast.Solar (core) | | One entry per panel plane. The PV forecast reads planes, kWp and location from it and fetches Open-Meteo itself |
| Local calendar (core) | | `calendar.energiplan`: planned loads and away (see `docs/forecast.md`) |

Frontend cards for `energi_dashboard.yaml`: apexcharts-card, lovelace-multiple-entity-row,
sunsynk-power-flow-card.

## Owner-specific entity ids

These defaults (`const.py`) have no setting in the UI; the code reads
`config.get(key, default)`. To use other entities, stop HA, add the keys to `data` in
`.storage/energy_planner.config`, then start HA again (or change the defaults in
`const.py` on a branch):

| Key | Owner's entity |
| --- | --- |
| `forecast_load_sensor` | `sensor.solis_s6_solis_household_load_power` (W, the house load) |
| `forecast_pv_sensor` | `sensor.solis_s6_solis_total_pv_power` |
| `forecast_ev_sensor` | `sensor.ehwuhqtp_effekt` (charger power, subtracted from the load) |
| `forecast_calendar` | `calendar.energiplan` |
| `battery_soc_sensor`, `battery_voltage_sensor` | `sensor.solis_s6_solis_battery_soc`, `..._battery_voltage` |
| `grid_import_sensor` | `sensor.matarstallning_aktiv_energi_uttag` (HAN meter, kWh total) |
| `outdoor_temperature_sensor`, `indoor_temperature_sensor` | `sensor.gw1100a_outdoor_temperature`, `..._indoor_temperature` |
| `heat_pump_climate` | `climate.hallen` (set `heat_pump_power` to 0 when there is none) |

The forecast needs hourly statistics (`state_class: measurement`) of the load and PV
sensors, and works better with a couple of weeks of history.

## Automations and scripts

- `follow_battery_action.py` + `follow_battery_action_automation.yml`: the inverter
  follows the plan (Solis entity names).
- `follow_heat_pump_action_automation.yml`: the heat pump.
- `nordpool_reload_automation.yml`: reloads Nord Pool when tomorrow's prices are late.
- `peak_shaving_automations.yml`: the older Solis peak shaving, a fallback.
- `energy_planner_extras.yaml` (repo root): the add slot form, as a package.
