# Forecast

Method and numbers come from a backtest on the real HA data (Oct 2026):

- **Load** = `sensor.solis_s6_solis_household_load_power` hourly mean minus EV
  charging (`sensor.ehwuhqtp_effekt`) and the heat pump (share of the hour
  `climate.hallen` was in `heat` mode × its modelled draw, see `docs/heat-pump.md`), which are
  planned separately. Forecast per local hour =
  mean of (last 7 days' profile, last 4 same-type days' profile; same type =
  workday/weekend), using only days before today, then split evenly into quarters.
  About 24% hourly and 14% daily error. Quarter profiles, temperature and gradient
  boosting gave no real improvement (district heating, so the load hardly depends on
  temperature). Before profiling, hours more than 3.5 kWh above the median of the same
  hour in the previous 14 days are capped (`cap_large_loads`): one-off loads like the
  sauna come from the calendar or the reserve instead, so they are not counted twice.
- **Load, temperature model** (`forecast_load_model` = `temperature`, for houses heated
  with electricity; `temperature_load_quarters`): each day's profile is scaled so its
  total changes by the heating slope × (Open-Meteo's daily mean temperature − the
  measured mean of the days behind the profile). The slope (kWh/day per °C, at most 0,
  published as `heating_slope`) is a least-squares fit of the 35 history days' load
  against their mean `outdoor_temperature_sensor`. Without Open-Meteo, or with too
  little or too even temperature history, it is the profile. Backtest on the owner's
  parents' house (electric heating, 595 days 2025-01 - 2026-09): daily error 21% ->
  18%, Nov-Mar 18% -> 13%, summer unchanged. The owner's house keeps `profile`.
- **PV** = Open-Meteo `global_tilted_irradiance` per panel plane (planes, kWp and
  location read from the Forecast.Solar config, `_solar_planes`; a disabled entry is
  left out, e.g. panels with a fault), averaged over the
  models in `OPEN_METEO_MODELS` (MET Nordic, ECMWF, ICON), derated for cell
  temperature, summed over planes, then interpolated to quarters from mid-hour points
  (keeps the hourly energy). One request per plane to the previous-runs API returns the
  latest forecast and the forecast made the day before for the last 14 days. Open-Meteo
  hourly values are means of the hour *before* the timestamp. Scaled by
  `actual / day-before forecast` over the last 14 days (actual: Solis `total_pv_power`
  with glitches above 25 kW dropped), limited to 0.3–2.5, 1.0 until 5 kWh of forecast
  exist. Day-ahead backtest (448 days): 0.63 kWh hourly, 11% daily error
  (Forecast.Solar with the same correction: 1.02 kWh, 17%).
  `forecast_pv_calibration` = `hourly` (for panels shaded part of the day;
  `pv_hourly_calibration`) uses a factor per local hour of the day instead, over the
  same 14 days, limited to 0.05-3.0; hours with under 1 kWh of forecast in the window
  keep the daily factor. The weather models assume an open horizon, so a daily factor
  spreads lost shaded hours over the whole day. Backtest on the parents' house (trees
  from about 15:00 in spring and autumn, 425 days): hourly error 1.07 -> 0.71 kWh,
  daily 14% -> 15%. Published as `pv_hourly_calibration` (hour -> factor). If Open-Meteo fails,
  Forecast.Solar `watts` with its `power_production_now` history is used instead;
  `pv_source` says which. Its PV is None on days it does not reach (so the plan ends
  there instead of planning them without sun).
- **Planned loads** come from timed events in the local calendar
  `calendar.energiplan` (recurring events work; all-day events are ignored). Energy:
  `<n> kWh` in the title or description, otherwise the sum of defaults for activities
  named in the title (`PLANNED_LOAD_DEFAULTS`: bastu 5, torktumlare 2.5, tvätt 1.5,
  disk 1, gäster 4, elbil/ladda 20 kWh), otherwise the event is ignored. The energy is
  spread evenly over the event, so the event should cover when the load runs.
- **Reserve** = `forecast_weekend_reserve` spread over Sat/Sun
  `forecast_reserve_start`–`forecast_reserve_end` (default 18–22, when the sauna
  usually runs), for unplanned weekend load. Holidays are not detected.
- **Away:** events in the same calendar whose title contains `borta`, `bortrest` or
  `away` (`forecast.AWAY_WORDS`, all-day events too) mark the quarters they cover as
  `away`: no weekend reserve there. The battery keeps `battery_reserve_soc`, and the
  load profile is not lowered.
- Output: a `forecast.Forecast` in `hass.data[DOMAIN]["forecast"]`: a `timeline` and
  per-quarter `load` (profile), `planned`, `reserve`, `pv` (kWh per quarter, all of
  today and the next two days, `FORECAST_DAYS`; the plan uses the third day once
  tomorrow's prices are published; it plans to `known_until`),
  `temperature` (Open-Meteo air temperature mid-quarter, None without Open-Meteo),
  `away` (bool per quarter), `heat_pump_loss` (measured heat loss, kW/K) and
  `ev_need_kwh`; what only the sensors show is in `details`: `heat_pump_loss_hours`,
  `planned_events`, the totals and the PV calibration. Published by
  `to_attributes` as before (local ISO `starts`). `load_tomorrow` / `load_today_remaining` = profile +
  planned (today counted from the current quarter); `base_tomorrow` and
  `planned_tomorrow` split it. The sensors expose the lists as attributes, which are
  kept out of the recorder. The Energi dashboard has a "Prognos" view
  (ApexCharts) that draws them.
- **Yesterday's forecast** is rebuilt on every update instead of stored: the load
  profile only uses earlier days and the calendar still has yesterday's events, and the
  PV forecast is Open-Meteo's day-before run (`previous`, hence `past_days` 15)
  calibrated on the 14 days before yesterday (empty with the Forecast.Solar fallback).
  Output: `yesterday_starts`, `yesterday_load` (profile + planned), `yesterday_pv` and
  the totals `yesterday_{load,pv}_kwh` / `yesterday_{load,pv}_actual_kwh` (actual load
  without EV). The totals are recorded, so forecast accuracy builds up in history.
