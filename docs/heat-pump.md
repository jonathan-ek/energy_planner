# Heat pump

The air-to-air heat pump (`climate.hallen`, Mitsubishi MSZ-AP42VG) should only heat
when that is cheaper than district heating. `heat_pump.evaluate` compares a kWh of
heat: district heating at the season's energy price against electricity / COP.
`sensor.energy_planner_heat_pump_saving` is district heating minus heat pump heat
(öre per kWh of heat, positive: run the heat pump), with the COP, prices and
`break_even_spot` in the attributes. Updated every minute.

- COP: the service manual's curves at the compressor's rated frequency, defrost
  included (OBH789, 9-1): about 3.0 from −15 to +5 °C, 3.6 at +7, 4.05 at +16; below
  −15 °C the heat pump does not run (`None`). Conservative: at low output the COP is
  higher (SCOP 4.7). The owner chose the conservative curve.
- District heating, Tekniska verken 2026 incl. VAT: 99.7 öre/kWh Dec–Feb, 76.7 Mar,
  Apr, Oct, Nov, 13.0 May–Sep. The annual fee (1 350 kr + 370 kr per MWh used
  Dec–Feb, at least 6 900 kr) adds nothing per kWh here: the owner uses below 15 MWh
  in Dec–Feb (2026: Jan 2.53, Feb 2.42 MWh, from the bills). These sensors
  replace the MQTT feed (`sensor.fjarrvarme`, stuck since 2025) and the old heat pump
  automations, removed in Oct 2026.
- Electricity (saving sensor): spot × 1.25 + `tariff.buy_fee`, or, for the part of
  draw the battery plan exports this quarter, spot + `tariff.sell_fee`.
  Power charges are not included.
- Break-even with COP 3: spot about 134 öre/kWh in Oct–Nov and Mar–Apr, 189 in
  Dec–Feb; in summer only exported solar pays.
- Draw: MELCloud's counter (`sensor.hallen_energy`) is useless, it showed 0.6 kWh for
  a night the house load showed about 3.3 kWh (Oct 2026, 9 °C out: about 750 W the
  first hour, then 400–600 W). Last winter's data could not give a curve either (it
  only has that counter, and daily load varies ±6 kWh). So the draw is modelled:
  heat = loss × (indoor − outdoor), draw = heat / COP (`heat_pump.draw`, at most
  2 kW), with indoor `sensor.gw1100a_indoor_temperature` (in the hall; MELCloud's own
  room temperature reads the warm air at the unit, 16 → 23 °C while the room went
  21.5 → 21.9). The forecast measures `loss` (kW of heat per °C): hours
  `climate.hallen` heated at least 75% of, the load against the median of the same
  hour on days it did not heat (35 days of history, `forecast.heat_pump_excess`,
  capped at 2.5 kW), × COP / (indoor − outdoor), the mean over at least 6 hours with
  a difference of 3 °C or more (`heat_pump.heat_loss`). The first night gave about
  0.2 kW/K warming up and 0.14 later: 0.4 kW at 10 °C, 1.5 kW at −5 °C. The plan uses
  it per quarter with the live indoor and the forecast outdoor temperature, the saving
  sensor with both live (`actions.heat_pump_economy`); without a measurement or
  indoor temperature `heat_pump_power` (W) is used, and 0 turns planning off (the
  forecast then keeps the heat pump's draw in the house load; `heat_pump.power_kw`
  serves both). The draw per
  hour is also taken out of the load history. Not modelled: the setpoint (the room
  hardly reaches it, and it can not be fitted from a night; a changed setpoint shows
  in the measurement over the following days), warming up after an off period, and
  the living room next door (`climate.vardagsrum`, not in the statistics). Once the
  heat pump has run every day for 35 days there are no days without it to compare
  with, and the setting is used again. Its power peak costs little (0.6 kW × 45 kr/kW
  in winter days) and it nearly always pays for it.
- In the battery plan (`planning.build_plan`): a first run prices the mean draw as more
  load per quarter (`Plan.marginal`, öre/kWh: buy price, lost export, or the battery
  energy it uses, plus power charges). `heat_pump.schedule` turns it on where the
  outdoor temperature (forecast, else live) is below `heat_pump_heating_limit` and
  `district − marginal / COP ≥ heat_pump_margin`, without runs or gaps under 30 min.
  A second run plans the battery with that load added and `exempt` from the import
  limit (only power charges limit it). The plan publishes `heat_pump`, `marginal`,
  `temperature`, `heat_pump_cop` and `heat_pump_power` (kW while heating) per
  quarter.
- `sensor.energy_planner_heat_pump_action` (`on`/`off`): the plan now (source `plan`,
  `until` the end of the run); if the plan is stale or missing (e.g. no
  battery SOC), the saving sensor against the margin and the heating limit (source
  `fallback`); without a published spot price now or an outdoor temperature (just after a restart) no
  state (source `none`), so the
  automation leaves the heat pump alone.
  `examples/heat_pump/follow_heat_pump_action_automation.yml` sets `climate.hallen` to `heat` or
  `off` (only from `off` / `heat`, so cooling is left alone); the target stays at
  24 °C for now, to see how much it changes the use.
