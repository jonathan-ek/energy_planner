# Car

While the car is plugged in, the battery plan plans it with the battery (`planning.vehicle`,
`Vehicle` in the optimizer): its need by the next
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
Numbers: `ev_charge_limit` (%, default 100, the target without a limit sensor) and
`ev_soc` (%, the SOC entered for this session, also by the `set_ev_soc` service with
an optional charge limit; stored with the session energy then in
`ev_soc_session_kwh`, 0 when entered before plugging in, and cleared when the charger
says unplugged, not when it is unavailable).

- Need: (limit − SOC) × capacity / 0.9 when the car reports its SOC (the limit
  sensor, else `ev_charge_limit`); otherwise the same from the entered `ev_soc` minus
  the session energy delivered since it was entered (source `entered`; not planned
  once that is used up); otherwise the
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
  `need_kwh`, `need_source` (`soc`/`entered`/`history`/`trip`), `ready` (the deadline, also past the
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
