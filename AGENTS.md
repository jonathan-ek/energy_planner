# Energy Planner — agent guide

Home Assistant custom integration (HACS, domain `energy_planner`) that builds a
battery charge/discharge schedule from Nord Pool spot prices. The integration
only *produces* the schedule as HA entities; applying it to an inverter is done
outside the integration by scripts/automations (see `examples/`).

Domain terms (plan, schedule, slot, mode, run, action, setting, reading) are defined
in `GLOSSARY.md`.

## Layout

| Path | What it is |
| --- | --- |
| `custom_components/energy_planner/__init__.py` | Setup, `hass.data` structure, services (`async_setup`), timers (`async_setup_entry`), planner dispatch (`run_planner`) |
| `custom_components/energy_planner/planner/` | All planning logic (see below) |
| `custom_components/energy_planner/button.py` | `button.energy_planner_update_battery_plan`, recalculates the battery plan |
| `custom_components/energy_planner/{datetime,number,select,switch,time}.py` | Entity platforms; each holds its entity definitions as dicts plus one generic entity class |
| `custom_components/energy_planner/sensor.py` | The two forecast sensors (`sensor.energy_planner_load_forecast`, `sensor.energy_planner_pv_forecast`) `sensor.energy_planner_battery_plan`, the three action sensors in `ACTION_SENSORS`, one class: `battery_action` (what the inverter should do now, `battery_plan.current_action`), `heat_pump_action` (`heating.heat_pump_action`) and `ev_action` (`battery_plan.car_action`), the heat pump sensors `heat_pump_cop`, `district_heating_price`, `heat_pump_saving` (`heating.heat_pump_economy`) and `fuse_guard` (`fuse_guard.update_guard`); they only read `hass.data` |
| `custom_components/energy_planner/store.py` | Thin wrapper around HA `Store` (`.storage/energy_planner.<key>`) |
| `custom_components/energy_planner/config_flow.py` | Setup asks for `nordpool_entity_id`; the options flow chooses the grid tariff (flat, a preset or custom YAML) |
| `custom_components/energy_planner/services.yaml`, `translations/en.json` | Service descriptions |
| `custom_components/energy_planner/migrate.py` | Moves stored settings of older versions to the current keys (`migrate_config`, on load) and removes the registry entries of entities that are gone (`remove_entities`, on setup) |
| `tests/` | `planner/`: the planning modules and their HA glue; `comfort/`; at the top config flow, entities, services, triggers and the migration. `tests/README.md` says how they reach HA |
| `examples/` | Scripts, automations and cards that consume the schedule, by topic: `battery/` (the inverter scripts, incl. Remote Dispatch and its test), `ev/`, `heat_pump/`, `comfort/` (the heating test package), `dashboard/`; index in `examples/README.md` |
| `energy_planner_extras.yaml`, `add_slot_form.yml` | HA package + card for the manual "add slot" form |
| `local_deploy.sh` | Copies the component into `~/projects/ha_demo/config` and restarts its docker compose |

### `planner/`

