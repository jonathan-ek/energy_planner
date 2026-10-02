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
| `custom_components/energy_planner/{datetime,number,select,switch,time}.py` | Entity platforms (there is no sensor platform); each holds its entity definitions as dicts plus one generic entity class |
| `custom_components/energy_planner/store.py` | Thin wrapper around HA `Store` (`.storage/energy_planner.<key>`) |
| `custom_components/energy_planner/config_flow.py` | Single step: asks for `nordpool_entity_id` |
| `custom_components/energy_planner/services.yaml`, `translations/en.json` | Service descriptions |
| `tests/planner/test_basic_planner.py` | The only tests (basic planner). The `.md`/`.txt`/`ARCHITECTURE.py` files in `tests/` are documentation |
| `examples/` | Lovelace cards, `python_script`s and automations that consume the schedule |
| `energy_planner_extras.yaml`, `add_slot_form.yml`, `Basic_config_card.yml` | HA package + cards for the manual "add slot" form |
| `local_deploy.sh` | Copies the component into `~/projects/ha_demo/config` and restarts its docker compose |

### `planner/`

| File | Role |
| --- | --- |
| `__init__.py` | Re-exports each module's `planner()` as `basic_planner`, `cheapest_hours_planner`, `price_peak_planner`, `dynamic_planner` |
| `basic_planner.py` | N cheapest quarters between earliest charge and earliest discharge time → charge; N most expensive after → discharge; rest pause |
| `cheapest_hours_planner.py` | N cheapest quarters per calendar day → charge; everything else discharge |
| `price_peak_planner.py` | Finds cheap/expensive windows and pairs them (`match_charge_discharge_periods`) only when the spread beats efficiency loss + network fees. Most complex and most recently changed |
| `dynamic_planner.py` | WIP: only queries the recorder DB and logs, writes no schedule |
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
- `tmp` — scratch space during a planner run.

### Slots

The schedule is `SLOT_COUNT` (49) fixed slots, numbered from 1. Slot `i` is four keys
in `values`, each mirrored by an entity `<platform>.energy_planner_<key>`:

| Key | Platform | Notes |
| --- | --- | --- |
| `slot_{i}_date_time_start` | `datetime` | tz-aware; stored as ISO string, parsed back on load by `parse_stored_data` |
| `slot_{i}_state` | `select` | `charge`, `discharge`, `sell`, `sell-excess`, `discard-excess`, `pause`, `off` |
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
  `battery_shutdown_soc`, `battery_max_soc`, `network_cost`, `network_compensation` (öre/kWh)

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
  `run_planner`, `clear_manual_slots`.
- **Entities** are passive views: `update()` reads from `hass.data`, setters write
  back and call `save`.

## Domain facts worth knowing

- Prices are 15-minute resolution. "Hours" settings are multiplied by 4 to get a
  quarter count.
- Area and currency are parsed from the Nord Pool entity id by position:
  `entity_id.split("_")[2]` and `[3]` (e.g. `sensor.nordpool_kwh_se3_sek_…`).
- Requires the `nordpool` custom integration for the `nordpool.hourly` service.
- Price values are per MWh. In `price_peak_planner`, `* 1.25` is VAT and `* 10`
  converts the öre/kWh network fees to the same unit.
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
- Only the basic planner has tests. The other planners, manual slots and
  `clear_passed_slots` are untested.
- CI: `.github/workflows/validate.yml` (ruff, hassfest, HACS) and `tests.yml`
  (pytest on 3.14 + ruff).

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
- `dynamic_planner.py` is work in progress and writes no schedule.
- Card labels and some example entity names are in Swedish.
