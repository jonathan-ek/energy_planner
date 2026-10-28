# The households and grid tariffs

## Household and tariff

Background for the planner (from the owner, Oct 2026):

- Location Linköping (58.38 N, 15.66 E), price area SE3, grid operator Tekniska verken.
- Main heating is district heating (fjärrvärme). An air-to-air heat pump ("Hallen",
  MELCloud `climate.hallen`, energy `sensor.hallen_energy`) can heat or cool part of
  the house; it should heat when that is cheaper than district heating (see "Heat
  pump").
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
charging the battery from the grid in the day costs far more than at night. The heat
pump's load is exempt from it (Oct 2026): its saving pays for the higher peak. Later, the
planner should also be able to shift controllable loads (e.g. the heat pump) to stay
under it.

The owner's brother uses E.ON Energidistribution (Virserum), preset `eon_20a` from his
price list: 32.30 öre/kWh transfer fee (+ 45 öre energy tax, assumed), 835 kr/month,
no power charge, selling spot + 10 öre. He has separate meters on the spa and the
ground-source heat pump, which could later feed load patterns. His forecast (backtest
Oct 2026): the load follows the outdoor temperature (about -2.2 kWh/day per °C, 20
kWh/day above 15 °C, 66 below 0), so he uses the `temperature` model (daily error 22% ->
19%, Nov-Mar 18% -> 14%), with the Wallbox power as `forecast_ev_sensor` and, until he
has an outdoor sensor, a template sensor from his SMHI weather entity (history imported
from Open-Meteo). PV: the inverter clips at about 10 kW, "Platta" is shaded in the
afternoon (`hourly` calibration) and "Frigge 1/2" seem faulty, so their Forecast.Solar
entries are disabled. The model covers the common Swedish variants: mean of the N highest hourly means per
month, optionally at most one per day, limited by months, weekdays and hours.
Holidays are not detected.