| File | Role |
| --- | --- |
| `__init__.py` | Re-exports `dynamic_planner` (the module's `planner()`) and `async_update_forecast`. Note `planner.dynamic_planner` is then the function, not the module (tests use `importlib`) |
| `dynamic_planner.py` | The planner, and the forecast glue. `async_update_forecast` reads recorder hourly statistics and the Forecast.Solar estimates, runs `forecast.py` and stores the result in `hass.data[DOMAIN]["forecast"]`. `planner()` updates the forecast and the plan (waiting for a calculation already running) and writes one `auto` slot to the plan's end, with manual slots laid over it; without a new plan from the last one (with a warning), without a plan reaching now not at all |
| `battery_optimizer.py` | Pure battery optimizer (numpy): `optimize` picks a mode per quarter (`self_use`, `hold`, `sell_excess`, `discard_excess`, `charge`, `sell`) by dynamic programming over the SOC and searches the monthly power peak levels. See "Battery plan" |
| `battery_plan.py` | HA glue for the optimizer: `async_update_plan` reads the forecast, prices, battery settings and state and this month's hourly grid import, and stores a `plan.Plan` in `hass.data[DOMAIN]["plan"]`; `async_request_plan` (one run at a time, requests during a run give one more), `current_action`, `car_action` |
| `plan.py` | Pure: `Plan`, the battery plan as one object (per-quarter series parallel to `starts`, canonical units: spot SEK/MWh excl. VAT, SOC %, kW), `index_at`, `value_at` (a series' value at a moment), `run_at` (a `Run` of equal values from now, with `until`), `is_stale` (`STALE_AFTER` 30 min from `finished`, when the plan became available; `updated` is the moment it was planned for), `soc_at` (in the first quarter from `soc_start`, the SOC when planned), `start` / `end` (times compared and added in UTC, so the hour repeated when DST ends works; `quarter_start` / `quarter_end`, the price dicts' UTC keys), `to_attributes` (what the plan sensor publishes). `follow` (the run now, or why not: no plan, plan is old, plan does not cover now) and `action` (the keys every action has: `state`, `source`, `until`, `reason`, `plan_updated`) for the resolvers |
| `battery_action.py` | Pure: `resolve` turns slot 1 and the plan into the action now (slot state, source, SOC, current, import target, until), mode ↔ slot state maps |
| `heat_pump.py` | Pure heat pump economy: COP curve per outdoor temperature (`COP_CURVES`), district heating price per month (`DISTRICT_HEATING_PRESETS`), `evaluate` compares a kWh of heat from both, `action` (on/off now from a `Plan` or the economy now). See "Heat pump" |
| `ev.py` | Pure car charging: `is_plugged` (charger state), `need_from_history` (the largest charging night, noon to noon), `need_from_soc`, `ready_time` / `deadline_index` (the next working morning's ready time, or an earlier departure), `ev_action` (charge/wait now, from a `Plan`). See "Car" |
| `settings.py` | Every config store key with its one default (`SETTINGS`; the config entities take their defaults from it), `get` / `number`, typed groups in domain units (`battery`, `car`, `heat_pump`, `fuse`; `car` and `fuse` are None when not set up), `replans` (the plan triggers, `replan_entity_ids`) and `reading` (a sensor's float value, None if not configured, unavailable or old) |
| `fuse.py` | Pure main fuse guard: `limits` splits the room on the busiest phase between the car charger and the battery's grid charging, `smooth` (hysteresis for the devices). See "Main fuse" |
| `fuse_guard.py` | HA glue for `fuse.py`: live phase readings → `hass.data[DOMAIN]["fuse_guard"]` |
| `heating.py` | HA glue for `heat_pump.py`: outdoor temperature, the spot price now (`spot_prices`, also without a plan), the export now (from the plan, even an old one), the tariff |
| `price_estimate.py` | Pure: tomorrow's prices before they are published (same-type days + a learned correction per hour, Swedish holidays). See "Battery plan" |
| `../comfort/schedule.py`, `../comfort/thermal.py` | WIP comfort control, not wired into HA yet. See "Comfort" |
| `tariff.py` | Pure grid tariff model: `Tariff` with `buy_fee` / `sell_fee` (öre/kWh) and `power_cost` (kr), parsed from a dict by `tariff_from_dict`, presets in `TARIFF_PRESETS`, the household's `import_limit`. Format in the module docstring |
| `forecast.py` | Pure forecast functions, no HA imports: quarter list (DST safe), load forecast per hour, PV integration of the `watts` forecast, PV calibration, weekend reserve |
| `manual_slots.py` | `add_manual_slots` overlays user-added slots on the generated schedule, shifting slots forward/back |
| `nordpool_utils.py` | `fetch_nordpool_data` (via the `nordpool.hourly` service, cached per date) and the area → timezone map `tzs` |
| `utils.py` | `reset`, `clear_slot`, `write_schedule`, `store_disable_state`/`restore_disable_state`, `update_entities`, `clear_passed_slots`, `parse_datetime` |

## Data model

Everything lives in `hass.data["energy_planner"]`:

- `values` — the schedule, plus the `nordpool_values` price cache. Persisted.
- `config` — user settings (entity values with `data_store: "config"`), plus
  `entry_id` and `nordpool_entity_id`. Persisted.
- `manual_slots` — list of `{start, end, state, soc}`. Persisted.
- `number_entities`, `select_entities`, … — lists of entity objects per platform
  (keys in `const.py`), used by `update_entities`.
- `save` — coroutine that persists the three stores.
- `options` — a copy of the config entry options (`tariff_preset`, `tariff`), kept
  current by an update listener. Read the tariff with `get_tariff(hass)` (`utils.py`),
  which falls back to a flat tariff from `network_cost` / `network_compensation`.
- `spot_prices` — the published spot prices per quarter (SEK/MWh excl. VAT, keyed by
  the quarter's start in UTC, so the hour repeated when DST ends has its own), written
  by every plan run before it can fail on a missing forecast or battery state; read by
  the heat pump sensors.
- `tmp` — scratch space during a planner run.

### Slots

The schedule is `SLOT_COUNT` (49) fixed slots, numbered from 1. Slot `i` is four keys
in `values`, each mirrored by an entity `<platform>.energy_planner_<key>`:

| Key | Platform | Notes |
| --- | --- | --- |
| `slot_{i}_date_time_start` | `datetime` | UTC in memory (`write_schedule`, `parse_stored_data` on load, the entity and `add_slot` convert); stored as ISO string, shown in local time |
| `slot_{i}_state` | `select` | `auto` (follow the battery plan), `charge`, `discharge`, `sell`, `sell-excess`, `discard-excess`, `pause`, `off` |
| `slot_{i}_soc` | `number` | target SOC % |
| `slot_{i}_active` | `switch` | user can disable a slot |

- A slot has no end; it ends where slot `i+1` starts.
- The schedule is terminated by a slot with state `off` whose start is the end of
  the last real slot. Planners append with `write_schedule`, which finds the first
  `off` slot and drops (with a warning) whatever does not fit.
- Slot 1 is always the current slot. `clear_passed_slots` runs every minute and
  shifts everything down one when slot 2's start has passed.
- State semantics for the inverter are documented in the comment in `select.py`.

### Config keys (entity ids are `<platform>.energy_planner_<key>`)

- `switch`: `enabled` (on; off: the plan is still made and shown, but the battery
  action does not follow it, manual slots still run; the car and heat pump actions
  follow the plan either way)
- `select`: `forecast_load_model`
  (`profile`, default; `temperature`) and `forecast_pv_calibration` (`daily`, default;
  `hourly`), see "Forecast"
- `time`: `ev_ready_time` (06:30)
- `datetime`: `ev_departure` (a trip, see "Car")
- `number`: `battery_efficiency` (%, round trip),
  `max_charge_current`, `max_discharge_current`, `battery_capacity` (Wh),
  `battery_shutdown_soc`, `battery_reserve_soc` (%, default 20: the plan does not
  discharge below it, see "Battery plan"), `battery_max_soc`, `network_cost` (öre/kWh incl. VAT paid on
  bought energy: transfer fee + energy tax, ~63 here), `network_compensation` (öre/kWh
  received when selling, ~5 here),
  `forecast_weekend_reserve` (kWh, default 4), `forecast_reserve_start` /
  `forecast_reserve_end` (h, default 18 / 22), `grid_import_limit` (kW hourly mean the
  planner may import, 0 = off), `grid_import_limit_start` / `grid_import_limit_end`
  (h, default 6 / 23), `main_fuse` (A per phase, three phases, 0 = off) and
  `fuse_margin` (A, default 2; see "Main fuse"), `battery_wear_cost` (öre per kWh taken out of the battery,
  default 20), `heat_pump_power` (W while heating until the draw is measured,
  default 300, 0 = not planned),
  `heat_pump_margin` (öre per kWh of heat, default 5), `heat_pump_heating_limit`
  (°C outdoor, default 15)
- Forecast inputs (config store only, defaults in `planner/settings.py`): `forecast_load_sensor`,
  `forecast_pv_sensor`, `forecast_ev_sensor`, `forecast_calendar`
  (`calendar.energiplan`); battery plan inputs `battery_soc_sensor`,
  `battery_voltage_sensor`, `grid_import_sensor`; heat pump inputs
  `outdoor_temperature_sensor` (`sensor.gw1100a_outdoor_temperature`),
  `heat_pump_model` (`msz_ap42`), `district_heating` (`tekniska_verken_2026`),
  `heat_pump_climate` (`climate.hallen`), `indoor_temperature_sensor`
  (`sensor.gw1100a_indoor_temperature`); fuse guard inputs (no defaults, see "Main
  fuse") `grid_phase_import_sensors`, `grid_phase_export_sensors`,
  `inverter_ac_power_sensor`, `inverter_grid_power_sensor`, `ev_phase_current_sensors`,
  `ev_charger_current_number`

## Control flow

- **Triggers:** a timer that fires every hour at :31:15 and acts when it is 15:xx
  Stockholm time (after next-day prices are published), and the
  `energy_planner.run_planner` service. Both call `run_planner(hass)` in
  `__init__.py`, which runs the dynamic planner, also while `enabled` is off (only
  the battery action ignores the `auto` slot then). The rule-based planners were
  removed (docs/adr/0001-one-planner-no-schedule.md); a house that ran one gets the
  dynamic planner's slots once HA has started (`migrate.migrate_config`).
- **A planner run:** update the forecast and the plan → `store_disable_state` →
  `reset` (all slots off) → one `auto` slot → `add_manual_slots` →
  `restore_disable_state` → `update_entities` → `save`.
- **Services:** `add_slot` (appends to `manual_slots` and re-applies them),
  `run_planner`, `clear_manual_slots` (reruns the planner so the schedule loses
  them), `update_forecast`, `update_battery_plan`
  (also the button `button.energy_planner_update_battery_plan`).
- **Forecast:** recalculated at :05 every hour (after the recorder compiles hourly
  statistics), once after HA has started, by the `update_forecast` service and by
  the dynamic planner. Errors are logged and do not stop the timers.
- **Battery plan:** recalculated every quarter (:00:30, :15:30, …), after each timed
  forecast update, and when tomorrow's prices arrive (`tomorrow_valid`), a setting with
  `replans` (`planner/settings.py`) or the tariff
  changes, manual slots change, or the
  SOC is `SOC_DRIFT` (5) % from the plan (at most every 5 min). All go through
  `async_request_plan`. The action sensor is rewritten every minute and when slot 1
  or `enabled` changes.
- **Entities** are passive views: `update()` reads from `hass.data`, setters write
  back and call `save`.

## Forecast

Method and numbers come from a backtest on the real HA data (Oct 2026):

- **Load** = `sensor.solis_s6_solis_household_load_power` hourly mean minus EV
  charging (`sensor.ehwuhqtp_effekt`) and the heat pump (share of the hour
  `climate.hallen` was in `heat` mode × its modelled draw, see "Heat pump"), which are
  planned separately. Forecast per local hour =
  mean of (last 7 days' profile, last 4 same-type days' profile; same type =
  workday/weekend), using only days before today, then split evenly into quarters.
  About 24% hourly and 14% daily error. Quarter profiles, temperature and gradient
  boosting gave no real improvement (district heating, so the load hardly depends on
  temperature). Before profiling, hours more than 3.5 kWh above the median of the same
  hour in the previous 14 days are capped (`cap_large_loads`): one-off loads like the
  sauna come from the calendar or the reserve instead, so they are not counted twice.
- **Load, temperature model** (`forecast_load_model` = `temperature`, for houses heated
  with electricity; `temperature_load_quarters`): each day's profile is scaled so its
  total changes by the heating slope × (Open-Meteo's daily mean temperature − the
  measured mean of the days behind the profile). The slope (kWh/day per °C, at most 0,
  published as `heating_slope`) is a least-squares fit of the 35 history days' load
  against their mean `outdoor_temperature_sensor`. Without Open-Meteo, or with too
  little or too even temperature history, it is the profile. Backtest on the owner's
  parents' house (electric heating, 595 days 2025-01 - 2026-09): daily error 21% ->
  18%, Nov-Mar 18% -> 13%, summer unchanged. The owner's house keeps `profile`.
- **PV** = Open-Meteo `global_tilted_irradiance` per panel plane (planes, kWp and
  location read from the Forecast.Solar config, `_solar_planes`; a disabled entry is
  left out, e.g. panels with a fault), averaged over the
  models in `OPEN_METEO_MODELS` (MET Nordic, ECMWF, ICON), derated for cell
  temperature, summed over planes, then interpolated to quarters from mid-hour points
  (keeps the hourly energy). One request per plane to the previous-runs API returns the
  latest forecast and the forecast made the day before for the last 14 days. Open-Meteo
  hourly values are means of the hour *before* the timestamp. Scaled by
  `actual / day-before forecast` over the last 14 days (actual: Solis `total_pv_power`
  with glitches above 25 kW dropped), limited to 0.3–2.5, 1.0 until 5 kWh of forecast
  exist. Day-ahead backtest (448 days): 0.63 kWh hourly, 11% daily error
  (Forecast.Solar with the same correction: 1.02 kWh, 17%).
  `forecast_pv_calibration` = `hourly` (for panels shaded part of the day;
  `pv_hourly_calibration`) uses a factor per local hour of the day instead, over the
  same 14 days, limited to 0.05-3.0; hours with under 1 kWh of forecast in the window
  keep the daily factor. The weather models assume an open horizon, so a daily factor
  spreads lost shaded hours over the whole day. Backtest on the parents' house (trees
  from about 15:00 in spring and autumn, 425 days): hourly error 1.07 -> 0.71 kWh,
  daily 14% -> 15%. Published as `pv_hourly_calibration` (hour -> factor). If Open-Meteo fails,
  Forecast.Solar `watts` with its `power_production_now` history is used instead;
  `pv_source` says which. Its PV is None on days it does not reach (so the plan ends
  there instead of planning them without sun).
- **Planned loads** come from timed events in the local calendar
  `calendar.energiplan` (recurring events work; all-day events are ignored). Energy:
  `<n> kWh` in the title or description, otherwise the sum of defaults for activities
  named in the title (`PLANNED_LOAD_DEFAULTS`: bastu 5, torktumlare 2.5, tvätt 1.5,
  disk 1, gäster 4, elbil/ladda 20 kWh), otherwise the event is ignored. The energy is
  spread evenly over the event, so the event should cover when the load runs.
- **Reserve** = `forecast_weekend_reserve` spread over Sat/Sun
  `forecast_reserve_start`–`forecast_reserve_end` (default 18–22, when the sauna
  usually runs), for unplanned weekend load. Holidays are not detected.
- **Away:** events in the same calendar whose title contains `borta`, `bortrest` or
  `away` (`forecast.AWAY_WORDS`, all-day events too) mark the quarters they cover as
  `away`: no weekend reserve there. The battery keeps `battery_reserve_soc`, and the
  load profile is not lowered.
- Output: `hass.data[DOMAIN]["forecast"]` with parallel lists `starts`, `load`
  (profile), `planned`, `reserve`, `pv` (kWh per quarter, all of today and the next two days, `FORECAST_DAYS`; the
  plan uses the third day once tomorrow's prices are published),
  `temperature` (Open-Meteo air temperature mid-quarter, None without Open-Meteo),
  `heat_pump_loss` / `heat_pump_loss_hours` (measured heat loss, kW/K),
  `planned_events`, `away` (bool per quarter), and totals. `load_tomorrow` / `load_today_remaining` = profile +
  planned (today counted from the current quarter); `base_tomorrow` and
  `planned_tomorrow` split it. The sensors expose the lists as attributes, which are
  kept out of the recorder. The Energi dashboard has a "Prognos" view
  (ApexCharts) that draws them.
- **Yesterday's forecast** is rebuilt on every update instead of stored: the load
  profile only uses earlier days and the calendar still has yesterday's events, and the
  PV forecast is Open-Meteo's day-before run (`previous`, hence `past_days` 15)
  calibrated on the 14 days before yesterday (empty with the Forecast.Solar fallback).
  Output: `yesterday_starts`, `yesterday_load` (profile + planned), `yesterday_pv` and
  the totals `yesterday_{load,pv}_kwh` / `yesterday_{load,pv}_actual_kwh` (actual load
  without EV). The totals are recorded, so forecast accuracy builds up in history.

## Battery plan

`battery_optimizer.optimize`, published as `sensor.energy_planner_battery_plan`.
The plan is not written to the slots, because it changes every quarter. Instead the
dynamic planner writes one `auto` slot, and `sensor.energy_planner_battery_action`
resolves what to do now (`battery_action.resolve`): a running manual slot wins; an
`auto` slot follows the plan (self-use if the plan is stale, see `plan.py`, or ends),
unless `enabled` is off; otherwise self-use. An inverter script follows
the action sensor (`examples/battery/follow_battery_action.py`). Manual slots are
also forced into the plan (`forced`), so it plans around them, also while `enabled`
is off.

- Horizon: the current quarter until the last quarter with both a price and a load
  forecast. Load = forecast profile + planned + reserve; PV = forecast.
- The day after the published prices uses an estimate (`price_estimate.estimate`,
  `_estimated_prices`): tomorrow until its prices are published (about 13:00), then
  the day after tomorrow (`ESTIMATE_MAX_LEAD`), so the plan always sees a day past the
  coming night and does not value the energy left at that night's cheapest price.
  Below "today" and "tomorrow" are the last published day and the estimated one. Per
  hour: the mean of
  today's price and the last 7 days of tomorrow's type (workday, or weekend and
  Swedish holiday incl. Midsummer, Christmas and New Year's Eve, within 21 days), plus
  the median error of that estimate for the same hour and type change in the history,
  shrunk by n / (n + 20). Then the gap from today's last quarter price to the first
  hour's estimate is added, fading as exp(-(h + 0.5) / 12) (`GAP_TIME_CONSTANT`), so
  the night does not jump at midnight (hourly error 30 -> 25 öre/kWh, 00-07 29 -> 18,
  SE3 May 2025 - Oct 2026; plan cost not backtested). Then a wind correction
  (`wind_correction`): per hour, coefficients (least absolute error, no intercept,
  fitted each day on the last 365 days' estimate errors) times tomorrow's change in
  wind power against today for the Swedish sites, the Danish/German ones and that
  hour over all `WIND_SITES` (8 sites, Open-Meteo's 100 m wind made as many days
  before as the estimated day is ahead, `previous_day1` or `previous_day2`, fitted on
  the same lead; `_wind_forecast`, one request; without it the estimate goes on
  without wind, `wind` in the cache says which). Hourly error 25.4 -> 23.3 öre/kWh (426 days,
  both halves better, mostly the morning and evening peaks); Stockholm temperature,
  German sun, fitted blend weights, a season term and gradient boosting gave
  nothing more (Oct 2026). History (400 days, `price_estimate.merge`, per day the
  first complete source): the plan's own copy of each day's hourly mean prices
  (`_record_prices`, `.storage/energy_planner.price_history`, from the plan's first
  run), the Nord Pool sensor's hourly statistics (its last state per hour) and
  Energy-Charts' day-ahead prices for the zone (`_energy_charts_history`, EUR/MWh, one
  request); the last two scaled to SEK/MWh excl. VAT by the known prices of
  yesterday and today (`scale_to`). The sensor's statistics lose 14-24 every day once
  its attributes pass HA's 16 KB limit (tomorrow's prices in SEK/kWh; the SE4 houses
  Oct 2025 - Oct 2026, only about two days after a restart were complete), so without
  the other sources there were no same-type days. Backtest on Energy-Charts prices
  (520 days): SE4 33.1 -> 29.4 öre/kWh with wind, SE3 26.3 -> 23.8; each step helps in
  both zones. Calculated once per day and lead (`price_estimate` in `hass.data`, with
  `day`, `lead` and `sources`: days per source; logged), so the evening's estimate of
  the day after tomorrow is made again at midnight with a fresher wind forecast. The
  plan publishes `prices` (spot incl. VAT, öre/kWh) and `estimated` per quarter; the
  real prices replace the estimate when `tomorrow_valid` triggers a new plan.
  Estimating the day after tomorrow from 13:00 (Oct 2026; receding horizon replanned
  every 2 h, real load, perfect PV, estimates without wind): the owner's house
  (487 days from 2025-06, power charges) 54 kr less over the period, Oscar's (593
  days, E.ON, no power charge) 20; always planning to the end of the day after
  tomorrow (two estimated days in the morning) 38 / 20, the real prices for that day
  31 / 52. So the gain is small and hardly depends on the estimate: with the fees,
  efficiency and wear, storing for the day after pays only on a cheap night before a
  much dearer day. It is there so the evening's plan already shows the night's
  charging instead of changing at midnight. The plan takes up to about 60 % longer
  in the evening (owner about 2 -> 3 s per run).
  Backtest (SE3, 778 mornings May 2025 - Oct 2026, the plan made at 07:00 followed until
  13:00, real load and PV): a plan ending at midnight cost 0.31 kr/day more than knowing
  tomorrow's prices, the simple mean of today and the last 7 days 0.15, this 0.10 (no
  losses in winter, where the simple mean lost on Sunday -> Monday). Hourly error 23
  öre/kWh (simple mean 25). A weather model (day-ahead wind, temperature and sun for
  11 sites in the Nordics and Germany, gradient boosting) predicted prices better (21)
  but planned no better (0.18); adjusting for tomorrow's sun alone changed nothing.
  Most of the gain is in March and Sep-Oct, when solar excess meets prices the plan
  cannot see yet.
- Modes per quarter, matching what the inverter scripts can do: `self_use` (battery
  covers the load, stores PV surplus), `hold` (keeps the energy but shaves import
  above the quarter's target, like the "Peak +/-" automations), `sell_excess` (PV
  surplus goes to the grid instead of the battery, slot state `sell-excess`: worth it
  while prices fall during the day and the battery will fill later anyway; a full
  battery exports in `self_use`, which wins ties), `discard_excess` (like `self_use`,
  but surplus the battery cannot take is curtailed, slot state `discard-excess`: when
  the export price, spot plus the network compensation, is negative; the plan also
  empties the battery before such hours when that pays), `charge` (from PV and grid
  up to the import target), `sell` (full discharge to the grid).
  Slot states (`battery_action.SLOT_STATES`): `self_use` → `discharge`, `hold` →
  `pause`, `sell_excess` → `sell-excess`, `discard_excess` → `discard-excess`,
  `charge` → `charge`, `sell` → `sell`.
- Mode changes cost `SWITCH_COST` (0.05 SEK): the dynamic program's state is the SOC
  and the previous mode, and the first quarter's previous mode is the one the last
  plan has for now, so a recalculation only changes the running mode when that pays.
  Modes that do the same in a quarter (`sell` with an empty battery and
  `sell_excess`) are named after the simplest.
- Costs: import at `spot * 1.25 + tariff.buy_fee`, export at `spot +
  tariff.sell_fee`, import above `grid_import_limit` at `LIMIT_PENALTY` (20 SEK/kWh, so
  only when unavoidable), import above the main fuse (`main_fuse` × 3 × 230 V,
  `tariff.fuse_power`) at the same penalty, for all load (the car and the heat pump
  too), battery wear `battery_wear_cost` per kWh taken out of the
  battery, energy left at the end valued at the cheapest buy price in the horizon's
  last 24 h (times the discharge efficiency) minus the wear: it can be refilled then,
  so a higher value (the median price was tried) makes the plan hoard energy and
  charge at the end. Wear only changes decisions that add a cycle (sell and refill), since energy
  kept is also taken out later; it stops trades that gain only a few öre.
- Power charges: each charge and month gets an import target (kW), starting at the
  peak already reached this month (from the hourly changes of `grid_import_sensor`,
  default the HAN meter `sensor.matarstallning_aktiv_energi_uttag`): import up to it is
  free. A coordinate search raises the targets in 0.5 kW steps while the cost drops.
  `max_charge_current` is only the hardware limit; the target sets the charging speed.
  Raising a peak is charged at the share of the month's remaining hours the plan
  covers, because the peak is then free for the rest of the month (`_month_share`): a
  higher peak is accepted early in the month, hardly at the end.
- Reserve: the plan keeps `battery_reserve_soc` (a soft minimum above the shutdown
  SOC) for load the forecast misses, e.g. a washing machine. `Battery.reserve_kwh`:
  no mode discharges below it, a SOC below it (after a peak) is allowed, and the
  energy missing at the end is valued at the end price, so the plan refills it. The
  plan publishes `peak_levels` (the quarter's power charge level, kW, without the
  household import limit, capped at the main fuse). When the live SOC is at or below the reserve, the action
  turns the plan's `self_use`, `sell_excess` and `hold` into `hold` with that level as
  import target (`reserve: true`): the inverter's peak shaving only uses the reserve,
  down to the shutdown SOC, for import that would raise the power charge or trip the fuse; without a
  power charge or fuse the battery is just kept. Without a SOC reading the action
  uses the SOC the plan expects now (`Plan.soc_at`), not 0, which would look like an
  empty battery.
- Car: `optimize(vehicle=Vehicle(...))` plans an EV with the battery (in HA see
  "Car" below). `Vehicle`: charger powers (`charger_powers(min, max)`, 0 or one
  of them per quarter), the energy needed at the plug, when it is plugged in and the
  deadline (quarter index; past the horizon: the part the charger can still deliver
  after it costs the end price). Missing energy costs `shortfall_price` (10 SEK/kWh).
  The dynamic program's second state is the energy delivered (0.5 kWh steps); the
  car is exempt from the household import limit but not from the power charge
  levels or the main fuse. The import targets include the car's planned charging
  and are capped at the fuse, so the inverter's peak shaving slows the battery's
  charging while the car charges. The target search uses 2 kW steps, then 0.5 kW around the best
  (`_candidate_levels`, same results, half the time). Searching the levels with the
  car as a fixed load was 2-3 times faster but lost up to 3 kr/day, so every search
  run is joint. Plan fields: `ev_kwh`, `ev_missing_kwh`, `ev_missing_cost`.
- Battery: capacity, shutdown/max SOC, charge/discharge current × the live voltage
  (`battery_voltage_sensor`), SOC (`battery_soc_sensor`), round-trip efficiency
  `battery_efficiency`. About 1 s per day of quarters, run in the executor.
  The efficiency was measured from the recorder's hourly statistics (Oct 2026): night
  runs of 3 h or more without PV, the battery only charging or only discharging above
  0.8 kW, SOC 15-95%; the Solis AC port energy against the SOC change, charge leg times
  discharge leg (the capacity cancels). The inverter's own draw (50-70 W, paid whether
  the battery cycles or not) is left out. Owner 0.91 (0.86 with it), parents 0.94,
  Oscar 0.87, Kidde 0.85; set to 91, 94, 87 and 85. The Solis battery power is the AC
  port power plus a constant on the owner's inverter, so it cannot split inverter
  and battery losses; early months with few charge runs or a changed SOC scale were
  left out (parents before 2025-04, Oscar before 2025-10, Kidde before 2025-08).
- The inverter follows the import target while charging and in `hold` with Solis peak
  shaving (`examples/battery/follow_battery_action.py`; works while no time-of-use window is
  active): `number.solis_s6_eh1p_peak_max_usable_grid_power` = the import target, and
  `number.solis_s6_eh1p_peak_baseline_soc` = the SOC to charge to (for `hold` at most
  the live SOC, so it does not charge from the grid). Below the max grid power the
  inverter charges up to the baseline; above it the battery covers the load. The
  S6-EH3P does not keep to the max grid power while charging (19 kW from the grid with
  it at 7.8 kW, Oct 2026), so with `DISPATCH = True` the script runs every mode
  through Solis Remote Dispatch instead (kept in RAM, not flash: `battery_charge` with
  an import limit, `battery_discharge`, `hold` as `grid_import` without grid charging
  (in daylight `battery_charge` without import, which never discharges: `grid_import`
  curtails the PV the house does not need after the target, so it runs only while the
  house is above the PV plus the target, until the battery stops discharging),
  `feed_in_priority` for `sell-excess`, `self_consumption` with export limit 0 for a
  full battery in `discard-excess`; self-use without dispatch; repeated every minute
  with a 3 min failsafe; needs register 34502 = 0xAA55). Tests and register map: `docs/solis_remote_dispatch_2026_10.md`.
  Without dispatch, `PEAK_SHAVING_CHARGE = False` charges in the time-of-use window at
  the action's current (the plan's energy over the run) instead of peak shaving, so
  the import is the house plus that current (Kiddes: S6-EH3P without dispatch; the house
  and the car share a 16 A fuse, the battery has its own, the connection is 35 A, so
  `main_fuse` is 35).

## Heat pump

The air-to-air heat pump (`climate.hallen`, Mitsubishi MSZ-AP42VG) should only heat
when that is cheaper than district heating. `heat_pump.evaluate` compares a kWh of
heat: district heating at the season's energy price against electricity / COP.
`sensor.energy_planner_heat_pump_saving` is district heating minus heat pump heat
(öre per kWh of heat, positive: run the heat pump), with the COP, prices and
`break_even_spot` in the attributes. Updated every minute.

- COP: the service manual's curves at the compressor's rated frequency, defrost
  included (OBH789, 9-1): about 3.0 from −15 to +5 °C, 3.6 at +7, 4.05 at +16; below
  −15 °C the heat pump does not run (`None`). Conservative: at low output the COP is
  higher (SCOP 4.7). The owner chose the conservative curve.
- District heating, Tekniska verken 2026 incl. VAT: 99.7 öre/kWh Dec–Feb, 76.7 Mar,
  Apr, Oct, Nov, 13.0 May–Sep. The annual fee (1 350 kr + 370 kr per MWh used
  Dec–Feb, at least 6 900 kr) adds nothing per kWh here: the owner uses below 15 MWh
  in Dec–Feb (2026: Jan 2.53, Feb 2.42 MWh, from the bills). These sensors
  replace the MQTT feed (`sensor.fjarrvarme`, stuck since 2025) and the old heat pump
  automations, removed in Oct 2026.
- Electricity (saving sensor): spot × 1.25 + `tariff.buy_fee`, or, for the part of
  draw the battery plan exports this quarter, spot + `tariff.sell_fee`.
  Power charges are not included.
- Break-even with COP 3: spot about 134 öre/kWh in Oct–Nov and Mar–Apr, 189 in
  Dec–Feb; in summer only exported solar pays.
- Draw: MELCloud's counter (`sensor.hallen_energy`) is useless, it showed 0.6 kWh for
  a night the house load showed about 3.3 kWh (Oct 2026, 9 °C out: about 750 W the
  first hour, then 400–600 W). Last winter's data could not give a curve either (it
  only has that counter, and daily load varies ±6 kWh). So the draw is modelled:
  heat = loss × (indoor − outdoor), draw = heat / COP (`heat_pump.draw`, at most
  2 kW), with indoor `sensor.gw1100a_indoor_temperature` (in the hall; MELCloud's own
  room temperature reads the warm air at the unit, 16 → 23 °C while the room went
  21.5 → 21.9). The forecast measures `loss` (kW of heat per °C): hours
  `climate.hallen` heated at least 75% of, the load against the median of the same
  hour on days it did not heat (35 days of history, `forecast.heat_pump_excess`,
  capped at 2.5 kW), × COP / (indoor − outdoor), the mean over at least 6 hours with
  a difference of 3 °C or more (`heat_pump.heat_loss`). The first night gave about
  0.2 kW/K warming up and 0.14 later: 0.4 kW at 10 °C, 1.5 kW at −5 °C. The plan uses
  it per quarter with the live indoor and the forecast outdoor temperature, the saving
  sensor with both live (`heating._power_kw`); without a measurement or indoor
  temperature `heat_pump_power` (W) is used, and 0 turns planning off (the
  forecast then keeps the heat pump's draw in the house load; `heat_pump.power_kw`
  serves both). The draw per
  hour is also taken out of the load history. Not modelled: the setpoint (the room
  hardly reaches it, and it can not be fitted from a night; a changed setpoint shows
  in the measurement over the following days), warming up after an off period, and
  the living room next door (`climate.vardagsrum`, not in the statistics). Once the
  heat pump has run every day for 35 days there are no days without it to compare
  with, and the setting is used again. Its power peak costs little (0.6 kW × 45 kr/kW
  in winter days) and it nearly always pays for it.
