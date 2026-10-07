# Energy Planner — agent guide

Home Assistant custom integration (HACS, domain `energy_planner`) that builds a
battery charge/discharge schedule from Nord Pool spot prices. The integration
only *produces* the schedule as HA entities; applying it to an inverter is done
outside the integration by scripts/automations (see `examples/`).

## Layout

| Path | What it is |
| --- | --- |
| `custom_components/energy_planner/__init__.py` | Setup, `hass.data` structure, services (`async_setup`), timers (`async_setup_entry`), planner dispatch (`run_planner`) |
| `custom_components/energy_planner/planner/` | All planning logic (see below) |
| `custom_components/energy_planner/button.py` | `button.energy_planner_update_battery_plan`, recalculates the battery plan |
| `custom_components/energy_planner/{datetime,number,select,switch,time}.py` | Entity platforms; each holds its entity definitions as dicts plus one generic entity class |
| `custom_components/energy_planner/sensor.py` | The two forecast sensors (`sensor.energy_planner_load_forecast`, `sensor.energy_planner_pv_forecast`) `sensor.energy_planner_battery_plan` and `sensor.energy_planner_battery_action` (what the inverter should do now, `battery_plan.current_action`); they only read `hass.data` |
| `custom_components/energy_planner/store.py` | Thin wrapper around HA `Store` (`.storage/energy_planner.<key>`) |
| `custom_components/energy_planner/config_flow.py` | Setup asks for `nordpool_entity_id`; the options flow chooses the grid tariff (flat, a preset or custom YAML) |
| `custom_components/energy_planner/services.yaml`, `translations/en.json` | Service descriptions |
| `tests/planner/` | Tests for the basic planner, the price peak planner, the forecast functions and the forecast glue. The `.md`/`.txt`/`ARCHITECTURE.py` files in `tests/` are documentation |
| `examples/` | Lovelace cards, `python_script`s and automations that consume the schedule; `follow_battery_action.py` + `_automation.yml` follow the action sensor and only write changed settings |
| `energy_planner_extras.yaml`, `add_slot_form.yml`, `Basic_config_card.yml` | HA package + cards for the manual "add slot" form |
| `local_deploy.sh` | Copies the component into `~/projects/ha_demo/config` and restarts its docker compose |

### `planner/`

| File | Role |
| --- | --- |
| `__init__.py` | Re-exports each module's `planner()` as `basic_planner`, `cheapest_hours_planner`, `price_peak_planner`, `dynamic_planner`, plus `async_update_forecast`. Note `planner.dynamic_planner` is then the function, not the module (tests use `importlib`) |
| `basic_planner.py` | N cheapest quarters between earliest charge and earliest discharge time → charge; N most expensive after → discharge; rest pause |
| `cheapest_hours_planner.py` | N cheapest quarters per calendar day → charge; everything else discharge |
| `price_peak_planner.py` | `find_charge_periods` / `find_discharge_periods` pick the cheapest / most expensive windows (best first), `remove_overlaps` drops conflicts, `match_charge_discharge_periods` pairs them where `is_profitable` says the spread pays off, `build_schedule` turns pairs into slots. All pure and tested; `plan_day` is the HA glue |
| `dynamic_planner.py` | WIP planner. `async_update_forecast` reads recorder hourly statistics and the Forecast.Solar estimates, runs `forecast.py` and stores the result in `hass.data[DOMAIN]["forecast"]`. `planner()` updates the forecast and the plan and writes one `auto` slot to the plan's end, with manual slots laid over it |
| `battery_optimizer.py` | Pure battery optimizer (numpy): `optimize` picks a mode per quarter (`self_use`, `hold`, `sell_excess`, `charge`, `sell`) by dynamic programming over the SOC and searches the monthly power peak levels. See "Battery plan" |
| `battery_plan.py` | HA glue for the optimizer: `async_update_plan` reads the forecast, prices, battery settings and state and this month's hourly grid import, and stores `hass.data[DOMAIN]["plan"]`; `async_request_plan` (one run at a time, requests during a run give one more), `current_action`, `expected_soc` |
| `battery_action.py` | Pure: `resolve` turns slot 1 and the plan into the action now (slot state, source, SOC, current, import target, until), mode ↔ slot state maps |
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
- `tmp` — scratch space during a planner run.

