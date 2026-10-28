# Battery plan

`battery_optimizer.optimize`, published as `sensor.energy_planner_battery_plan`.
`sensor.energy_planner_battery_action` resolves what to do now
(`battery_action.resolve`): a manual slot covering now wins (see `docs/architecture.md` "Manual slots");
with `enabled` on, the plan (self-use if the plan is stale, see `plan.py`, or ends);
with it off, self-use. An inverter script follows the action sensor
(`examples/battery/follow_battery_action.py`).

- Horizon: the current quarter until the last quarter with both a price and a load
  forecast. Load = forecast profile + planned + reserve; PV = forecast.
- The day after the published prices uses an estimate (`price_estimate.estimate`,
  `prices._estimated_prices`): tomorrow until its prices are published (about 13:00), then
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
  surplus goes to the grid instead of the battery, action state `sell-excess`: worth it
  while prices fall during the day and the battery will fill later anyway; a full
  battery exports in `self_use`, which wins ties), `discard_excess` (like `self_use`,
  but surplus the battery cannot take is curtailed, action state `discard-excess`: when
  the export price, spot plus the network compensation, is negative; the plan also
  empties the battery before such hours when that pays), `charge` (from PV and grid
  up to the import target), `sell` (full discharge to the grid).
  Action states (`battery_action.ACTION_STATES`): `self_use` → `discharge`, `hold` →
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
  `docs/car.md`). `Vehicle`: charger powers (`charger_powers(min, max)`, 0 or one
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