- In the battery plan (`async_update_plan`): a first run prices the mean draw as more
  load per quarter (`Plan.marginal`, öre/kWh: buy price, lost export, or the battery
  energy it uses, plus power charges). `heat_pump.schedule` turns it on where the
  outdoor temperature (forecast, else live) is below `heat_pump_heating_limit` and
  `district − marginal / COP ≥ heat_pump_margin`, without runs or gaps under 30 min.
  A second run plans the battery with that load added and `exempt` from the import
  limit (only power charges limit it). The plan publishes `heat_pump`, `marginal`,
  `temperature`, `heat_pump_cop` and `heat_pump_power` (kW while heating) per
  quarter.
- `sensor.energy_planner_heat_pump_action` (`on`/`off`): the plan now (source `plan`,
  `until` the end of the run); if the plan is stale or missing (e.g. no
  battery SOC), the saving sensor against the margin and the heating limit (source
  `fallback`); without a published spot price now or an outdoor temperature (just after a restart) no
  state (source `none`), so the
  automation leaves the heat pump alone.
  `examples/heat_pump/follow_heat_pump_action_automation.yml` sets `climate.hallen` to `heat` or
  `off` (only from `off` / `heat`, so cooling is left alone); the target stays at
  24 °C for now, to see how much it changes the use.

