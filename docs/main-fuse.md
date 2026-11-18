# Main fuse

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
  (the car status ends in `finished`, Zaptec, or `complete`, the car's own Tesla BLE
  state while the charger has not seen the plug-in), no room is kept for it and its
  setting is left alone.
- **Car circuit fuse** (`number.energy_planner_ev_circuit_fuse`, 0 = none): a smaller
  fuse the house and the car share while the battery is on the main fuse's side
  (Kidde: house + car 16 A, connection 35 A). With `ev_circuit_load_sensors` (the
  circuit's load per phase, the car included) the car's room is also the circuit's,
  (fuse − margin) × 230 V − its load without the car on the busiest phase, and a
  circuit phase over it lowers the car by the excess (the battery can not take load
  off that fuse), also while the car should not charge; a car whose current is not
  reported yet (below 6 A) is lowered from its setting, not stopped. The battery's
  limit is unchanged. A missing or silent (2 min) circuit phase holds the car at 6 A
  and leaves the battery's limit live; the sensors are only read while the circuit
  fuse is set. It uses `fuse_margin` too (Kidde: 0), assumes a balanced three-phase
  car and one sensor per phase. Attribute `circuit_room_a` (the circuit's own room).
- The battery gets the rest: `grid_limit_kw` = house + car + the room left per phase
  (after the car's setting) × 3. Above it the inverter's peak shaving covers the load
  from the battery, so it also shaves house peaks in `charge` and `hold`. With
  `inverter_grid_power_sensor` (the import the inverter's own meter sees) the limit
  is shifted by what that meter misses (the parents': about 1.3 kW,
  `inverter_meter_offset_kw`).
- `fuse.smooth`: lower values apply at once (the grid limit 0.2 kW lower, so small
  rises do not write again), higher ones once a step higher (0.5 kW, 1 A) and the last
  change is 1 min (grid) / 15 min (charger, Zaptec's cloud API is rate limited) old.
- Updated on every change of the configured sensors (at most every 5 s) and at
  second 0 of every minute right after the actions (never held back by the 5 s).
  Room is kept for the car while it is plugged in, has not finished and its
  published action is `charge`; plugged in since the action was published (or
  without actions), the car action's rule decides now (`ev.ev_action`). A missing
  phase, or a meter silent for 2 min, keeps the last result (any of its phase
  sensors counts, since MQTT does not write an unchanged value).
  `sensor.energy_planner_fuse_guard`: state the room (A), attributes `grid_limit_kw`,
  `ev_current_a` (None without `ev_charger_current_number`), `phase_a`, `house_a`, `over_a`, `updated`.
- `examples/battery/follow_battery_action.py` caps the import target at `grid_limit_kw` (a
  guard older than 2 min is ignored) and its automation triggers on it;
  `follow_ev_action_automation.yml` sets the charger's current and pauses at 0. Not
  covered: `self_use` (the battery already covers the load) and manual time-of-use
  charge slots without an import target. Peak shaving does not limit the S6-EH3P's
  grid charging (see `docs/battery-plan.md`), so the cap only works with the script's
  Remote Dispatch, which the parents' firmware lacks so far.
- The parents: phases `sensor.l{1,2,3}_aktiv_effekt_uttag_momentan_effekt` /
  `_inmatning_` (HAN, kW), inverter `sensor.solis_s6_solis_ac_grid_port_power` (W,
  negative while charging) and `sensor.solis_s6_eh3p_grid_power_net` (W), car `sensor.garaget_current_phase_{1,2,3}`, setting
  `number.garaget_charger_max_current` (the installation's available current is
  unavailable to their account).
- Kidde: no HAN port; the phases are the Solis meter's (`meter_active_power_{a,b,c}`,
  negative on import), flipped by template sensors `sensor.grid_import_l{1,2,3}`. Both
  inverters are on the 35 A side, so the circuit load per phase
  (`sensor.car_circuit_load_l{1,2,3}`) = import + the hybrid's output on the phase
  (`ac_grid_port_active_power_phase_*`) + the second inverter / 3. Nothing measures
  the second inverter (19 kWp): estimated as 2 × the hybrid's PV, or the export the
  meter sees beyond the hybrid if higher. The hourly means Apr-Sep 2026 put it at 1.2-1.4
  × at midday (a lower bound: what the house uses is not seen); too high only slows
  the car in sun, too low risks the fuse. The car's current is the Easee's power / 690
  (`sensor.easee_current_a`), its setting `sensor.1_dynamic_charger_limit`, applied by
  the "Follow car charging action" automation from `ev_current_a`.
