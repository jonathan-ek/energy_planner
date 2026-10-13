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

## Not tested yet

- `script.solis_dispatch_hold_test` (evening): hold through `grid_import` without
  grid charging and `battery_hold`, `battery_charge` with the house above the import
  limit, and discharging at `soc_min`.
- Long runs and many repeated commands.

## How it is used

`examples/battery/follow_battery_action.py` with `DISPATCH = True` sends `battery_charge` with
`import_limit_watts` = the import target (capped by the fuse guard) for the plan's
`charge`, and `battery_discharge` (current × battery voltage, `soc_min` = the target)
for `sell`; everything else uses the inverter's own modes as before. At the owner's
it runs as `python_scripts/follow_battery_action_dispatch.py` from the automation
"Follow battery action (dispatch)"
(`examples/battery/follow_battery_action_dispatch_automation.yml`); the old script and `automation.update_battery_action`
stay as the fallback: turn the dispatch automation off and the old one on, and the
failsafe ends any dispatch within 3 minutes.