## Car

While the car is plugged in, the battery plan plans it with the battery (`_vehicle` in
`battery_plan.py`, `Vehicle` in the optimizer): its need by the next
`time.energy_planner_ev_ready_time` (default 06:30) on a working day
(`price_estimate.is_offday`: weekends, holidays and the eves that price alike), or by
`datetime.energy_planner_ev_departure` if that is earlier and still ahead (a trip; past
ones are ignored, so it needs no clearing). Plugged in on Friday, the car charges in
the weekend's cheapest hours; past the horizon the optimizer only charges where it
beats the end price. At
`number.energy_planner_ev_charger_max_power` (default 11 kW; with
`ev_charger_min_power` > 0 also powers in between). Config store only:
`ev_charger_status_sensor` (no default: without it the car is not planned; any state
but `disconnected`/`unavailable`/`unknown`/`off` means plugged in, so Zaptec's
`connected_*`), `ev_session_energy_sensor` (kWh since plugged in),
`ev_soc_sensor` / `ev_charge_limit_sensor` (%, with `ev_battery_capacity`, kWh).

- Need: (limit − SOC) × capacity / 0.9 when the car reports both; otherwise the
  largest ended charging night (noon to noon) in the 35 days of `forecast_ev_sensor`
  history (`ev_need_kwh` in the forecast) minus the session's energy; for a trip (the
  departure is the deadline) a full battery (`ev_battery_capacity` / 0.9, source
  `trip`) minus the session's energy, as the car stops at its own charge limit: the
  owner's parents expect a full car by the departure. Overestimating
  is cheap: a full car stops by itself, and the rest of the planned quarters pass
  without charging. Zaptec's `connected_finished` does not mean a full car: it is also
  the state after the charging switch pauses it, so a car skipped while `finished`
  (tried Oct 2026) made the plan and the automation pause and resume it about 80 times
  in 15 minutes, after which the switch stayed unavailable and the car did not charge
  that night. Underestimating is not (the plan would pause a car that is not
  full; the 90th percentile was 19 kWh where nights reach 33), so once the session
  has taken more than that, or without either, the car is not planned and the
  action falls back to charging. The plan publishes `ev` (None while unplugged): `power` per quarter (kW),
  `need_kwh`, `need_source` (`soc`/`history`/`trip`), `ready` (the deadline, also past the
horizon), `planned_kwh`, `missing_kwh`.
  Plugging in or out and the car settings (incl. the departure) trigger a new plan.
