# Architecture: layout, data model and control flow

## Layout

| Path | What it is |
| --- | --- |
| `custom_components/energy_planner/__init__.py` | Setup, `hass.data` structure, the manual slot services (`async_setup`), timers and plan triggers (`async_setup_entry`) |
| `custom_components/energy_planner/planner/` | All planning logic (see below) |
| `custom_components/energy_planner/button.py` | `button.energy_planner_update_battery_plan`, recalculates the battery plan |
| `custom_components/energy_planner/{datetime,number,select,switch,time}.py` | Entity platforms; each holds its entity definitions as dicts plus one generic entity class |
| `custom_components/energy_planner/sensor.py` | The two forecast sensors (`sensor.energy_planner_load_forecast`, `sensor.energy_planner_pv_forecast`) `sensor.energy_planner_battery_plan`, the three action sensors in `ACTION_SENSORS`, one class: `battery_action` (what the inverter should do now), `heat_pump_action` and `ev_action`, the heat pump sensors `heat_pump_cop`, `district_heating_price`, `heat_pump_saving`, all read from the `actions.Actions` resolved together (`battery_plan.actions_now`), and `fuse_guard` (`fuse_guard.update_guard`); they only read `hass.data` |
| `custom_components/energy_planner/store.py` | Thin wrapper around HA `Store` (`.storage/energy_planner.<key>`) |
| `custom_components/energy_planner/config_flow.py` | Setup asks for `nordpool_entity_id`; the options flow chooses the grid tariff (flat, a preset or custom YAML) |
| `custom_components/energy_planner/services.yaml`, `translations/en.json` | Service descriptions |
| `custom_components/energy_planner/migrate.py` | Moves stored settings of older versions to the current keys (`migrate_config`, on load) and removes the registry entries of entities that are gone (`remove_entities`, on setup) |
| `tests/` | `planner/`: the planning modules and their HA glue; `comfort/`; at the top config flow, entities, services, triggers and the migration. `tests/README.md` says how they reach HA |
| `examples/` | Scripts, automations and cards that consume the actions, by topic: `battery/` (the inverter scripts, incl. Remote Dispatch and its test), `ev/`, `heat_pump/`, `comfort/` (the heating test package), `dashboard/`; index in `examples/README.md`; `energi_dashboard.yaml` (the owner's dashboard), `nordpool_reload_automation.yml`, `peak_shaving_automations.yml` (older Solis peak shaving) and `new_home_setup.md` (integrations and owner-specific entity ids, for another home) were copied from the owner's HA |
| `energy_planner_extras.yaml`, `add_slot_form.yml` | HA package + card for the manual "add slot" form; the extras file also has the car SOC card (`ev_soc`, in a comment) |
| `local_deploy.sh` | Copies the component into `~/projects/ha_demo/config` and restarts its docker compose |

### `planner/`

| File | Role |
| --- | --- |
| `__init__.py` | Re-exports `async_update_forecast` and the plan run's `async_request_plan`, `async_update_plan` and `write_plan_sensors` |
| `forecast_update.py` | The forecast's HA glue: `async_update_forecast` fetches recorder hourly statistics, the heat pump's `heat` hours, the calendar, Open-Meteo (or Forecast.Solar's estimates) into a `forecast.ForecastInputs`, builds the `Forecast` and stores it in `hass.data[DOMAIN]["forecast"]` |
| `battery_optimizer.py` | Pure battery optimizer (numpy): `optimize` picks a mode per quarter (`self_use`, `hold`, `sell_excess`, `discard_excess`, `charge`, `sell`) by dynamic programming over the SOC and searches the monthly power peak levels. See `docs/battery-plan.md` |
| `battery_plan.py` | The plan run's HA glue: `async_update_plan` updates the prices (`prices.async_update_prices`), gathers a `planning.PlanInputs` (the forecast, battery settings and state, this month's hourly grid import, the car's and heat pump's readings, the manual slots, the last plan), runs `planning.build_plan` in the executor and stores the `plan.Plan` in `hass.data[DOMAIN]["plan"]` (`finished` set when it is done); `async_request_plan` (one run at a time, requests during a run give one more); `situation` (the `actions.Situation` at a moment from `hass.data` and the readings), `resolve_actions` (once into `hass.data[DOMAIN]["actions"]` at the timer's moment, None when the situation fails; each failing part logged once while it fails), `write_plan_sensors` (resolves and writes the plan sensors that are added; never raises), `actions_now`, `car_status` (the charger's state, for the plan, the actions and the fuse guard) |
| `planning.py` | Pure: `build_plan(PlanInputs) -> Plan | None` slices the horizon (current quarter while load, PV and a price are known), forces the manual slots, plans the car (`vehicle`) and the heat pump (`heat_pump_quarters`, two optimizer runs) and converts the result to the plan's units. `CarReadings`, `HeatPumpReadings`, `battery` (the optimizer's `Battery` from settings, SOC and voltage), `soc_limits` |
| `timeline.py` | Pure: time kept as UTC instants. `Timeline` (consecutive quarters: `between`, `of`, `index_at`, `quarter_end`, `local` / `local_end` / `local_hour` / `local_date` / `isoformat` for calendar logic and display), `quarter_start` (the price dicts' key), `hour_start` (the key of hourly sums), `at_local` (a local date and wall-clock time as an instant). Its tests are the DST suite |
| `plan.py` | Pure: `Plan`, the battery plan as one object (per-quarter series parallel to its `timeline`, canonical units: spot SEK/MWh excl. VAT, SOC %, kW), `index_at`, `value_at` (a series' value at a moment), `run_at` (a `Run` of equal values from now, with `until`), `is_stale` (`STALE_AFTER` 30 min from `finished`, when the plan became available; `updated` is the moment it was planned for), `soc_at` (in the first quarter from `soc_start`, the SOC when planned), `to_attributes` (what the plan sensor publishes). `follow` (the run now, or why not: no plan, plan is old, plan does not cover now) and `action` (the keys every action has: `state`, `source`, `until`, `reason`, `plan_updated`) for the resolvers |
| `actions.py` | Pure: `resolve(Situation) -> Actions`, every action now from one situation (one moment, the plan, the manual slots, the settings, the readings): the battery's, the car's, the heat pump's, the heat pump economy (`heat_pump_economy`, worked out once) and the plan's mode. A missing reading is None; each device's rule says what it means. Each action resolves on its own: one that fails is None (its error in `Actions.errors`) and the others are kept; the heat pump's fallback has no decision without an economy |
| `battery_action.py` | Pure: `resolve` turns the manual slot covering now, the plan, the battery settings and its SOC and voltage readings into the battery action now (action state, source, SOC, current, import target, until), `ACTION_STATES` (mode → action state) |
| `manual.py` | Pure: `ManualSlot` (an action state over whole quarters, UTC, optional target SOC), `make` (rounds out to quarters), `covering`, `current` (drops ended ones), `target` (defaults and limits), `forced` (mode and target per quarter for the optimizer), `STATE_MODES` (action state → mode) |
| `heat_pump.py` | Pure heat pump economy: COP curve per outdoor temperature (`COP_CURVES`), district heating price per month (`DISTRICT_HEATING_PRESETS`), `cop_curve` (a model's), `evaluate` compares a kWh of heat from both, `action` (on/off now from a `Plan` or the economy now). See `docs/heat-pump.md` |
| `ev.py` | Pure car charging: `is_plugged` (charger state), `need_from_history` (the largest charging night, noon to noon), `need_from_soc`, `ready_time` / `deadline_index` (the next working morning's ready time, or an earlier departure; `timeline.at_local`, and the plan's `Timeline`), `ev_action` (charge/wait now, from a `Plan`). See `docs/car.md` |
| `settings.py` | Every config store key with its one default (`SETTINGS`; the config entities take their defaults from it), `get` / `number`, typed groups in domain units (`battery`, `car`, `heat_pump`, `fuse`; `car` and `fuse` are None when not set up), `replans` (the plan triggers, `replan_entity_ids`) and `reading` (a sensor's float value, None if not configured, unavailable or old) |
| `fuse.py` | Pure main fuse guard: `limits` splits the room on the busiest phase between the car charger and the battery's grid charging, `smooth` (hysteresis for the devices). See `docs/main-fuse.md` |
| `fuse_guard.py` | HA glue for `fuse.py`: live phase readings → `hass.data[DOMAIN]["fuse_guard"]`; whether room is kept for the car: from the published car action (`battery_plan.actions_now`), so the guard and `sensor.energy_planner_ev_action` agree, and the live charger state (for a car plugged in since, or with no actions, `ev.ev_action` now; none for a finished car) |
| `prices.py` | The prices' HA glue: `async_update_prices` (every plan run) fetches the published prices (`fetch_nordpool_data`), records each day's hourly prices, makes the estimate from the price history (recorder, Energy-Charts) and the wind forecast, and stores a `SpotPrices` (`published`, `estimated`) in `hass.data[DOMAIN]["prices"]`; `spot_at` (the published price at a moment), `nordpool_zone` (area and currency from the entity id). See `docs/battery-plan.md` |
| `price_estimate.py` | Pure: tomorrow's prices before they are published (same-type days + a learned correction per hour, Swedish holidays). See `docs/battery-plan.md` |
| `../comfort/schedule.py`, `../comfort/thermal.py` | WIP comfort control, not wired into HA yet. See `docs/comfort.md` |
| `tariff.py` | Pure grid tariff model: `Tariff` with `buy_fee` / `sell_fee` (öre/kWh) and `power_cost` (kr), parsed from a dict by `tariff_from_dict`, presets in `TARIFF_PRESETS`, the household's `import_limit`. Format in the module docstring |
| `forecast.py` | Pure forecast, no HA imports: `build_forecast(ForecastInputs) -> Forecast` (load, planned, reserve, PV per quarter on a `Timeline`, `known_until` (where load and PV are known), `yesterday`, `to_attributes` for the sensors), and its functions: load forecast per hour, PV integration of the `watts` forecast, PV calibration, weekend reserve |
| `nordpool_utils.py` | `fetch_nordpool_data` (via the `nordpool.hourly` service, cached per date) and the area → timezone map `tzs` |
| `utils.py` | `get_tariff` |

## Data model

Everything lives in `hass.data["energy_planner"]`:

- `values` — the `nordpool_values` price cache. Persisted.
- `config` — user settings (entity values with `data_store: "config"`), plus
  `entry_id` and `nordpool_entity_id`. Persisted.
- `manual_slots` — list of `manual.ManualSlot` (stored as `{start, end, state,
  soc}`, ended ones dropped on load and on every plan run). Persisted.
- `number_entities`, `select_entities`, … — lists of entity objects per platform
  (keys in `const.py`).
- `save` — coroutine that persists the three stores.
- `options` — a copy of the config entry options (`tariff_preset`, `tariff`), kept
  current by an update listener. Read the tariff with `get_tariff(hass)` (`utils.py`),
  which falls back to a flat tariff from `network_cost` / `network_compensation`.
- `forecast` — the `forecast.Forecast` (see `docs/forecast.md`); `plan` — the `plan.Plan`
  (see `docs/battery-plan.md`). Neither is stored; both are rebuilt after a restart.
- `actions` — the `actions.Actions` last resolved by `write_plan_sensors`, read by the
  plan, action and heat pump sensors and the fuse guard. An action that fails to
  resolve is None and its sensor unavailable, so its device does what it does without
  the planner; all are None while the situation fails (the plan sensor then shows the
  stored plan with no state). `actions_failing` holds the failing parts, so each is
  logged once. Not stored; resolved when the config entry
  is set up, before the sensors are added.
- `prices` — the `prices.SpotPrices`: the published and estimated spot prices per
  quarter (SEK/MWh excl. VAT, keyed by the quarter's start in UTC, so the hour repeated
  when DST ends has its own), written by every plan run before it can fail on a missing
  forecast or battery state, the published ones before the estimate is made; read by
  the heat pump sensors (`prices.spot_at`). `price_estimate` (the estimate's cache) and
  `price_history` (the recorded prices) belong to `prices.py` too.

### Manual slots

The user's overrides of the plan (`manual.py`), added with the `add_slot` service (or
the form in `energy_planner_extras.yaml`): an action state from start to end, rounded
out to whole quarters, kept in UTC. Where they overlap, the last added wins.

- `charge` and `sell` have a target SOC: charge up to it (default the max SOC), then
  hold for the rest of the slot; sell down to it (default the reserve SOC, it may go
  below the reserve), then self-use. Kept between the shutdown and the max SOC.
- They are forced into the plan (`manual.forced`, the optimizer's `forced` and
  `forced_soc`), also while `enabled` is off, and published per quarter as the
  plan's `manual` / `manual_soc` (separate from `modes` / `soc`) and as a list in its
  `manual_slots` attribute.
- The battery action follows the slot covering now as it is, without a plan too
  (source `manual`, `until` where it ends or a later-added slot takes over,
  `manual.running`): `charge`/`sell` at the highest current with the target as
  `soc`, `pause` with the max SOC, `sell-excess` with the reserve. A plan quarter
  made around a slot that has since been removed is not followed (fallback).
- Action states for the inverter: the comment in `examples/battery/follow_battery_action.py`.

### Config keys (entity ids are `<platform>.energy_planner_<key>`)

- `switch`: `enabled` (on; off: the plan is still made and shown, but the battery
  action does not follow it, manual slots still run; the car and heat pump actions
  follow the plan either way)
- `select`: `forecast_load_model`
  (`profile`, default; `temperature`) and `forecast_pv_calibration` (`daily`, default;
  `hourly`), see `docs/forecast.md`
- `time`: `ev_ready_time` (06:30)
- `datetime`: `ev_departure` (a trip, see `docs/car.md`)
- `number`: `ev_soc` and `ev_charge_limit` (see `docs/car.md`), `battery_efficiency` (%, round trip),
  `max_charge_current`, `max_discharge_current`, `battery_capacity` (Wh),
  `battery_shutdown_soc` (%, default 10), `battery_reserve_soc` (%, default 20: the plan does not
  discharge below it, see `docs/battery-plan.md`), `battery_max_soc`, `network_cost` (öre/kWh incl. VAT paid on
  bought energy: transfer fee + energy tax, ~63 here), `network_compensation` (öre/kWh
  received when selling, ~5 here),
  `forecast_weekend_reserve` (kWh, default 4), `forecast_reserve_start` /
  `forecast_reserve_end` (h, default 18 / 22), `grid_import_limit` (kW hourly mean the
  planner may import, 0 = off), `grid_import_limit_start` / `grid_import_limit_end`
  (h, default 6 / 23), `main_fuse` (A per phase, three phases, 0 = off) and
  `fuse_margin` (A, default 2; see `docs/main-fuse.md`), `battery_wear_cost` (öre per kWh taken out of the battery,
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

- **Services:** `add_slot`, `remove_manual_slot` (by its start), `clear_manual_slots`
  (each stores the slots, rewrites the action sensors and requests a plan),
  `set_ev_soc` (see `docs/car.md`), `update_forecast`, `update_battery_plan` (also the button
  `button.energy_planner_update_battery_plan`). The rule-based planners and the
  schedule of slots were removed (docs/adr/0001-one-planner-no-schedule.md);
  `migrate.py` cleans up their stored data and entities.
- **Forecast:** recalculated at :05 every hour (after the recorder compiles hourly
  statistics), once after HA has started and by the `update_forecast` service. Errors are logged and do not stop the timers.
- **Battery plan:** recalculated every quarter (:00:30, :15:30, …), after each timed
  forecast update, and when tomorrow's prices arrive (`tomorrow_valid`), a setting with
  `replans` (`planner/settings.py`) or the tariff
  changes, manual slots change, or the
  SOC is `SOC_DRIFT` (5) % from the plan (at most every 5 min). All go through
  `async_request_plan`. The action sensors are rewritten at second 0 of every minute
  (so a run that starts with a quarter is published on time, and the fuse guard is
  updated right after from it), after every plan and when the manual slots,
  `enabled` or the charger's state change.
- **Entities** are passive views: `update()` reads from `hass.data`, setters write
  back and call `save`.
