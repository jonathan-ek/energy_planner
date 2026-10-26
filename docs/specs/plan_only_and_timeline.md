# Spec: one planner, manual slots in the plan, and a quarter timeline

Agreed 2026-10-26, from the architecture review and a design session. Why the slots
go: [ADR 0001](../adr/0001-one-planner-no-schedule.md). Terms: `GLOSSARY.md`.

## Goals

- **One planner.** The battery optimizer's plan is the only plan. Manual slots are
  forced into it, and the same plan sensor and dashboard show it.
- **Time handled in one module.** Most recent DST bugs came from four time
  conventions (local ISO strings, UTC price keys, local `Plan.starts` compared by
  timestamp, UTC slots). Each fix added the same guard in another file.
- **A typed `Forecast`, like `Plan`.** Today the forecast is a dict with about 40
  string keys, built by a 285-line function in `dynamic_planner.py` that tests reach
  with about 65 patches.

## Steps

Each step is one commit on this branch, passes `scripts/check.sh`, and updates
AGENTS.md. Before step 1, tag the current commit `v-last-slot-planners`.

### 1. Remove the rule-based planners

- Delete `basic_planner.py`, `cheapest_hours_planner.py`, `price_peak_planner.py` and
  their tests. Delete the parts of `nordpool_utils.py` that only they use;
  `fetch_nordpool_data` stays for the plan run.
- Delete their settings and entities: `basic_nr_of_charge_hours`,
  `basic_nr_of_discharge_hours`, `cheapest_hours_nr_of_charge_hours`,
  `price_peak_nr_of_charge_hours`, `price_peak_nr_of_discharge_hours`,
  `price_peak_planner_{cheap,expensive,inbetween}_state`, `earliest_charge_time`,
  `earliest_discharge_time`.
- Rename `price_peak_efficiency_factor` to `battery_efficiency`, the battery's
  round-trip efficiency. On load, move a stored value to the new key, so the houses
  keep 91 / 94 / 87 / 85.
- Replace `planner_state` with `switch.energy_planner_enabled` ("Planner enabled",
  on by default). Migrate the stored value: `off` becomes off, anything else on. It
  only affects the battery, as `planner_state` does today. While off, the plan is
  still made and shown, and the battery action is self-use (source `none`). Manual
  slots still run. The car and heat pump actions follow the plan either way.

### 2. Remove the schedule; manual slots become forced quarters

- Delete the 49 slots: the slot entities in `datetime.py`, `select.py`, `number.py`
  and `switch.py`, the slot keys in `values`, `write_schedule`, `clear_slot`,
  `reset`, `clear_passed_slots` and its timer, `store_disable_state` /
  `restore_disable_state`, `manual_slots.py`'s shifting, and the `auto` slot.
- Delete the 15:31 planner timer, the `run_planner` service and the dynamic
  planner's schedule writing. The forecast already updates hourly, and the plan
  every quarter and on `tomorrow_valid`.
- **Manual slots**
  - **Stored:** a list of `{start, end, state, soc}` with UTC instants on quarter
    boundaries. `add_slot` rounds the start down and the end up to whole quarters,
    and logs when it rounds. Stored slots are rounded on load. Ended slots are
    dropped on load and on every plan run.
  - **States:** `state` takes the action state names (`charge`, `discharge`, `sell`,
    `sell-excess`, `discard-excess`, `pause`). `soc` is a target SOC for `charge`
    and `sell` only; the other states ignore it.
  - **Services:** `add_slot` and `clear_manual_slots` stay. A new
    `remove_manual_slot` removes one slot.
- **In the plan**
  - **Series:** a separate per-quarter series for manual slots, the forced mode and
    its target SOC, None where no manual slot covers the quarter. It never shares a
    field with the plan's own values: `Plan.soc` stays the SOC the plan expects.
  - **Charge:** a manual `charge` charges up to its target and then holds for the
    rest of the slot, like the Solis time-of-use charge window at its cut-off SOC.
  - **Sell:** a manual `sell` sells down to its target and then runs self-use.
  - **Published:** the plan sensor publishes the slots as a `manual_slots` attribute.
- **The battery action**
  - **Source:** a manual slot that covers now gives source `manual`, with the
    target in the action's `soc` attribute (the same meaning as for a planned
    charge), so `follow_battery_action.py` is unchanged.
  - **No plan:** a manual slot is followed even without a fresh plan, from its own
    list. Otherwise a missing or stale plan gives self-use.
  - **`resolve`:** `battery_action.resolve` loses the slot logic.
- **Cleanup on setup:** remove the registry entries of every entity deleted in steps
  1 and 2, and drop their keys from the stored `values` and `config`.
- **Examples:**
  - Delete `examples/dashboard/schedule_card.yml`, `generate_schedule_card.py`,
    `examples/battery/update_battery_action.py` and its automation.
  - Remove the `run_planner` action from `examples/nordpool_reload_automation.yml`.
  - Keep the add-slot form.
- **GLOSSARY.md:** drop Schedule and Slot. Add Manual slot: a user's override over
  whole quarters, a mode with an optional target SOC, forced into the plan. Rename
  "slot state" to Action state; the values stay the same.
- **README.md:** a section "Upgrading from the slot planners" with the HA-side steps
  (see below).

### 3. Time module, and `Plan` onto it

`planner/timeline.py`, pure:

- **`Timeline`:** a run of quarters, kept as UTC instants. It has lookup by moment
  (`index_at`), the end of a quarter and of the timeline, and the local hour, local
  date and local ISO string of a quarter. Local time only comes out through these.