### Slots

The schedule is `SLOT_COUNT` (49) fixed slots, numbered from 1. Slot `i` is four keys
in `values`, each mirrored by an entity `<platform>.energy_planner_<key>`:

| Key | Platform | Notes |
| --- | --- | --- |
| `slot_{i}_date_time_start` | `datetime` | tz-aware; stored as ISO string, parsed back on load by `parse_stored_data` |
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

- `select`: `planner_state` (`basic`, `cheapest hours`, `dynamic`, `price peak`, `off`),
  `price_peak_planner_{cheap,expensive,inbetween}_state`
- `time`: `earliest_charge_time` (22:00), `earliest_discharge_time` (06:00)
- `number`: `basic_nr_of_charge_hours`, `basic_nr_of_discharge_hours`,
  `cheapest_hours_nr_of_charge_hours`, `price_peak_nr_of_charge_hours`,
  `price_peak_nr_of_discharge_hours`, `price_peak_efficiency_factor` (%),
  `max_charge_current`, `max_discharge_current`, `battery_capacity` (Wh),
  `battery_shutdown_soc`, `battery_max_soc`, `network_cost` (öre/kWh incl. VAT paid on
  bought energy: transfer fee + energy tax, ~63 here), `network_compensation` (öre/kWh
  received when selling, ~5 here),
  `forecast_weekend_reserve` (kWh, default 4), `forecast_reserve_start` /
  `forecast_reserve_end` (h, default 18 / 22), `grid_import_limit` (kW hourly mean the
  planner may import, 0 = off), `grid_import_limit_start` / `grid_import_limit_end`
  (h, default 6 / 23), `battery_wear_cost` (öre per kWh taken out of the battery,
  default 20)
- Forecast inputs (config store only, defaults in `const.py`): `forecast_load_sensor`,
  `forecast_pv_sensor`, `forecast_ev_sensor`, `forecast_calendar`
  (`calendar.energiplan`); battery plan inputs `battery_soc_sensor`,
  `battery_voltage_sensor`, `grid_import_sensor`

## Control flow

- **Triggers:** a timer that fires every hour at :31:15 and acts when it is 15:xx
  Stockholm time (after next-day prices are published), and the
  `energy_planner.run_planner` service. Both call `run_planner(hass)` in
  `__init__.py`, which dispatches on `config["planner_state"]`.
- **A planner run:** fetch prices → `store_disable_state` → `reset` (all slots off)
  → `plan_day` (once per day for basic/cheapest hours, once over the whole range
  for price peak) → `add_manual_slots` → `restore_disable_state` → `update_entities`
  → `save`.
- **Services:** `add_slot` (appends to `manual_slots` and re-applies them),
  `run_planner`, `clear_manual_slots`, `update_forecast`, `update_battery_plan`
  (also the button `button.energy_planner_update_battery_plan`).
- **Forecast:** recalculated at :05 every hour (after the recorder compiles hourly
  statistics), once after HA has started, by the `update_forecast` service and by
  the dynamic planner. Errors are logged and do not stop the timers.
- **Battery plan:** recalculated every quarter (:00:30, :15:30, …), after each timed
  forecast update, and when tomorrow's prices arrive (`tomorrow_valid`), a plan setting
  (`PLAN_SETTINGS` in `__init__.py`) or the tariff changes, manual slots change, or the
  SOC is `SOC_DRIFT` (5) % from the plan (at most every 5 min). All go through
  `async_request_plan`. The action sensor is rewritten every minute and when slot 1
  changes.
- **Entities** are passive views: `update()` reads from `hass.data`, setters write
  back and call `save`.

## Forecast

Method and numbers come from a backtest on the real HA data (Oct 2026):

- **Load** = `sensor.solis_s6_solis_household_load_power` hourly mean minus EV
  charging (`sensor.ehwuhqtp_effekt`, planned separately). Forecast per local hour =
  mean of (last 7 days' profile, last 4 same-type days' profile; same type =
  workday/weekend), using only days before today, then split evenly into quarters.
  About 24% hourly and 14% daily error. Quarter profiles, temperature and gradient
  boosting gave no real improvement (district heating, so the load hardly depends on
  temperature). Before profiling, hours more than 3.5 kWh above the median of the same
  hour in the previous 14 days are capped (`cap_large_loads`): one-off loads like the
  sauna come from the calendar or the reserve instead, so they are not counted twice.
