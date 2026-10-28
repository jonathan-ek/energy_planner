# Energy Planner — agent guide

Home Assistant custom integration (HACS, domain `energy_planner`) that plans a home
battery, an electric car and a heat pump per quarter from Nord Pool spot prices,
forecasts and the grid tariff. The integration only *produces* the plan and the
actions as HA entities; applying them to the devices is done outside the integration
by scripts/automations (see `examples/`).

Domain terms (plan, mode, run, action, action state, manual slot, setting, reading)
are defined in `GLOSSARY.md`. Decisions: `docs/adr/`.

## Topic docs

Read the doc for the part you are about to change or explain before you start;
each holds the method, the backtests behind its numbers and its HA entities.

| Doc | Read it for |
| --- | --- |
| `docs/architecture.md` | Where code lives (file per module), `hass.data`, the manual slots, every config key and setting, services, timers and plan triggers |
| `docs/forecast.md` | Load and PV forecasts, the calendar's planned loads and away, yesterday's forecast |
| `docs/battery-plan.md` | The optimizer, modes and costs, power charges, the reserve, price estimates, the car in the optimizer, the inverter scripts and Solis Remote Dispatch |
| `docs/heat-pump.md` | Heat pump economy, COP, district heating prices, its draw and its plan |
| `docs/car.md` | Car charging: need, deadline, charger states, `ev_action` |
| `docs/main-fuse.md` | The per-phase fuse guard |
| `docs/comfort.md` | Indoor temperature schedules (WIP) and the owner's heating system |
| `docs/households.md` | The owner's house, loads and grid tariff (Tekniska verken), the tariff model and presets, Oscar's home (the other homes' quirks are in the topic they concern) |

## Domain facts worth knowing

- Prices are 15-minute resolution. "Hours" settings are multiplied by 4 to get a
  quarter count.
- Area and currency are parsed from the Nord Pool entity id by position
  (`prices.nordpool_zone`: parts 2 and 3 split at `_`, e.g.
  `sensor.nordpool_kwh_se3_sek_…`).
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
- **New action state:** add it to `battery_action.ACTION_STATES` and
  `manual.STATE_MODES` (and its SOC in `battery_action.resolve` / `manual.target`),
  `services.yaml`, `translations/en.json`, `energy_planner_extras.yaml`, and the
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
- CI: `.github/workflows/validate.yml` (ruff, hassfest, HACS) and `tests.yml`
  (pytest on 3.14 + ruff).

## Before committing

- Update the topic doc (`docs/<topic>.md`) whose behaviour, numbers or entities the
  change touches; new topics get a doc and a row in "Topic docs".
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

- `add_slot` input without an offset is in HA's configured time zone.
- Compare, sort, add to and key by instants, never local times in one zone: Python
  compares and hashes those by wall clock and ignores `fold`, so the hour repeated
  when DST ends sorts into (and in a dict merges with) its copy, and adding a
  timedelta keeps the wall clock. Use `timeline.py`: the plan's quarters, manual
  slots, price keys (`quarter_start`) and hourly sums (`hour_start`, the import
  history and the optimizer's power charge hours) are UTC, and the forecast is on a
  `Timeline`. `forecast.py`'s functions still take local quarter starts for their
  calendar logic; compare those as timestamps. Local time is only for calendar logic
  (day windows, hour of day) and display.
- Dates and times in `config` and `manual_slots` are always objects in memory
  (`parse_stored_data` and `_load_manual_slots` convert them on load), so do not add
  string checks. The `nordpool_values` price cache is the exception and still holds
  ISO strings.
- Card labels and some example entity names are in Swedish.
