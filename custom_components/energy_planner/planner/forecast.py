"""Consumption and PV forecast per quarter, without Home Assistant dependencies.

Backtested on a year of data (see AGENTS.md): blending a 7-day hourly profile with
a profile of the last same-type days (workday / weekend) gave the best day-ahead
consumption forecast, and splitting the hour evenly into quarters is as good as any
quarter profile. For houses heated with electricity the profile can be corrected for
the outdoor temperature (temperature_load_quarters).

PV comes from Open-Meteo's tilted irradiance per panel plane, averaged over three
weather models (day-ahead backtest: 11% daily error against 17% for Forecast.Solar).
Forecast.Solar is the fallback. Either is scaled by the actual/forecast ratio of the
last two weeks, which absorbs the season, shading and snow.

Large one-off loads (sauna, washing) are not forecast from the profile: they are
capped in the history and added back from calendar events (planned loads) or covered
by a reserve.
"""

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from itertools import pairwise
from statistics import mean, median

QUARTER = dt.timedelta(minutes=15)

# Profile settings
PROFILE_DAYS = 7  # days in the recent profile
PROFILE_MIN_DAYS = 4
SAME_TYPE_DAYS = 4  # same-type days in the day-type profile
SAME_TYPE_MIN_DAYS = 2
SAME_TYPE_LOOKBACK_DAYS = 35

# Open-Meteo weather models to average. Adding GFS did not improve the backtest
OPEN_METEO_MODELS = ("metno_seamless", "ecmwf_ifs025", "icon_seamless")
# Power loss per °C of cell temperature above 25 °C
TEMPERATURE_COEFFICIENT = 0.004

# Load models (setting forecast_load_model): the profile alone, or corrected for the
# outdoor temperature (temperature_load_quarters)
LOAD_MODELS = ("profile", "temperature")
# Temperature model: a day with at least this many hours counts as complete
DAY_MIN_HOURS = 20
# ... days with a temperature needed to fit the heating slope
TEMPERATURE_MIN_DAYS = 14
# ... standard deviation (°C) of their temperatures needed to trust the fit
TEMPERATURE_MIN_SPREAD = 1.0

# PV calibration settings
CALIBRATION_MIN_FORECAST_KWH = 5.0
CALIBRATION_LIMITS = (0.3, 2.5)
# PV calibrations (setting forecast_pv_calibration): one factor for the day, or one
# per local hour of the day (pv_hourly_calibration), for panels shaded part of the day
PV_CALIBRATIONS = ("daily", "hourly")
# Hourly calibration: forecast energy an hour of the day needs over the window, and
# the factor's range (shade can take most of an hour)
HOURLY_CALIBRATION_MIN_FORECAST_KWH = 1.0
HOURLY_CALIBRATION_LIMITS = (0.05, 3.0)

# Hours more than this above the median of the same hour in the previous two weeks
# are capped in the history. Backtested: no loss of accuracy, removes ~0.1 kWh/day
LARGE_LOAD_MARGIN_KWH = 3.5
LARGE_LOAD_REFERENCE_DAYS = 14
LARGE_LOAD_MIN_REFERENCES = 7

# Energy for a planned load without "<n> kWh" in its title or description, matched
# on the words of the title (prefix match for keywords of 4+ letters). Every matching
# activity is added, so "Tvätt och tork" is laundry plus dryer
PLANNED_LOAD_DEFAULTS = (
    (("bastu", "sauna"), 5.0),
    (("torktumlare", "tork", "dryer"), 2.5),
    (("tvätt", "tvatt", "laundry"), 1.5),
    (("disk", "dishwasher"), 1.0),
    (("gäster", "gaster", "guests", "fest", "party"), 4.0),
    (("elbil", "laddning", "ladda", "ev", "charge"), 20.0),
)
ENERGY_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*kwh", re.IGNORECASE)
# Calendar events (also all-day) whose title contains one of these (lower case) mark
# that nobody is home: no reserve for unplanned load
AWAY_WORDS = ("borta", "bortrest", "away")