- **PV** = Open-Meteo `global_tilted_irradiance` per panel plane (planes, kWp and
  location read from the Forecast.Solar config, `_solar_planes`), averaged over the
  models in `OPEN_METEO_MODELS` (MET Nordic, ECMWF, ICON), derated for cell
  temperature, summed over planes, then interpolated to quarters from mid-hour points
  (keeps the hourly energy). One request per plane to the previous-runs API returns the
  latest forecast and the forecast made the day before for the last 14 days. Open-Meteo
  hourly values are means of the hour *before* the timestamp. Scaled by
  `actual / day-before forecast` over the last 14 days (actual: Solis `total_pv_power`
  with glitches above 25 kW dropped), limited to 0.3–2.5, 1.0 until 5 kWh of forecast
  exist. Day-ahead backtest (448 days): 0.63 kWh hourly, 11% daily error
  (Forecast.Solar with the same correction: 1.02 kWh, 17%). If Open-Meteo fails,
  Forecast.Solar `watts` with its `power_production_now` history is used instead;
  `pv_source` says which.
- **Planned loads** come from timed events in the local calendar
  `calendar.energiplan` (recurring events work; all-day events are ignored). Energy:
  `<n> kWh` in the title or description, otherwise the sum of defaults for activities
  named in the title (`PLANNED_LOAD_DEFAULTS`: bastu 5, torktumlare 2.5, tvätt 1.5,
  disk 1, gäster 4, elbil/ladda 20 kWh), otherwise the event is ignored. The energy is
  spread evenly over the event, so the event should cover when the load runs.
- **Reserve** = `forecast_weekend_reserve` spread over Sat/Sun
  `forecast_reserve_start`–`forecast_reserve_end` (default 18–22, when the sauna
  usually runs), for unplanned weekend load. Holidays are not detected.
- Output: `hass.data[DOMAIN]["forecast"]` with parallel lists `starts`, `load`
  (profile), `planned`, `reserve`, `pv` (kWh per quarter, all of today and tomorrow),
  `planned_events`, and totals. `load_tomorrow` / `load_today_remaining` = profile +
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
resolves what to do now (`battery_action.resolve`): a running slot with a real state
(manual, or from another planner) wins; an `auto` slot follows the plan (self-use if
the plan is older than 30 min or ends); otherwise self-use. An inverter script follows
the action sensor (`examples/follow_battery_action.py`). With the dynamic planner, manual
slots are also forced into the plan (`forced`), so it plans around them; with the other
planners the plan is a preview and ignores them.

- Horizon: the current quarter until the last quarter with both a price and a load
  forecast. Load = forecast profile + planned + reserve; PV = forecast.
- Modes per quarter, matching what the inverter scripts can do: `self_use` (battery
  covers the load, stores PV surplus), `hold` (keeps the energy but shaves import
  above the quarter's target, like the "Peak +/-" automations), `sell_excess` (PV
  surplus goes to the grid instead of the battery, slot state `sell-excess`: worth it
  while prices fall during the day and the battery will fill later anyway; a full
  battery exports in `self_use`, which wins ties), `charge` (from PV and grid up to
  the import target), `sell` (full discharge to the grid).
  Slot states (`battery_action.SLOT_STATES`): `self_use` → `discharge`, `hold` →
  `pause`, `sell_excess` → `sell-excess`, `charge` → `charge`, `sell` → `sell`.
- Mode changes cost `SWITCH_COST` (0.05 SEK): the dynamic program's state is the SOC
  and the previous mode, and the first quarter's previous mode is the one the last
  plan has for now, so a recalculation only changes the running mode when that pays.
  Modes that do the same in a quarter (`sell` with an empty battery and
  `sell_excess`) are named after the simplest.
