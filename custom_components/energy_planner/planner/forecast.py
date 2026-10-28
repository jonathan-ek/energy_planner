"""Consumption and PV forecast per quarter, without Home Assistant dependencies.

Backtested on a year of data (see docs/forecast.md): blending a 7-day hourly profile
with a profile of the last same-type days (workday / weekend) gave the best day-ahead
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

import dataclasses
import datetime as dt
import functools
import re
from collections.abc import Mapping, Sequence
from itertools import pairwise
from statistics import mean, median
from typing import Any

from . import ev as ev_need
from . import heat_pump
from .timeline import QUARTER, Timeline, at_local


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
    """Return per quarter whether it starts inside one of the (start, end) periods.

    Compared as instants, so the hour repeated when DST ends is not taken twice.
    """
    spans = [(start.timestamp(), end.timestamp()) for start, end in periods]
    return [any(start <= q.timestamp() < end for start, end in spans) for q in quarters]


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
    """Spread each (start, end, kWh) event evenly over the time it covers.

    In timestamps: local times compare and add by wall clock (see timeline.py).
    """
    result = [0.0] * len(quarters)
    starts = [quarter.timestamp() for quarter in quarters]
    for start, end, kwh in events:
        first, last = start.timestamp(), end.timestamp()
        duration = last - first
        if duration <= 0:
            continue
        for i, quarter in enumerate(starts):
            overlap = min(last, quarter + QUARTER.total_seconds()) - max(first, quarter)
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
        middle = start.timestamp() + QUARTER.total_seconds() / 2
        if not points or not points[0][0] <= middle <= points[-1][0]:
            result.append(None)
            continue
        result.append(_power_at(points, middle))
    return result


def hourly_power_to_watts(hourly_kw: Mapping[dt.datetime, float]) -> dict:
    """Place hourly mean power (kW) mid-hour, in W, as input for pv_quarters."""
    half = dt.timedelta(minutes=30)
    return {hour + half: kw * 1000 for hour, kw in hourly_kw.items()}


# The forecast covers today and the next two days: the battery plan uses the third
# day once tomorrow's prices are published (it then estimates that day's prices)
FORECAST_DAYS = 3
# Days of PV history the calibration compares with the forecast
CALIBRATION_DAYS = 14
# Hourly PV means above this are Modbus glitches
PV_MAX_KW = 25


@dataclasses.dataclass(frozen=True)
class ForecastInputs:
    """What a forecast is made from, fetched by forecast_update.py.

    Hourly means are kW (= kWh for the hour) keyed by the UTC hour start.
    """

    now: dt.datetime
    tz: dt.tzinfo
    load: Mapping[dt.datetime, float]  # house load, the car and heat pump included
    pv: Mapping[dt.datetime, float]  # produced
    ev: Mapping[dt.datetime, float]  # car charging
    indoor: Mapping[dt.datetime, float]  # °C in the heat pump's room
    outdoor: Mapping[dt.datetime, float]  # °C
    heating_shares: Mapping[dt.datetime, float]  # share of each hour it heated
    # (start, end, kWh, summary) from the calendar
    planned_loads: Sequence[tuple[dt.datetime, dt.datetime, float, str]]
    away_periods: Sequence[tuple[dt.datetime, dt.datetime]]
    # Open-Meteo's latest and day-before PV power (kW per UTC hour, all planes) and
    # air temperature per UTC timestamp; None without planes or when it failed
    open_meteo: tuple[Mapping, Mapping, Mapping] | None
    # Forecast.Solar per entry: its `watts` forecast and the history of its power
    # production estimate (kW per UTC hour, empty without the sensor)
    forecast_solar: Sequence[tuple[Mapping[dt.datetime, float], Mapping]]
    load_model: str  # LOAD_MODELS
    pv_calibration: str  # PV_CALIBRATIONS
    weekend_reserve_kwh: float
    reserve_start: int
    reserve_end: int
    heat_pump_power_kw: float  # the setting, 0: not planned
    cop_curve: Sequence[tuple[float, float]]


@dataclasses.dataclass(frozen=True)
class Forecast:
    """Load and PV per quarter (kWh) on a timeline, and what the sensors publish.

    `load` is the profile; `planned` (calendar) and `reserve` come on top. `pv` is
    None without a PV source, and a quarter's PV is None where Forecast.Solar does
    not reach. `yesterday` is yesterday's forecast rebuilt as it was made (load,
    planned and PV). `details` are the totals and calibration only the sensors show
    (yesterday's actual totals too), as `to_attributes` publishes them.
    """

    updated: dt.datetime
    timeline: Timeline
    load: tuple[float | None, ...]
    planned: tuple[float, ...] = ()
    reserve: tuple[float, ...] = ()
    away: tuple[bool, ...] = ()
    pv: tuple[float | None, ...] | None = None
    temperature: tuple[float | None, ...] | None = None  # °C mid-quarter
    heat_pump_loss: float | None = None  # kW of heat per °C, measured
    ev_need_kwh: float | None = None  # the car's need from its charging nights
    yesterday: "Forecast | None" = None
    details: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        """Check that every series given has a value per quarter."""
        for name in ("load", "planned", "reserve", "away", "pv", "temperature"):
            series = getattr(self, name)
            if series and len(series) != len(self.timeline):
                raise ValueError(
                    f"{name} has {len(series)} values, not {len(self.timeline)}"
                )

    def known_until(self, moment: dt.datetime) -> dt.datetime:
        """Return the end (UTC) of the quarters from `moment` with load and PV known.

        `moment` itself if its quarter is not known (or outside the forecast).
        """
        index = self.timeline.index_at(moment)
        if index is None:
            return moment
        last = None
        for i in range(index, len(self.timeline)):
            if self.load[i] is None or (self.pv is not None and self.pv[i] is None):
                break
            last = i
        return moment if last is None else self.timeline.quarter_end(last)

    def to_attributes(self) -> dict[str, Any]:
        """Return everything the two forecast sensors publish (local ISO times).

        Built once and shared: read only.
        """
        return self._attributes

    @functools.cached_property
    def _attributes(self) -> dict[str, Any]:
        details = dict(self.details)
        yesterday = self.yesterday
        return {
            "updated": self.updated.isoformat(),
            "starts": self.timeline.isoformat(),
            "load": list(self.load),
            "planned": list(self.planned),
            "reserve": list(self.reserve),
            "away": list(self.away),
            "pv": None if self.pv is None else list(self.pv),
            "temperature": None if self.temperature is None else list(self.temperature),
            "heat_pump_loss": self.heat_pump_loss,
            "ev_need_kwh": self.ev_need_kwh,
            **details,
            "yesterday_starts": yesterday.timeline.isoformat() if yesterday else [],
            # Profile plus planned loads, what was expected
            "yesterday_load": None
            if yesterday is None
            else [
                None if base is None else round(base + extra, 3)
                for base, extra in zip(yesterday.load, yesterday.planned, strict=True)
            ],
            "yesterday_pv": None
            if yesterday is None or yesterday.pv is None
            else list(yesterday.pv),
        }


def _local_hours(
    hourly: Mapping[dt.datetime, float], tz: dt.tzinfo
) -> dict[tuple[dt.date, int], float]:
    """Key hourly values by (local date, hour) instead of the UTC hour start."""
    result = {}
    for hour, value in hourly.items():
        local = hour.astimezone(tz)
        result[(local.date(), local.hour)] = value
    return result


def _rounded(values, digits: int = 3):
    """Round a series, keeping None (the series and its values)."""
    if values is None:
        return None
    return tuple(None if v is None else round(v, digits) for v in values)


def _total(values) -> float | None:
    """Sum a series, None if it has no values."""
    if values is None or all(v is None for v in values):
        return None
    return round(sum(v for v in values if v is not None), 2)


def _daily_totals(timeline: Timeline, values, since: dt.datetime) -> dict:
    """Sum per local date, counting only the quarters from `since` on."""
    first = timeline.index_at(since)
    first = 0 if first is None else first
    totals: dict[dt.date, float] = {}
    for index in range(first, len(timeline)):
        value = values[index]
        if value is not None:
            day = timeline.local_date(index)
            totals[day] = totals.get(day, 0.0) + value
    return totals


def _day_total(hourly, start: dt.datetime, end: dt.datetime) -> float | None:
    """Sum hourly kWh from start to end, None if more than an hour is missing."""
    start_utc, end_utc = start.astimezone(dt.UTC), end.astimezone(dt.UTC)
    values = [kw for hour, kw in hourly.items() if start_utc <= hour < end_utc]
    if len(values) < (end_utc - start_utc) / dt.timedelta(hours=1) - 1:
        return None
    return round(sum(values), 2)


def build_forecast(inputs: ForecastInputs) -> Forecast:
    """Forecast load and PV per quarter for all of today and the next two days.

    Today's forecast only uses days before today, so it is the same all day and can
    be compared with what actually happens. Totals for today count from now.

    1. Load history: the house load minus the car's charging and the heat pump
       (the share of each hour it heated times its draw, measured from the load
       where possible: `heat_pump.heat_loss`), large one-off loads capped.
    2. Load per quarter from the profile (`load_quarters`), or with the
       temperature model corrected for the outdoor temperature; planned loads from
       the calendar and the weekend reserve (not while away) on top.
    3. PV from Open-Meteo, else Forecast.Solar, scaled by the actual / forecast
       ratio of the last `CALIBRATION_DAYS` (per hour of the day with the hourly
       calibration). Forecast.Solar's PV is None past the last day it reaches.
    4. Yesterday rebuilt as it was: the same profile, the calendar, and Open-Meteo's
       day-before forecast calibrated on the days before yesterday.
    """
    tz, now = inputs.tz, inputs.now.astimezone(inputs.tz)
    today = now.date()
    yesterday = today - dt.timedelta(days=1)
    tomorrow = today + dt.timedelta(days=1)
    midnight = at_local(today, dt.time(0), tz)
    end = at_local(today + dt.timedelta(days=FORECAST_DAYS), dt.time(0), tz)
    timeline = Timeline.between(midnight, end, tz)
    quarters = list(timeline.locals)
    yesterday_start = at_local(yesterday, dt.time(0), tz)
    past = Timeline.between(yesterday_start, midnight, tz)
    yesterday_quarters = list(past.locals)

    # 1. The house load without the car and the heat pump
    without_ev = {
        hour: max(kw - max(inputs.ev.get(hour, 0.0), 0.0), 0.0)
        for hour, kw in inputs.load.items()
    }
    curve = inputs.cop_curve
    loss, loss_hours = heat_pump.heat_loss(
        heat_pump_excess(
            _local_hours(without_ev, tz), _local_hours(inputs.heating_shares, tz)
        ),
        _local_hours(inputs.indoor, tz),
        _local_hours(inputs.outdoor, tz),
        curve,
    )
    house = {
        hour: max(
            kw
            - inputs.heating_shares.get(hour, 0.0)
            * heat_pump.power_kw(
                inputs.heat_pump_power_kw,
                loss,
                inputs.indoor.get(hour),
                inputs.outdoor.get(hour),
                curve,
            ),
            0.0,
        )
        for hour, kw in without_ev.items()
    }
    # One-off large loads are added back from the calendar or covered by the reserve
    history = cap_large_loads(_local_hours(house, tz))

    # 2. Load per quarter
    load = load_quarters(history, quarters, today)
    yesterday_load = load_quarters(history, yesterday_quarters, yesterday)
    # Nobody home: no reserve for unplanned load (here and the battery's reserve SOC)
    away = away_quarters(inputs.away_periods, quarters)
    reserve = reserve_quarters(
        quarters,
        inputs.weekend_reserve_kwh,
        inputs.reserve_start,
        inputs.reserve_end,
        away,
    )
    planned_energy = [
        (start, stop, kwh) for start, stop, kwh, _ in inputs.planned_loads
    ]
    planned = planned_quarters(planned_energy, quarters)
    yesterday_planned = planned_quarters(planned_energy, yesterday_quarters)

    # 3. PV: Open-Meteo, or Forecast.Solar if that is missing
    since = now - dt.timedelta(days=CALIBRATION_DAYS)
    pv_actual = {hour: kw for hour, kw in inputs.pv.items() if 0 <= kw <= PV_MAX_KW}
    actual = {hour: kw for hour, kw in pv_actual.items() if hour >= since}
    yesterday_pv = None
    pv = None
    calibration = None
    raw = None
    pv_source = None
    temperature = None
    temperatures: Mapping = {}
    hourly = None

    def hour_factors(produced, predicted):
        """Return the factor per local hour with the hourly calibration, else None."""
        if inputs.pv_calibration != "hourly":
            return None
        return pv_hourly_calibration(
            _local_hours(produced, tz), _local_hours(predicted, tz)
        )

    if inputs.open_meteo is not None:
        latest, previous, temperatures = inputs.open_meteo
        recent = {hour: kw for hour, kw in previous.items() if hour >= since}
        calibration = pv_calibration(actual, recent)
        hourly = hour_factors(actual, recent)
        raw = pv_quarters(hourly_power_to_watts(latest), quarters)
        pv_source = "open-meteo"
        temperature = temperature_quarters(temperatures, quarters)
        # Yesterday: the day-before forecast, calibrated on the days before it
        window = (
            yesterday_start - dt.timedelta(days=CALIBRATION_DAYS),
            yesterday_start,
        )
        produced = {h: kw for h, kw in pv_actual.items() if window[0] <= h < window[1]}
        predicted = {h: kw for h, kw in previous.items() if window[0] <= h < window[1]}
        yesterday_pv = calibrated_quarters(
            pv_quarters(hourly_power_to_watts(previous), yesterday_quarters),
            yesterday_quarters,
            pv_calibration(produced, predicted),
            hour_factors(produced, predicted),
        )
    if raw is None and inputs.forecast_solar:
        predicted = {}
        for _, estimates in inputs.forecast_solar:
            for hour, kw in estimates.items():
                if hour >= since:
                    predicted[hour] = predicted.get(hour, 0.0) + kw
        calibration = pv_calibration(actual, predicted)
        hourly = hour_factors(actual, predicted)
        raw = [0.0] * len(quarters)
        for watts, _ in inputs.forecast_solar:
            plane = pv_quarters(watts, quarters)
            raw = [a + b for a, b in zip(raw, plane, strict=True)]
        pv_source = "forecast.solar"
    raw_totals = {}
    if raw is not None and calibration is not None:
        raw_totals = _daily_totals(timeline, raw, now)
        pv = calibrated_quarters(raw, quarters, calibration, hourly)
    if pv is not None and pv_source == "forecast.solar":
        # Forecast.Solar may not reach the last day: no PV there (not 0), so the
        # battery plan ends where the forecast does
        covered = min(
            (
                at_local(
                    max(watts).astimezone(tz).date() + dt.timedelta(days=1),
                    dt.time(0),
                    tz,
                )
                for watts, _ in inputs.forecast_solar
                if watts
            ),
            default=midnight,
        )
        pv = [
            value if timeline.starts[i] < covered else None
            for i, value in enumerate(pv)
        ]

    # Houses heated with electricity: the profile corrected for the outdoor
    # temperature, measured for the history days, Open-Meteo's for the forecast days
    slope = None
    if inputs.load_model == "temperature" and temperatures:
        measured = daily_means(_local_hours(inputs.outdoor, tz))
        expected = daily_means(_local_hours(temperatures, tz))
        slope = heating_slope(history, measured, today)
        load = temperature_load_quarters(history, quarters, today, measured, expected)
        yesterday_load = temperature_load_quarters(
            history, yesterday_quarters, yesterday, measured, expected
        )
    yesterday_expected = [
        None if base is None else base + extra
        for base, extra in zip(yesterday_load, yesterday_planned, strict=True)
    ]

    # 4. Totals for the sensors
    base_totals = _daily_totals(timeline, load, now)
    planned_totals = _daily_totals(timeline, planned, now)
    pv_totals = _daily_totals(timeline, pv, now) if pv is not None else {}

    def total(day):
        """Return the expected load for a day: profile plus planned loads."""
        if day not in base_totals:
            return None
        return round(base_totals[day] + planned_totals.get(day, 0.0), 2)

    def two_decimals(value):
        return None if value is None else round(value, 2)

    details = {
        "heat_pump_loss_hours": loss_hours,
        "load_model": inputs.load_model,
        # Temperature model: daily load change per °C warmer (kWh/°C), 0 = none
        "heating_slope": None if slope is None else round(slope, 2),
        "planned_events": [
            {
                "summary": summary,
                "start": start.isoformat(),
                "end": stop.isoformat(),
                "kwh": kwh,
            }
            for start, stop, kwh, summary in inputs.planned_loads
            if stop > quarters[0]
        ],
        "load_today_remaining": total(today),
        "load_tomorrow": total(tomorrow),
        "base_tomorrow": two_decimals(base_totals.get(tomorrow)),
        "planned_tomorrow": round(planned_totals.get(tomorrow, 0.0), 2),
        "pv_today_remaining": round(pv_totals.get(today, 0.0), 2)
        if pv is not None
        else None,
        "pv_tomorrow": two_decimals(pv_totals.get(tomorrow)),
        "pv_tomorrow_uncalibrated": two_decimals(raw_totals.get(tomorrow)),
        "pv_calibration": None if calibration is None else round(calibration, 3),
        "pv_calibration_model": inputs.pv_calibration,
        # Hourly calibration: the factor per local hour of the day (others: daily)
        "pv_hourly_calibration": None
        if hourly is None
        else {hour: round(factor, 3) for hour, factor in sorted(hourly.items())},
        "pv_source": pv_source,
        "yesterday_load_kwh": _total(yesterday_expected),
        "yesterday_load_actual_kwh": _day_total(house, yesterday_start, midnight),
        "yesterday_pv_kwh": _total(yesterday_pv),
        "yesterday_pv_actual_kwh": _day_total(pv_actual, yesterday_start, midnight),
    }
    return Forecast(
        updated=inputs.now,
        timeline=timeline,
        load=_rounded(load) or (),
        planned=tuple(round(v, 3) for v in planned),
        reserve=tuple(round(v, 3) for v in reserve),
        away=tuple(away),
        pv=_rounded(pv),
        temperature=_rounded(temperature),
        heat_pump_loss=None if loss is None else round(loss, 3),
        ev_need_kwh=two_decimals(
            ev_need.need_from_history(_local_hours(inputs.ev, tz), today)
        ),
        yesterday=Forecast(
            updated=inputs.now,
            timeline=past,
            load=_rounded(yesterday_load) or (),
            planned=tuple(round(v, 3) for v in yesterday_planned),
            pv=_rounded(yesterday_pv),
        ),
        details=details,
    )