- **`quarter_start(moment)`:** the UTC key of the price dicts.
- **`at_local(day, time, tz)`:** a local date and wall-clock time as an instant.
  The repeated and the skipped DST hour are resolved here, once.

`Plan` holds a `Timeline` instead of `starts`. Its series stay plain tuples parallel
to it. The plan sensor's attributes are unchanged.

DST is tested once, against this module: the 92 and 100 quarter days, the repeated
hour, and `at_local` on both change days. Delete repeated-hour tests elsewhere that
only test conversion. Keep the ones where a consumer has DST behaviour of its own.

### 4. `Forecast`, and the glue out of `dynamic_planner.py`

- **`Forecast`:** pure, in `forecast.py`. It has a `Timeline`, the series (`load`,
  `planned`, `reserve`, `pv`, `temperature`, `away`), the totals, `heat_pump_loss`,
  `ev_need_kwh` and `known_until`.
  - **`known_until`:** the end of the last quarter where load and PV are both
    known. The series keep their Nones (PV past the reach of the Forecast.Solar
    fallback).
  - **`yesterday`:** yesterday's forecast, a second `Forecast`. The actual totals
    are plain fields.
  - **`to_attributes`:** publishes exactly the attributes the two forecast sensors
    publish today, local ISO `starts` included, so the Energi dashboard is
    unchanged. A test pins the attributes.
- **`planner/forecast_update.py`:** the HA glue. It fetches into a plain
  `ForecastInputs`: recorder hourly means, the Open-Meteo responses, the
  Forecast.Solar fallback, calendar events, solar planes and the settings it needs.
  A pure `build_forecast(inputs, now)` turns that into a `Forecast`, which is then
  published to `hass.data[DOMAIN]["forecast"]`.
- **The plan run:** reads the `Forecast` and builds its timeline from the forecast's
  timeline, cut at `known_until` and at the last quarter with a price. Prices stay
  a dict keyed by `quarter_start`.
- **Clean up:** delete `dynamic_planner.py`. `heating.py` and `battery_plan.py` read
  `Forecast` fields instead of string keys.

### 5. Car deadline onto `at_local`

`ev.ready_time` and `deadline_index` use `at_local` and the plan's `Timeline`
instead of their own `combine` and `.timestamp()` comparisons.

## Upgrading the houses

These steps go in README.md ("Upgrading from the slot planners"). After each deploy,
Claude applies them over ssh in the owner's, the parents' and Kiddes' HA, backing up
first. Oscar and the fifth user stay on `v-last-slot-planners` until they are moved.

- **Owner:** remove the `energy_planner.run_planner` action from "Auto ladda om
  nordpool". Replace the schedule card on the Energi dashboard with the manual slots
  list and the plan chart.
- **Parents and Kiddes:** remove the slot triggers (`switch.energy_planner_slot_1_active`,
  `select.energy_planner_slot_1_state`, `datetime.energy_planner_slot_{1,2}_date_time_start`)
  from the battery automation. The action sensor's trigger is enough. On the
  dashboard:
  - replace the schedule card
  - replace `select.energy_planner_planner_state` with
    `switch.energy_planner_enabled`
  - replace `number.energy_planner_price_peak_efficiency_factor` with
    `number.energy_planner_battery_efficiency`
  - remove the earliest charge and discharge times
- **All:** check that the old entities are gone from the entity registry after the
  restart, and that `sensor.energy_planner_battery_action` still has a state.

## Out of scope

- Prices as a series on the timeline, and a price feed module (candidate 3 in the
  review).
- A readings snapshot and resolving all actions at one moment (candidate 5).
- Typed runtime data instead of `hass.data` (candidate 6).
- Moving the fifth user to the optimizer.

## Left open after the reviews

Findings from the code reviews between the steps that were not fixed, and why.

- Step 1: `examples/energi_dashboard.yaml` (the owner's dashboard copy, untracked and
  not part of these commits) still lists the removed entities. Update it with the
  owner's dashboard when the houses are upgraded.
- Step 2: the battery action is rewritten every minute, so a manual slot starts or
  ends up to a minute late (as the slots did). A timer at the next slot boundary
  would fix it.
- Step 2: `remove_manual_slot` with a time without an offset in the hour repeated
  when DST ends always means the first 02:xx; the `manual_slots` attribute's times
  have offsets and work.
- Step 2: a manual `charge` charges at the highest current without an import limit
  (as the slots did), but the plan models a forced `charge` at its import target, so
  the plan can expect slower charging than happens. The next plans correct the SOC.
- Step 2: `examples/battery/solis_dispatch_test.yaml` and
  `solis_dispatch_mode_test.yaml` turn `automation.update_battery_action` off and
  on. That is the HA automation id in the houses that still have it; check it when
  the houses are upgraded.
- Step 2: `examples/energi_dashboard.yaml` (untracked) also has the slot card.
- Step 4, deviations from the spec: `build_forecast(inputs)` takes `now` inside
  `ForecastInputs`; `known_until(moment)` is a method; the totals, calibration and
  yesterday's actual totals the sensors show are in an untyped `Forecast.details`
  mapping (only the sensors and one log line read them). Type them when something
  else needs them.
- Step 4: the glue tests (`test_forecast_update.py`) still patch the fetches to test
  the wiring end to end; the forecast's behaviour is tested on `build_forecast`
  (`test_build_forecast.py`).
- Step 5: nothing left open. A ready time in the hour skipped when DST starts means
  the same time in summer time an hour later (`timeline.at_local`).
