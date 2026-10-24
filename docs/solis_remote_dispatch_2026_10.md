# Solis Remote Dispatch, tests 2026-10-11 (the owner's S6-EH3P)

Why: at the owner's parents (S6-EH3P, 35 A main fuse) peak shaving's "max usable grid
power" does not limit charging from the grid. With it at 7.8-15 kW the inverter
charged at full rate, about 19 kW from the grid (20 kW import, L3 at 35 A), so the
fuse guard's grid limit (`planner/fuse.py`) could not be applied that way. Solis
Remote Dispatch has an import limit and a failsafe; these tests check it on the
owner's inverter.

## The inverter and the integration

- The owner's inverter: S6-EH3P 5-10 kW (model register 0x2070: protocol 32,
  "S6 3-Phase HV Hybrid (5-10kW)"), datalogger S2-WL-ST, solis_modbus 4.2.6. It was
  set up as "S6-EH1P" (the first model in the setup dropdown), so its entity ids are
  `solis_s6_eh1p_*` (and some `solis_s6_eh3p_*`); changed to S6-EH3P on 2026-10-11 by
  editing the config entry (the Reconfigure form's connection check fails while the
  integration holds the datalogger's only Modbus TCP connection). That only changes
  names and the rated power the integration assumes (8 kW for EH1P, 60 kW for EH3P).
- Remote Dispatch needs register 34502 = 0xAA55. The owner's reads 43605 (0xAA55),
  function map (34503) 1. The parents' S6-EH3P (protocol 33) does not answer 34502
  (the Modbus timeouts at 34502/34504 in their log): not available, ask Solis support
  for a firmware with it.
- Registers (Modbus protocol Ver 3.4): 44100 master, 44101 failsafe (min), 44102 limit
  switches, 44103/44104 import/export limit (100 W), 44105 mode, 44106-44107 power
  (S32, 10 W), 44108 function bits, 44109/44110 SOC window, 44111 reserve SOC, 44112
  PV limit; status in 34504. The integration writes them as two blocks per
  `solis_modbus.solis_dispatch` call and keeps them in RAM only (no flash wear,
  according to its code; not confirmed by Solis). Every call rewrites the import and
  export limits: omitted means off (0xFFFF), so a limit does not carry over.
- `solis_dispatch_stop` writes mode 1, function 1, master 0.

## Test 1: grid and battery modes (22:10-22:28, dark, house ~1.0-1.2 kW, SOC 29 %)

`script.solis_dispatch_test` (`examples/battery/solis_dispatch_test.yaml`), a sample every 30 s.
Inverter meter `sensor.solis_s6_eh1p_grid_power_net` (+ import) and the HAN meter
agreed within ~50 W; battery `sensor.solis_s6_eh1p_battery_power_net` (+ discharge).

| Step | Command | Result |
| --- | --- | --- |
| 2 | `grid_import` 1000 W | import 1030-1070 W within 30 s; the battery idle (the house took ~1 kW) |
| 3 | `grid_import` 0 W | import ~60 W, the battery covers the house |
| 4 | `battery_charge` 3000 W | battery -2950 to -2985 W, import ~4.15 kW |
| 5 | `battery_charge` 3000 W, `import_limit_watts` 1500 | **import 1410-1460 W, the charging cut to 180-240 W** |
| 6 | `grid_import` 500 W, failsafe 2 min, not repeated | import 230-620 W (less steady), back to self-use 2-2.5 min after the command (34504: 2 -> 0) |
| 7 | `solis_dispatch_stop` | registers back to their defaults, self-use |

34504 read 1 for the first sample after a command, then 2 while dispatching, 0 when
off.

## Test 2: selling (22:38-22:43, SOC 28 -> 27 %)

| Command | Result |
| --- | --- |
| `battery_discharge` 2000 W, `soc_min` 25 | battery 1840-1960 W, export 580-840 W (the rest covered the house) |
| `grid_export` 1500 W | export 1475-1500 W, the battery 2.8-2.9 kW (house + export) |
| `solis_dispatch_stop` | for about 30-60 s the battery charged ~2 kW from the grid (+1.3 kW import), then self-use again |

## Conclusions

- The import limit caps the total import while charging, and the inverter sets the
  charging power itself: what the fuse guard and the plan's import target need.
  It stays slightly below the limit (1.41-1.46 kW at 1.5 kW).
- Changes take effect within 30 s. The failsafe ends dispatch about 2-2.5 min after
  the last command, so a script that repeats the command every minute with a
  failsafe of 3 minutes falls back to the inverter's own mode within 3 minutes of
  stopping.
- `grid_export` holds the export exactly; `battery_discharge` sets the battery's
  power.
- Leaving a discharge dispatch causes a short grid charge (about a minute, ~2 kW),
  probably the self-use loop settling; stopping from a charge or a grid import did
  not.

## Test 3: in sunshine (2026-10-13 12:12-12:31, SOC 23 %)

`script.solis_dispatch_sun_test`, PV 2.0-2.6 kW under thin cloud, the house about
0.55-0.7 kW (the heat pump and other loads off for the test). Until 12:15 the battery
script's `sell-excess` time-of-use window (discharge 00:00-12:15 at 0 A) was still
set, so steps 1 and 2 ran in it; the test now clears the windows first.

| Step | Command | Result |
| --- | --- | --- |
| 1 | baseline (the `sell-excess` window) | export 1.64 kW, the battery 0 |
| 2 | `self_consumption`, SOC 20-100 | **the same: export 1.4-1.6 kW, the battery 0**, 34504 = 2. Inside the time-of-use window, so not a test of self-use; to repeat |
| 3 | `feed_in_priority` (window over) | export 1.35-1.6 kW (all the surplus), the battery 0 |
| 4 | `self_consumption`, `soc_max` = SOC now (23) | export 1.5-1.65 kW, the battery 0: at the window's end the surplus is exported |
| 5 | as 4, `export_limit_watts` 0 | **PV curtailed 2.3 -> 0.7 kW, export 0 (±20 W)** within 30 s |
| 6 | `battery_charge` 5 kW, `import_limit_watts` 500 | the battery -1.95 to -2.25 kW (PV + grid), **import 0.40-0.46 kW** |
| 7 | `battery_charge` 3 kW, `soc_max` = SOC now | the battery stops, export 1.9 kW (not curtailed) |
| 8 | `solis_dispatch_stop` | 34504 = 0 within 30 s, then self-use: the battery takes the surplus (-1.79 kW), import ~0 |

- `feed_in_priority` replaces `sell-excess`, and an export limit of 0 replaces the
  feed-in limit switch for `discard-excess`.
- The import limit holds while charging with PV too, again a little below it.
- At the SOC window's end the battery stops and the surplus goes to the grid, both
  in `self_consumption` and `battery_charge`.
- Leaving the dispatch went straight to self-use, without the short grid charge seen
  after a discharge.

## Test 4: `self_consumption` outside a time-of-use window (2026-10-13 12:36-12:50)

The plan in self-use, no time-of-use window, peak shaving off, the battery taking the
surplus (-1.6 to -2.2 kW) before each run; PV 2.7-5.3 kW, the house 1.4-1.6 kW,
SOC 23-24 %. Commands from the HA API, a sample every 20 s.

| Command | Result |
| --- | --- |
| `self_consumption`, SOC 20-100 (power 0, the integration's default) | **the battery 0, the whole surplus exported** (1.8-3.7 kW), 34504 = 2, for 3 min and again for 1.5 min |
| `feed_in_priority` | the same |
| `self_consumption`, `power_watts` 5000 | the same: the battery 0, export 1.4-1.6 kW |
| `feed_in_priority`, `power_watts` 5000, the house above the PV for 40 s (2.5-2.9 kW) | **the battery 0, import 0.12 kW**: it does not discharge either |
| `solis_dispatch_stop` | self-use again within 20-40 s, the battery -0.5 to -1.75 kW |

- On this firmware modes 5 and 6 (44105) do the same: the battery idles, PV goes to
  the house and the rest to the grid, the grid covers what the PV does not. So
  `self_consumption` is no replacement for self-use: the script keeps the inverter's
  own mode for it (no dispatch).
- `feed_in_priority` works for `sell-excess` while the PV covers the house; below
  that it imports instead of discharging, unlike the script's `sell-excess` (which
  falls back to self-use). That only matters if the plan expects the battery to cover
  the house then.

## Test 5: hold (2026-10-14 01:45-01:58, dark, house 1.1-1.5 kW, SOC 76 %)

From the API after the night's charge, at the start of the plan's `hold`; time-of-use
windows and peak shaving off, a sample every 20 s.

| Step | Command | Result |
| --- | --- | --- |
| 1 | baseline, no dispatch | self-use: the battery takes over the house within 40 s |
| 2 | `grid_import` 500 W, `allow_grid_charge` false | **import 0.36-0.55 kW** (0.87 at a load step), the battery covers the rest (0.8-0.9 kW) |
| 3 | `grid_import` 2000 W, `allow_grid_charge` false, the house below it | **the battery 0, import = the house** (1.14-1.35 kW): no grid charging |
| 4 | `battery_hold`, `import_limit_watts` 500 | the battery 0, import 1.18-1.41 kW: **the import limit is not shaved** |
| 5 | `battery_discharge` 2 kW, `soc_min` = SOC now | the battery 0 (±0.1 kW): it stops at `soc_min` |
| 6 | `solis_dispatch_stop` | self-use within 40 s, no grid charge |

- `grid_import` at the import target without grid charging is `hold`: the battery
  covers only the load above the target and is not charged from the grid below it.
- `battery_hold` only keeps the battery; an import limit does not make it discharge,
  so it is only used for a manual pause slot (no target).
- The script then ran the plan's `hold` with `grid_import` 7300 W (the night's power
  charge level): the battery 0, import = the house (1.3-1.4 kW).