- Costs: import at `spot * 1.25 + tariff.buy_fee`, export at `spot +
  tariff.sell_fee`, import above `grid_import_limit` at `LIMIT_PENALTY` (20 SEK/kWh, so
  only when unavoidable), battery wear `battery_wear_cost` per kWh taken out of the
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
- Battery: capacity, shutdown/max SOC, charge/discharge current × the live voltage
  (`battery_voltage_sensor`), SOC (`battery_soc_sensor`), round-trip efficiency
  `price_peak_efficiency_factor`. About 1 s per day of quarters, run in the executor.
- Not done yet: making the inverter follow the import target while charging and in
  `hold` (candidates: Solis peak shaving
  `number.solis_s6_eh1p_peak_max_usable_grid_power` with force charge allowed under
  peak shaving, or adjusting the charge current like the "Peak +/-" automations).

## Household and tariff

Background for the planner (from the owner, Oct 2026):

- Location Linköping (58.38 N, 15.66 E), price area SE3, grid operator Tekniska verken.
- Main heating is district heating (fjärrvärme, price in `sensor.fjarrvarme`,
  öre/kWh). An air-to-air heat pump ("Hallen", MELCloud `climate.hallen`, energy
  `sensor.hallen_energy`) can heat or cool part of the house when that is cheaper than
  district heating; existing HA automations already compare spot price and the
  estimated COP (`sensor.estimerad_verkningsgrad_luft_luft_varmepump`) with the
  district heating price.
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
charging the battery from the grid in the day costs far more than at night. Later, the
planner should also be able to shift controllable loads (e.g. the heat pump) to stay
under it.

The owner's brother uses E.ON Energidistribution (Virserum), preset `eon_20a` from his
price list: 32.30 öre/kWh transfer fee (+ 45 öre energy tax, assumed), 835 kr/month,
no power charge, selling spot + 10 öre. He has separate meters on the spa and the
ground-source heat pump, which could later feed load patterns. The model covers the common Swedish variants: mean of the N highest hourly means per
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
- Price peak cost model (`is_profitable`): a delivered kWh costs
  `(charge price * 1.25 + network cost) / efficiency`. Used at home
  (`discharge`) it is worth `discharge price * 1.25 + network cost`, so the fees only
  count on the losses; sold (`sell`, `sell-excess`) it is worth
  `discharge price + network compensation` (no VAT for private sellers).
- A planning "day" in the basic planner runs from earliest charge time to the next
  earliest charge time, not midnight to midnight, so it pulls in yesterday's prices.

## Common changes

- **New setting:** add a definition dict in the right platform file with
  `"data_store": "config"`; read it as `hass.data[DOMAIN]["config"]["<id>"]`.
  Defaults are only written when the key is missing from the store.
- **New planner:** add `planner/<name>_planner.py` with `async def planner(hass, ...)`,
  export it in `planner/__init__.py`, add a branch in `run_planner`
  (`__init__.py`), and add the option to `planner_state` in `select.py`.
- **New slot state:** update the lists in `select.py`, the validation in
  `add_slot_service`, `services.yaml`, SOC handling in `manual_slots.py`, and the
  consumers in `examples/`.

## Commands

```bash
make install        # uv sync (creates .venv from pyproject.toml + uv.lock)
make test           # pytest tests/ -v
make test-basic     # only tests/planner/test_basic_planner.py
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
  Tests mock `hass` with `MagicMock`, patch `fetch_nordpool_data`, and freeze
  `dt_utils.now` at 2026-03-12 00:00 Stockholm time (autouse `fixed_now` fixture).
- The basic and price peak planners, the forecast, the tariff and the options flow
  have tests. The cheapest hours
  planner, manual slots and `clear_passed_slots` are untested.
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
- Dates and times in `values`, `config` and `manual_slots` are always objects in
  memory (`parse_stored_data` converts them on load), so do not add string checks.
  The `nordpool_values` price cache is the exception and still holds ISO strings.
- An `off` slot can appear in the middle of the list when manual slots leave a gap
  (for example a manual slot before the first planned slot). Unused slots are the
  ones whose start is `None`.
- The basic planner only plans the current day's window when tomorrow's prices
  exist or the window started yesterday; with `earliest_charge_time` at 00:00 it
  plans nothing for today until tomorrow's prices are published.
- Card labels and some example entity names are in Swedish.