# Heat pump draw, measured from the house load (its own energy counter, MELCloud,
# showed a fifth of it): hours it heated at least this share, against the median of
# the same hour on days it did not heat
HEAT_PUMP_MIN_SHARE = 0.75
HEAT_PUMP_MIN_REFERENCES = 3
# Per hour; more is another load (the MSZ-AP42 draws at most about 1.9 kW)
HEAT_PUMP_MAX_KW = 2.5


def is_offday(day: dt.date) -> bool:
    """Weekend; holidays are not known here."""
    return day.weekday() >= 5


def quarter_starts(start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    """Quarter starts from the quarter containing start up to end (exclusive).

    Works on UTC so a daylight saving change gives 92 or 100 quarters as it should.
    """
    tz = start.tzinfo
    current = start.astimezone(dt.UTC)
    current = current.replace(
        minute=current.minute - current.minute % 15, second=0, microsecond=0
    )
    end = end.astimezone(dt.UTC)
    result = []
    while current < end:
        result.append(current.astimezone(tz))
        current += QUARTER
    return result


def hourly_load_forecast(
    history: dict[tuple[dt.date, int], float], day: dt.date, hour: int, today: dt.date
) -> float | None:
    """Forecast the energy (kWh) for one local hour.

    history maps (local date, local hour) to kWh. Only days before today are used, so
    the forecast for today and tomorrow is based on the same complete days.
    """
    recent_days, same_type_days = _profile_days(history, day, hour, today)
    recent = [history[(d, hour)] for d in recent_days]
    same_type = [history[(d, hour)] for d in same_type_days]
    profiles = []
    if len(recent) >= PROFILE_MIN_DAYS:
        profiles.append(mean(recent))
    if len(same_type) >= SAME_TYPE_MIN_DAYS:
        profiles.append(mean(same_type))
    if profiles:
        return mean(profiles)
    # Not enough history yet, use whatever exists for this hour
    fallback = [kwh for (_, h), kwh in history.items() if h == hour]
    return mean(fallback) if fallback else None


def _profile_days(
    history: dict[tuple[dt.date, int], float], day: dt.date, hour: int, today: dt.date
) -> tuple[list[dt.date], list[dt.date]]:
    """Return the days hourly_load_forecast uses for an hour: recent and same-type."""
    recent = [
        today - dt.timedelta(days=i)
        for i in range(1, PROFILE_DAYS + 1)
        if (today - dt.timedelta(days=i), hour) in history
    ]
    same_type = []
    for i in range(1, SAME_TYPE_LOOKBACK_DAYS + 1):
        candidate = today - dt.timedelta(days=i)
        if is_offday(candidate) == is_offday(day) and (candidate, hour) in history:
            same_type.append(candidate)
            if len(same_type) == SAME_TYPE_DAYS:
                break
    return recent, same_type


def daily_means(hourly: Mapping[tuple[dt.date, int], float]) -> dict[dt.date, float]:
    """Return the mean per local date of hourly values, for days with most hours."""
    days: dict[dt.date, list[float]] = {}
    for (day, _), value in hourly.items():
        days.setdefault(day, []).append(value)
    return {
        day: mean(values)
        for day, values in days.items()
        if len(values) >= DAY_MIN_HOURS
    }


def heating_slope(
    history: dict[tuple[dt.date, int], float],
    temperatures: Mapping[dt.date, float],
    today: dt.date,
) -> float:
    """Return the daily load change per °C warmer outdoors (kWh/°C, at most 0).

    A least-squares line through each complete history day before today: its load
    against its mean outdoor temperature, over the last SAME_TYPE_LOOKBACK_DAYS days.
    0 (no correction) with fewer than TEMPERATURE_MIN_DAYS such days, when their
    temperatures hardly differ, or when the load rises with the temperature (cooling
    is not modelled).
    """
    hours: dict[dt.date, list[float]] = {}
    for (day, _), kwh in history.items():
        hours.setdefault(day, []).append(kwh)
    points = [
        (temperatures[day], sum(values))
        for i in range(1, SAME_TYPE_LOOKBACK_DAYS + 1)
        if (day := today - dt.timedelta(days=i)) in temperatures
        and len(values := hours.get(day, [])) >= DAY_MIN_HOURS
    ]
    if len(points) < TEMPERATURE_MIN_DAYS:
        return 0.0
    x_mean = mean(x for x, _ in points)
    y_mean = mean(y for _, y in points)
    spread = sum((x - x_mean) ** 2 for x, _ in points)
    if spread < TEMPERATURE_MIN_SPREAD**2 * len(points):
        return 0.0
    slope = sum((x - x_mean) * (y - y_mean) for x, y in points) / spread
    return min(slope, 0.0)


def temperature_load_quarters(
    history: dict[tuple[dt.date, int], float],
    quarters: list[dt.datetime],
    today: dt.date,
    measured: Mapping[dt.date, float],
    expected: Mapping[dt.date, float],
) -> list[float | None]:
    """Forecast kWh per quarter like load_quarters, corrected for the temperature.

    For houses heated with electricity (backtested on such a house: daily error 21%
    -> 18%, in Nov-Mar 18% -> 13%; a district heated house hardly changes). The
    profile is built from earlier days, so a cold day after a mild week is
    under-forecast. Each day's profile is scaled so its total changes by
    heating_slope * (expected temperature - the mean temperature of the days behind
    the profile), the recent and same-type days averaged like the profiles.

    measured is the daily mean outdoor temperature of the history days, expected the
    forecast daily mean of the forecast days. Days missing either keep the profile.
    quarters should cover whole local days, as the correction is spread over the
    quarters of a day in the list.
    """
    load = load_quarters(history, quarters, today)
    slope = heating_slope(history, measured, today)
    if not slope:
        return load
    days: dict[dt.date, list[int]] = {}
    for i, start in enumerate(quarters):
        days.setdefault(start.date(), []).append(i)
    for day, indices in days.items():
        total = sum(load[i] or 0.0 for i in indices)
        # The noon hour stands for the day: history days are complete apart from gaps
        groups = _profile_days(history, day, 12, today)
        references = [
            mean(measured[d] for d in group if d in measured)
            for group in groups
            if any(d in measured for d in group)
        ]
        if day not in expected or not references or total <= 0:
            continue
        change = slope * (expected[day] - mean(references))
        factor = max(1 + change / total, 0.0)
        for i in indices:
            value = load[i]
            if value is not None:
                load[i] = value * factor
    return load


def cap_large_loads(
    history: dict[tuple[dt.date, int], float],
) -> dict[tuple[dt.date, int], float]:
    """Cap hours far above the usual level for that hour (one-off large loads)."""
    capped = {}
    for (day, hour), kwh in history.items():
        references = [
            history[(day - dt.timedelta(days=i), hour)]
            for i in range(1, LARGE_LOAD_REFERENCE_DAYS + 1)
            if (day - dt.timedelta(days=i), hour) in history
        ]
        if len(references) >= LARGE_LOAD_MIN_REFERENCES:
            kwh = min(kwh, median(references) + LARGE_LOAD_MARGIN_KWH)
        capped[(day, hour)] = kwh
    return capped


def heating_shares(
    changes: Sequence[tuple[dt.datetime, bool]], end: dt.datetime
) -> dict[dt.datetime, float]:
    """Return the share of each hour the heat pump heated, keyed by UTC hour start.

    `changes` are (time, heating) state changes in time order, `end` closes the last.
    Hours it did not heat are left out.
    """
    shares: dict[dt.datetime, float] = {}
    bounds = [*changes, (end, False)]
    for (start, heating), (stop, _) in pairwise(bounds):
        if not heating:
            continue
        moment = start.astimezone(dt.UTC)
        stop = stop.astimezone(dt.UTC)
        while moment < stop:
            hour = moment.replace(minute=0, second=0, microsecond=0)
            until = min(hour + dt.timedelta(hours=1), stop)
            shares[hour] = shares.get(hour, 0.0) + (until - moment) / dt.timedelta(
                hours=1
            )
            moment = until
    return shares


def heat_pump_excess(
    history: Mapping[tuple[dt.date, int], float],
    shares: Mapping[tuple[dt.date, int], float],
) -> dict[tuple[dt.date, int], float]:
    """Measure what the heat pump drew (kW) in the hours it heated, from the load.

    `history` is the load per (local date, hour) without the heat pump taken out,
    `shares` the share of each hour it heated. Each hour it heated most of is compared
    with the median of the same hour on days it did not heat at all, scaled to a whole
    hour of heating and capped at HEAT_PUMP_MAX_KW.
    """
    references: dict[int, list[float]] = {}
    for (day, hour), kw in history.items():
        if not shares.get((day, hour)):
            references.setdefault(hour, []).append(kw)
    excess = {}
    for key, share in shares.items():
        if share < HEAT_PUMP_MIN_SHARE or key not in history:
            continue
        if len(references.get(key[1], [])) < HEAT_PUMP_MIN_REFERENCES:
            continue
        kw = (history[key] - median(references[key[1]])) / share
        excess[key] = min(kw, HEAT_PUMP_MAX_KW)
    return excess


def load_quarters(
    history: dict[tuple[dt.date, int], float],
    quarters: list[dt.datetime],
    today: dt.date,
) -> list[float | None]:
    """Forecast kWh per quarter: the hourly forecast split evenly over its quarters."""
    cache = {}
    result = []
    for start in quarters:
        key = (start.date(), start.hour)
        if key not in cache:
            cache[key] = hourly_load_forecast(history, start.date(), start.hour, today)
        hourly = cache[key]
        result.append(None if hourly is None else hourly / 4)
    return result


def _power_at(points: list[tuple[float, float]], ts: float) -> float:
    """Linear interpolation of (timestamp, W) points, 0 outside them."""
    if not points or ts < points[0][0] or ts > points[-1][0]:
        return 0.0
    for (t0, p0), (t1, p1) in pairwise(points):
        if t0 <= ts <= t1:
            return p0 if t1 == t0 else p0 + (p1 - p0) * (ts - t0) / (t1 - t0)
    return points[-1][1]


def pv_quarters(
    watts: Mapping[dt.datetime, float], quarters: list[dt.datetime]
) -> list[float]:
    """Integrate a power forecast (W at timestamps) to kWh per quarter (Simpson)."""
    points = sorted((t.timestamp(), float(w)) for t, w in watts.items())
    result = []
    for start in quarters:
        t0 = start.timestamp()
        t1 = t0 + QUARTER.total_seconds()
        avg_w = (
            _power_at(points, t0)
            + 4 * _power_at(points, (t0 + t1) / 2)
            + _power_at(points, t1)
        ) / 6
        result.append(avg_w * 0.25 / 1000)
    return result


def pv_calibration(
    actual: dict[dt.datetime, float], forecast: dict[dt.datetime, float]
) -> float:
    """Actual / forecast PV energy over the hours both cover, limited to a sane range.

    Returns 1.0 until there is enough forecast energy to compare against.
    """
    hours = [
        h
        for h in actual.keys() & forecast.keys()
        if actual[h] > 0.05 or forecast[h] > 0.05
    ]
    total_forecast = sum(forecast[h] for h in hours)
    if total_forecast < CALIBRATION_MIN_FORECAST_KWH:
        return 1.0
    ratio = sum(actual[h] for h in hours) / total_forecast
    return min(max(ratio, CALIBRATION_LIMITS[0]), CALIBRATION_LIMITS[1])


def pv_hourly_calibration(
    actual: Mapping[tuple[dt.date, int], float],
    forecast: Mapping[tuple[dt.date, int], float],
) -> dict[int, float]:
    """Actual / forecast PV energy per local hour of the day, limited to a range.

    For panels shaded part of the day (trees, buildings): the weather models assume
    an open horizon, so a single daily factor spreads the lost hours over the whole
    day. Keys are (local date, hour) over the calibration window. An hour of the day
    with less than HOURLY_CALIBRATION_MIN_FORECAST_KWH of forecast in the window is
    left out, and the daily factor is used there. Backtested on a house shaded from
    about 15:00 (Mar-Oct): hourly error 1.07 -> 0.71 kWh, daily 14% -> 15%.
    """
    totals: dict[int, list[float]] = {}
    for key in actual.keys() & forecast.keys():
        sums = totals.setdefault(key[1], [0.0, 0.0])
        sums[0] += actual[key]
        sums[1] += forecast[key]
    low, high = HOURLY_CALIBRATION_LIMITS
    return {
        hour: min(max(produced / predicted, low), high)
        for hour, (produced, predicted) in totals.items()
        if predicted >= HOURLY_CALIBRATION_MIN_FORECAST_KWH
    }


def calibrated_quarters(
    raw: Sequence[float],
    quarters: Sequence[dt.datetime],
    daily: float,
    hourly: Mapping[int, float] | None = None,
) -> list[float]:
    """Scale raw PV per quarter by its local hour's factor, else the daily factor."""
    hourly = hourly or {}
    return [
        value * hourly.get(start.hour, daily)
        for value, start in zip(raw, quarters, strict=True)
    ]


def reserve_quarters(
    quarters: list[dt.datetime],
    reserve_kwh: float,
    start_hour: int = 18,
    end_hour: int = 22,
    away: Sequence[bool] | None = None,
) -> list[float]:
    """Spread a reserve for unplanned weekend load evenly over the weekend window.

    Quarters marked `away` (nobody home) get none.
    """
    if end_hour <= start_hour:
        return [0.0] * len(quarters)
    away = away if away is not None else [False] * len(quarters)
    per_quarter = reserve_kwh / ((end_hour - start_hour) * 4)
    return [
        per_quarter
        if is_offday(q.date()) and start_hour <= q.hour < end_hour and not gone
        else 0.0
        for q, gone in zip(quarters, away, strict=True)
    ]


def is_away_event(summary: str) -> bool:
    """Return whether a calendar event title says nobody is home."""
    title = (summary or "").lower()
    return any(word in title for word in AWAY_WORDS)


def away_quarters(
    periods: Sequence[tuple[dt.datetime, dt.datetime]], quarters: list[dt.datetime]
) -> list[bool]:
    """Return per quarter whether it starts inside one of the (start, end) periods."""
    return [any(start <= q < end for start, end in periods) for q in quarters]


def planned_event_energy(summary: str, description: str = "") -> float | None:
    """Energy (kWh) of a planned load.

    "<n> kWh" in the title or description wins, otherwise the sum of the defaults for
    the activities named in the title, otherwise None (not an energy event).
    """
    for text in (summary or "", description or ""):
        if match := ENERGY_PATTERN.search(text):
            return float(match.group(1).replace(",", "."))
    words = re.findall(r"\w+", (summary or "").lower())
    matched = [
        kwh
        for keywords, kwh in PLANNED_LOAD_DEFAULTS
        if any(
            word == keyword or (len(keyword) >= 4 and word.startswith(keyword))
            for word in words
            for keyword in keywords
        )
    ]
    return sum(matched) if matched else None


def planned_quarters(
    events: list[tuple[dt.datetime, dt.datetime, float]], quarters: list[dt.datetime]
) -> list[float]:
    """Spread each (start, end, kWh) event evenly over the time it covers."""
    result = [0.0] * len(quarters)
    for start, end, kwh in events:
        duration = (end - start).total_seconds()
        if duration <= 0:
            continue
        for i, quarter in enumerate(quarters):
            overlap = (
                min(end, quarter + QUARTER) - max(start, quarter)
            ).total_seconds()
            if overlap > 0:
                result[i] += kwh * overlap / duration
    return result


def open_meteo_azimuth(compass: float) -> float:
    """Convert a compass azimuth (0 = north) to Open-Meteo's (0 = south)."""
    return compass % 360 - 180


def plane_power(
    irradiance: float, kwp: float, temperature: float | None = None
) -> float:
    """Mean power (kW) of a panel plane from tilted irradiance (W/m²).

    Hot cells produce less, about TEMPERATURE_COEFFICIENT per °C above 25 °C. The cell
    temperature is estimated as air temperature plus 25 °C at 1000 W/m².
    """
    kw = kwp * irradiance / 1000
    if temperature is not None:
        cell = temperature + 25 * irradiance / 1000
        kw *= 1 - TEMPERATURE_COEFFICIENT * (cell - 25)
    return max(kw, 0.0)


def open_meteo_plane_power(
    hourly: dict, kwp: float, models: tuple[str, ...] = OPEN_METEO_MODELS
) -> tuple[dict[dt.datetime, float], dict[dt.datetime, float]]:
    """Return (latest forecast, forecast from the day before) in kW per UTC hour start.

    hourly is Open-Meteo's "hourly" block with global_tilted_irradiance,
    temperature_2m and their _previous_day1 variants per model, in UTC. Its values are
    means over the hour before the timestamp, so they are moved back one hour. The
    models are averaged, skipping missing values.
    """

    def ensemble(variable, i):
        values = [
            series[i]
            for model in models
            if (series := hourly.get(f"{variable}_{model}")) is not None
            and series[i] is not None
        ]
        return mean(values) if values else None

    latest, previous = {}, {}
    for i, time in enumerate(hourly["time"]):
        hour = dt.datetime.fromisoformat(time).replace(tzinfo=dt.UTC) - dt.timedelta(
            hours=1
        )
        for target, suffix in ((latest, ""), (previous, "_previous_day1")):
            irradiance = ensemble(f"global_tilted_irradiance{suffix}", i)
            if irradiance is None:
                continue
            temperature = ensemble(f"temperature_2m{suffix}", i)
            target[hour] = plane_power(irradiance, kwp, temperature)
    return latest, previous


def open_meteo_temperature(
    hourly: dict, models: tuple[str, ...] = OPEN_METEO_MODELS
) -> dict[dt.datetime, float]:
    """Return the latest forecast's air temperature (°C) per UTC timestamp.

    temperature_2m is the value at the timestamp (not a mean), averaged over the
    models, skipping missing values.
    """
    temperatures = {}
    for i, time in enumerate(hourly["time"]):
        values = [
            series[i]
            for model in models
            if (series := hourly.get(f"temperature_2m_{model}")) is not None
            and series[i] is not None
        ]
        if values:
            moment = dt.datetime.fromisoformat(time).replace(tzinfo=dt.UTC)
            temperatures[moment] = mean(values)
    return temperatures


def temperature_quarters(
    temperatures: Mapping[dt.datetime, float], quarters: list[dt.datetime]
) -> list[float | None]:
    """Interpolate point temperatures to the middle of each quarter (None outside)."""
    points = sorted((m.timestamp(), value) for m, value in temperatures.items())
    result = []
    for start in quarters:
        middle = (start + QUARTER / 2).timestamp()
        if not points or not points[0][0] <= middle <= points[-1][0]:
            result.append(None)
            continue
        result.append(_power_at(points, middle))
    return result


def hourly_power_to_watts(hourly_kw: Mapping[dt.datetime, float]) -> dict:
    """Place hourly mean power (kW) mid-hour, in W, as input for pv_quarters."""
    half = dt.timedelta(minutes=30)
    return {hour + half: kw * 1000 for hour, kw in hourly_kw.items()}