- The night's charge (23:00-01:45, `battery_charge`, import target 7.3 kW): import
  7.20-7.29 kW, the battery 3.8-6.0 kW, SOC 20 -> 76 %.

## Test 6: hold in sunshine (2026-10-15 10:06-10:15, SOC 20 %)

The plan's `hold` at the reserve (import target 1.96 kW) had run as `grid_import`
since 08:45 and cut the PV from about 0.9 kW to 0.1 kW. From the API, a sample every
20 s; the house about 1.0-1.1 kW, PV about 1.9 kW (self-use at the end).

| Step | Command | Result |
| --- | --- | --- |
| 1 | `grid_import` 1960 W, `allow_grid_charge` false (the script's `hold`) | **PV curtailed to 0.11-0.14 kW, import 1.0 kW** = the house, the battery 0 |
| 2 | `battery_charge` 5 kW, `import_limit_watts` 0 | PV 1.65-1.75 kW, **the battery -0.45 to -0.58 kW** (the surplus), export about 0.05 kW, no import |
| 3 | `battery_hold` | PV 1.87 kW, the battery 0, **export 0.81-0.84 kW** |
| 4 | `self_consumption`, SOC 20-100 | the same as 3: the battery 0, export 0.84-0.86 kW |
| 5 | `solis_dispatch_stop` | self-use: the battery -0.73 kW, the grid about 0 |

- `grid_import` covers the house from the grid first and curtails the PV to keep the
  import, so it is no `hold` in daylight.
- `battery_charge` without import keeps the battery and stores the surplus like
  self-use (the PV slightly lower, 1.7 against 1.9 kW, partly the passing cloud):
  the script uses it for `hold` while `sun.sun` is above the horizon. Not tested: the
  PV below the house (the battery should not discharge, so the load above the import
  target is not shaved in daylight).
- `battery_hold` and `self_consumption` export the surplus instead.

## Test 7: peak shaving in a daylight hold (2026-10-24 11:13-11:21, SOC 18 -> 17 %)

The plan held at the reserve all morning (import target 1.96 kW) with `battery_charge`
without import (test 6), and the import reached 4.3 kW with the battery at 0: that
command never discharges. `grid_import` at lower targets, `allow_grid_charge` false,
`soc_min` 15, a sample every 20 s:

| Step | Command | Result |
| --- | --- | --- |
| 1 | `grid_import` 500 W | PV 1.25 kW used, the house 1.7 kW, **import 0.5 kW, the battery 0**; a step to 2.6 kW: **the battery +1.1 kW**, import 0.37 kW |
| 2 | `grid_import` 0 W | PV 0.9-1.1 kW used, the battery +0.7-0.9 kW, import 0 (about 0.12 kW export) |
| 3 | `grid_import` 1000 W | PV 0.8 kW used, the house 1.6 kW, import 0.9 kW, the battery 0 |

- `grid_import` takes the house from the grid up to the target, then from the PV,
  then from the battery. It curtails only the PV the house does not need after the
  target (test 6: the house 1.0 kW below a 1.96 kW target, so the grid covered all of
  it). Above the PV plus the target nothing is curtailed and the battery shaves.
- `sensor.solis_s6_solis_battery_current_direction` is 1 while discharging, 0 at
  rest and charging (`sensor.solis_s6_solis_battery_power` has no sign).
- So a daylight `hold` runs `battery_charge` without import, and `grid_import` at the
  target from when the house is above the PV plus the target (100 W margin) until the
  battery no longer discharges (more than 100 W); then the PV it shows is the part
  used, so the battery is the test.

## Modes through dispatch (2026-10-13, hold tested 2026-10-14)

Dispatch writes RAM only (per the integration), the other settings flash, so the aim
is every mode through dispatch, with the inverter set once to self-use, time-of-use
and peak shaving off:

| Plan mode | Dispatch |
| --- | --- |
| `charge` | `battery_charge`, `import_limit_watts` = the import target (tested) |
| `sell` | `battery_discharge`, `soc_min` = the target (tested; the window's end not yet) |
| `self_use` | none: stopped, the inverter's own self-use (tested) |
| `sell-excess` | `feed_in_priority` while the PV covers the house, else none (both tested) |
| `discard-excess` | none until the battery is full, then `self_consumption` with `export_limit_watts` 0 (tested). With room in the battery the surplus is not curtailed: the plan empties the battery before negative prices, so it fills first, and curtailing while it has room is not supported |
| `hold` | in the dark `grid_import` at the import target, `allow_grid_charge` false, `soc_min` = the shutdown SOC (tested up to 7.3 kW); in daylight `battery_charge` with `import_limit_watts` 0 up to the max SOC (test 6), and `grid_import` at the target while the house is above the PV plus the target (test 7); `battery_hold` for a manual pause slot without a target and for a target above 10 kW (`HOLD_IMPORT_MAX`, see below) |

## A hold target above the inverter's power (Oscar's S6-EH3P, Oct 2026)

Without a power charge the plan's import target is the main fuse, 11.9-12.8 kW. From
2026-10-14, when dispatch was turned on, every `hold` in the dark ran as `grid_import`
at that target, and the battery did not stay idle: it covered the house (about 1-2 kW)
or discharged at 5.6-9.5 kW, most of it to the grid (2026-10-16 02:45-03:15: 9.5 kW
out, the house 0.6 kW, SOC 57 -> 49 %; four such runs, about 13 kWh sold at night and
bought back in the next charge). Before, with peak shaving, the same holds kept the
battery at 0. The service wrote the command as asked (mode 3, power -1268 in 10 W). So
the script now sends `battery_hold` for a target above `HOLD_IMPORT_MAX` (10 kW, the
inverter's power; between 7.3 and 10 kW not tested): it keeps the battery but does not
shave the load above the target, which only matters at the fuse.

## Not tested yet

- `battery_charge` with the house above the import limit (does the battery then
  cover the excess?), and `hold` (`battery_charge` without import) with the PV below
  the house.
- Long runs and many repeated commands.

## How it is used

`examples/battery/follow_battery_action.py` with `DISPATCH = True` runs every mode
through dispatch as in the table above (`hold` as `grid_import` at the import target
without grid charging), with the inverter's
time-of-use windows, peak shaving and feed-in limit off. At the owner's
it runs as `python_scripts/follow_battery_action_dispatch.py` from the automation
"Follow battery action (dispatch)"
(`examples/battery/follow_battery_action_dispatch_automation.yml`); the old script and `automation.update_battery_action`
stay as the fallback: turn the dispatch automation off and the old one on, and the
failsafe ends any dispatch within 3 minutes.