- `sensor.energy_planner_ev_action`: `charge`/`wait` from the plan (source `plan`,
  `until`, `power_kw`); without a fresh plan `charge` (source `fallback`, the car
  charges as without the planner); unplugged no state. Its attributes also carry the
  (fresh) plan's `need_kwh`, `need_source`, `ready`, `planned_kwh` and `missing_kwh`.
  `examples/ev/follow_ev_action_automation.yml` starts/stops a Zaptec with its charging
  switch.
- The owner's parents (Oct 2026): Tesla on a Zaptec (`sensor.garaget_laddstatus`,
  `sensor.garaget_session_total_charge`, `switch.garaget_laddar`), plugged in about
  16-19 until about 06:45, charging 1.5-2 h at ~11 kW at once; median night 18 kWh,
  90th percentile 33 (2025-08 - 2026-10). Moving the evening charging to the night's cheapest hours saved
  about 57 öre/kWh on the nights with prices (~1 500 kr/year). The car's SOC is to
  come from ESPHome Tesla BLE (M5Stack NanoC6).

## Main fuse

The battery plan keeps each quarter's mean import below `main_fuse` × 3 × 230 V, as if
the load were even over the phases. It is not: at the owner's parents (35 A, Oct
2026) the battery charged from the grid at 20 kW with the phases at 25, 27 and 35 A
(most of the house is on L3). So a guard (`planner/fuse.py`, glue `fuse_guard.py`)
works on the live import per phase, with the two loads that can be controlled, both
three-phase and balanced: the car charger's current and the battery's grid charging
(the inverter's peak shaving max usable grid power, a limit on the total import).

- House load per phase = the phase's import − the inverter's draw / 3 − the car's
  current; the room per phase = (`main_fuse` − `fuse_margin`) × 230 V − that, the
  busiest phase sets it (the sensor's state, A).
- The car comes first while `ev_action` is `charge`: its current (`ev_current_a`) is
  raised to the room up to the charger's highest (`ev_charger_max_power` / 690,
  rounded: 16 A), and lowered only while a phase is over and the battery can not take
  enough off, by the excess; below 6 A it is 0 (the automation pauses the car).
  Zaptec reports the car's current about once a minute and the meter every 10-20 s,
  so a car that just started looks like house load; lowering only on a real excess
  keeps that from cutting the car. While the car should not charge, or has finished
  (`ev_charger_status_sensor` ends in `finished`), no room is kept for it and its
  setting is left alone.
- The battery gets the rest: `grid_limit_kw` = house + car + the room left per phase
  (after the car's setting) × 3. Above it the inverter's peak shaving covers the load
  from the battery, so it also shaves house peaks in `charge` and `hold`. With
  `inverter_grid_power_sensor` (the import the inverter's own meter sees) the limit
  is shifted by what that meter misses (the parents': about 1.3 kW,
  `inverter_meter_offset_kw`).
- `fuse.smooth`: lower values apply at once (the grid limit 0.2 kW lower, so small
  rises do not write again), higher ones once a step higher (0.5 kW, 1 A) and the last
  change is 1 min (grid) / 15 min (charger, Zaptec's cloud API is rate limited) old.
- Updated on every change of the configured sensors (at most every 5 s) and every
  minute; a missing phase, or a meter silent for 2 min, keeps the last result (any
  of its phase sensors counts, since MQTT does not write an unchanged value).
  `sensor.energy_planner_fuse_guard`: state the room (A), attributes `grid_limit_kw`,
  `ev_current_a` (None without `ev_charger_current_number`), `phase_a`, `house_a`, `over_a`, `updated`.
- `examples/battery/follow_battery_action.py` caps the import target at `grid_limit_kw` (a
  guard older than 2 min is ignored) and its automation triggers on it;
  `follow_ev_action_automation.yml` sets the charger's current and pauses at 0. Not
  covered: `self_use` (the battery already covers the load) and manual time-of-use
  charge slots without an import target. Peak shaving does not limit the S6-EH3P's
  grid charging (see "Battery plan"), so the cap only works with the script's
  Remote Dispatch, which the parents' firmware lacks so far.
- The parents: phases `sensor.l{1,2,3}_aktiv_effekt_uttag_momentan_effekt` /
  `_inmatning_` (HAN, kW), inverter `sensor.solis_s6_solis_ac_grid_port_power` (W,
  negative while charging) and `sensor.solis_s6_eh3p_grid_power_net` (W), car `sensor.garaget_current_phase_{1,2,3}`, setting
  `number.garaget_charger_max_current` (the installation's available current is
  unavailable to their account).

## Comfort (work in progress)

Indoor temperature schedules with optimum start, in `comfort/` (pure, tested, not
wired into HA yet). `schedule.py`: profiles (points of time and °C, optionally ramping
to the next) per room group (`living`, `bedrooms`, `offices`) and day kind
(`workday`, `offday`, `work_from_home`, `away`); `day_kind` picks the kind from an
override, calendar words (`borta`, `hemifrån`, ...), offdays and presence.
`thermal.py`: a first-order model per room (`loss`, `gain`, `heat` in °C/h at full
heating), `fit` from hourly samples, `plan` sets the setpoints so a higher target is
reached on time without overshoot.

The house (owner, Oct 2026): district heating through a Siemens RVA36.531 controller
(outdoor curve, mixing valve) feeds radiators with Tado valves (`climate.sovrum`,
`gastrum` = Elise's room, `arbetsrum_uppe` with a faulty valve, `arbetsrum_kallaren`,
`room_5` = workshop, unavailable) and water floor heating zones over MQTT
(`climate.gillestugan`, `tvattstugan`, `vardagsrum`, `badrum`; the `heatfloor` bridge
writes the setpoint to the floor controller at once, the battery room thermostats show it
some minutes later). The hall has the heat
pump and 4 m² of electric floor (Ebeco, `climate.hallen_2`, left alone: electricity at
COP 1); its temperature is `sensor.gw1100a_indoor_temperature` (the Ebeco and MELCloud
sensors read the outer wall or the unit's air). Doors are mostly open, the plan is
open. Radiator supply/return: `sensor.utgaende_radiatortemperatur` /
`retur_radiatortemperatur` (since 2023): supply about 31 °C (its floor) above +5 °C
outdoor, 40 at 0, 43 at -10, 50 at -20.

Last winter's Tado data: with the valve closed rooms cool 0.03-0.10 °C/h at 20 °C
(time constant 230-270 h, but the rooms around them were warm; with the whole house
cooling it is 48-100 h, see below); with it fully open they rise at most about 0.3 °C/h, some
not at all in the cold (Elise's room, the basement office), so the radiators, not the
control, limit setbacks. The heat pump is the only fast source. A weekend step test
(2026-10-09 - 11, `packages/heating_test.yaml` in HA, a copy in
`examples/comfort/heating_test_package.yaml`, phase in
`input_select.heating_test_phase`): everything off, then district heating only, then
the heat pump only, to fit the rooms and how heat spreads. Results in
`docs/heating_step_test_2026_10.md`: with the whole house cooling, the rooms above
ground lose 0.010-0.021 /h (48-100 h), the basement 0.004; the supply stays at the
curve's floor (about 32 °C) whatever the valves do, so 17.6 -> 20.1 °C took 20 h and no
room reached 22; the Tado sensors read 1.5-3 °C high while their valve heats; the heat
pump drew about 0.4 kW and held the hall at about 20.5 °C with a 22 °C setpoint.
Tekniska verken's daily district heating values: about 3.4 kW in B (all the
radiators give at the curve's floor), and the house as one zone loses about
0.19 kW/K with about 18 kWh/K stored (last winter's bills agree).

The RVA36 (manual SE1G2455): curve slope row 17, parallel shift row 64, min/max supply
rows 68/69, its own time program rows 5-11 with reduced setpoint row 14, summer/winter
limit row 16. Room influence and optimum start/stop (rows 65, 73, 74) need a QAA50
room unit on the PPS bus. Input H1 (a potential-free contact, row 96) switches the
operating mode, or with row 96 = 2 forces a minimum supply setpoint (row 97): a relay
on H1 gives HA a boost or a setback. The outdoor sensor is a QAC32 NTC on B9; a
resistance emulator there (e.g. Ohmigo) would let HA set the supply temperature
freely.

## Household and tariff

Background for the planner (from the owner, Oct 2026):

- Location Linköping (58.38 N, 15.66 E), price area SE3, grid operator Tekniska verken.
- Main heating is district heating (fjärrvärme). An air-to-air heat pump ("Hallen",
  MELCloud `climate.hallen`, energy `sensor.hallen_energy`) can heat or cool part of
  the house; it should heat when that is cheaper than district heating (see "Heat
  pump").
- Electricity is bought at the quarter-hour spot price plus network fees.
- Network tariff: Konsumtionsabonnemang, "Prislista alternativ" (time-differentiated),
  prices from 2026-01-01 incl. VAT
  (https://www.tekniskaverken.se/privat/elnat/priser-ersattningar):
  - Transfer fee 18.0 öre/kWh day (06–23), 9.0 öre/kWh night (23–06).
  - Energy tax 45.0 öre/kWh (incl. VAT) on bought energy.
  - Power charge per month on the highest *hourly average* power, separately for day
    (06–23) and night (23–06): day 23 kr/kW in summer (Apr–Oct) and 45 kr/kW in winter
    (Nov–Mar); night 8 kr/kW summer, 12 kr/kW winter. One day peak and one night peak
    per month. Example: an 8 kW sauna hour on a winter day costs 8 × 45 = 360 kr that
    month, so shaving peaks with the battery is valuable.
  - Main fuse 20 A: fixed fee 230 kr/month (three-phase 20 A is about 13.8 kW, a
    practical ceiling for planned charging plus house load).
- Selling: network compensation 7.20 öre/kWh in high-price periods (Mon–Fri 06–22,
  Jan–Mar and Nov–Dec), 5.10 öre/kWh otherwise (from 2026-05-01), plus the spot price
  from the electricity retailer. So selling earns spot + 5.10/7.20 öre, while buying
  costs spot + transfer fee + energy tax (+ the power charge), a spread of roughly
  50–60 öre/kWh: storing own solar for later use beats selling it unless the spot
  price difference is large.
- Large loads: electric sauna ~7 kW, ~4.6 kWh per session, mostly Sunday 19–21 on
  about a quarter of Sundays. EV charger (Easee) 7–9 kW, rarely used.


## Grid tariffs

Users have different grid operators, so fees and power charges come from the tariff
(`planner/tariff.py`), chosen in the integration options. Owner: preset
`tekniska_verken_alternativ`, `grid_import_limit` 1 kW 06–23. The limit is meant to
keep daytime import at the house's base load: with a 45 kr/kW winter day power charge,
charging the battery from the grid in the day costs far more than at night. The heat
pump's load is exempt from it (Oct 2026): its saving pays for the higher peak. Later, the
planner should also be able to shift controllable loads (e.g. the heat pump) to stay
under it.

The owner's brother uses E.ON Energidistribution (Virserum), preset `eon_20a` from his
price list: 32.30 öre/kWh transfer fee (+ 45 öre energy tax, assumed), 835 kr/month,
no power charge, selling spot + 10 öre. He has separate meters on the spa and the
ground-source heat pump, which could later feed load patterns. His forecast (backtest
Oct 2026): the load follows the outdoor temperature (about -2.2 kWh/day per °C, 20
kWh/day above 15 °C, 66 below 0), so he uses the `temperature` model (daily error 22% ->
19%, Nov-Mar 18% -> 14%), with the Wallbox power as `forecast_ev_sensor` and, until he
has an outdoor sensor, a template sensor from his SMHI weather entity (history imported
from Open-Meteo). PV: the inverter clips at about 10 kW, "Platta" is shaded in the
afternoon (`hourly` calibration) and "Frigge 1/2" seem faulty, so their Forecast.Solar
entries are disabled. The model covers the common Swedish variants: mean of the N highest hourly means per
month, optionally at most one per day, limited by months, weekdays and hours.
Holidays are not detected.

## Domain facts worth knowing

- Prices are 15-minute resolution. "Hours" settings are multiplied by 4 to get a
  quarter count.
- Area and currency are parsed from the Nord Pool entity id by position:
  `entity_id.split("_")[2]` and `[3]` (e.g. `sensor.nordpool_kwh_se3_sek_…`).
- Requires the `nordpool` custom integration for the `nordpool.hourly` service.
- Nord Pool prices are SEK/MWh excluding VAT; the network settings are öre/kWh
  including VAT (`* 1.25` adds VAT, `* 10` converts öre/kWh to SEK/MWh).

## Common changes

- **New setting:** add a `Setting` to `SETTINGS` in `planner/settings.py` (its
  default, platform, and `replans` if the battery plan reads it; a test checks that),
  then, for an entity, a definition dict without `"default"` in the platform file with
  `"data_store": "config"` and the setting's key as `"id"`. Read it with
  `settings.get(config, "<key>")` / `settings.number(...)` or a typed group, never
  `config.get` with a default of its own. Defaults are only written to the store when
  the key is missing.
- **Removed setting or entity:** add it to `migrate.REMOVED_ENTITIES` (or
  `RENAMED_SETTINGS`), so stored values and registry entries are cleaned up.
- **New slot state:** update the lists in `select.py`, the validation in
  `add_slot_service`, `services.yaml`, SOC handling in `manual_slots.py`, and the
  consumers in `examples/`.

## Commands

```bash
make install        # uv sync (creates .venv from pyproject.toml + uv.lock)
make test           # pytest tests/ -v
make lint           # ruff check custom_components/energy_planner/ tests/
make check          # all checks: scripts/check.sh
make hooks          # enable the git hooks
make format         # ruff format
./local_deploy.sh   # deploy to local ha_demo and restart it
```

- Dependencies are managed with uv: runtime ones under `[project]` and test/lint
  ones in the `dev` group of `pyproject.toml`, locked in `uv.lock`. Add with
  `uv add <pkg>` or `uv add --dev <pkg>`; run tools with `uv run <cmd>`.
- Python is 3.14 (`.python-version`, `ruff.toml`; required by HA), HA pinned to `~=2026.9.4`.
- Ruff rule set is strict (`D`, `S`, `T20`, `N`, `RET`, `SIM`, …): public functions
  need docstrings and `print` is rejected.
- pytest uses `asyncio_mode = auto` and `pytest-homeassistant-custom-component`.
  Tests mock `hass` with `MagicMock`, patch `fetch_nordpool_data` and patch
  `dt_utils.now` where the time matters.
- Manual slots (`add_manual_slots`) are untested.
- CI: `.github/workflows/validate.yml` (ruff, hassfest, HACS) and `tests.yml`
  (pytest on 3.14 + ruff).

## Before committing

- `scripts/check.sh` must pass: `ruff check`, `ruff format --diff`, `ty check` and the
  tests, on `custom_components` and `tests` (`make check` runs it too, and so does CI).
  Fix the code rather than silencing a check; if an ignore is unavoidable, explain it in
  a comment (see `store.py`).
- Commit messages follow Conventional Commits
  (https://www.conventionalcommits.org/en/v1.0.0/): `<type>[(scope)][!]: <description>`
  with type one of `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci`,
  `chore`, `style`, `revert`, e.g. `feat(forecast): add Open-Meteo PV forecast`. Use the
  body for the why.
- Git hooks in `.githooks/` enforce both: `pre-commit` runs `scripts/check.sh`,
  `commit-msg` checks the message format. Enable them once per clone with
  `make hooks` (`git config core.hooksPath .githooks`).

## Known quirks and pitfalls

- Manual slot times and `add_slot` input are interpreted in HA's configured time
  zone. `Europe/Stockholm` is only hardcoded for the Nord Pool publish time.
- Compare and sort instants, never local times in one zone: Python compares those
  by wall clock and ignores `fold`, so the hour repeated when DST ends sorts into
  its copy, and adding a timedelta keeps the wall clock. So slot and manual slot
  times are UTC; the plan's and
  the forecast's starts are local and compared by timestamp (`plan.py`). Local time
  is only for calendar logic (day windows, hour of day) and display.
- Dates and times in `values`, `config` and `manual_slots` are always objects in
  memory (`parse_stored_data` converts them on load), so do not add string checks.
  The `nordpool_values` price cache is the exception and still holds ISO strings.
- An `off` slot can appear in the middle of the list when manual slots leave a gap
  (for example a manual slot before the first planned slot). Unused slots are the
  ones whose start is `None`.
- Card labels and some example entity names are in Swedish.
