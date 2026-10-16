"""Estimate of tomorrow's spot prices before Nord Pool publishes them (about 13:00).

Pure functions, no Home Assistant imports. Until tomorrow's prices are known, the
battery plan would end at midnight and value the energy left at today's cheapest
price; with an estimate it can see whether keeping energy (or solar excess) for
tomorrow pays. The estimate is replaced as soon as the real prices arrive.

Per hour: the mean of today's price and the mean of the last 7 days of the same type
as tomorrow (workday, or weekend and holiday), plus a correction per hour and type
change (e.g. Friday -> Saturday) learned from the price history, shrunk toward zero
while it has few samples. Then the gap between today's last price and the estimate
for the first hour is added, fading with `GAP_TIME_CONSTANT`: the prices just after
midnight follow the evening's, not the profile's level. Backtested on SE3, May 2025
- Oct 2026 (see AGENTS.md): the mean hourly error was 23 öre/kWh against 25 without
the day types; following the plan made with it until the real prices came cost
0.10 kr/day more than knowing them, against 0.31 kr/day for a plan that ends at
midnight. Weather (wind, temperature, sun in the Nordics and Germany) predicted the
prices better but did not improve the battery's decisions. The gap (SE3, 426 days
May 2025 - Oct 2026, the sensor's last quarter per hour): the best factor per hour
was about 0.85 until 07:00, falling to 0 at 21:00; the fading gap gave a mean hourly
error of 25.4 öre/kWh against 30.1 without, 18 against 29 for 00-07, in both halves
of the period.

With the wind forecast (`wind`, Open-Meteo's day-before run for WIND_SITES) a
correction per hour is added: tomorrow's change in wind power against today, for the
Swedish sites, the Danish/German ones and the hour itself, times coefficients fitted
on the last year's estimates (least absolute error, so a few price spikes do not
steer it). Backtest (SE3, 426 days May 2025 - Oct 2026, refitted weekly on the days
before): 25.4 -> 23.4 öre/kWh, better in both halves (20.6 -> 19.4, 28.0 -> 25.6);
mostly in the morning and evening peaks, a typical day-to-day change in Swedish wind
moves them 5-10 öre. Temperature (Stockholm), sun (Germany), fitted blend weights, a
season term and gradient boosting gave nothing more.
"""

from collections.abc import Mapping
import datetime as dt
import functools
import math
from statistics import mean, median

import numpy as np

SAME_TYPE_DAYS = 7
SAME_TYPE_LOOKBACK_DAYS = 21
MIN_HOURS = 20  # a day with fewer known hours is not used
# Samples at which a correction counts half
CORRECTION_SHRINK = 20
# Hours over which the gap to today's last price fades by e
GAP_TIME_CONSTANT = 12

# Wind forecast sites (latitude, longitude, group): the day's change in wind power is
# taken separately for the Swedish and the Danish/German group; the hour's own change
# over all of them
WIND_SITES = {
    "se2": (63.0, 17.0, "se"),
    "se3": (58.5, 13.0, "se"),
    "se4": (55.9, 13.6, "se"),
    "dk": (56.2, 8.6, "de"),
    "de_north": (53.8, 8.5, "de"),
    "de_northeast": (53.7, 12.5, "de"),
    "no_south": (58.5, 7.5, None),
    "fi": (64.5, 25.0, None),
}
WIND_FIT_DAYS = 365
WIND_MIN_DAYS = 60  # fewer days with wind and prices: no wind correction
# The least absolute error fit (reweighted least squares) weights a residual by 1 /
# max(|residual|, this share of the mean absolute residual)
WIND_LAD_FLOOR = 0.2
WIND_LAD_ITERATIONS = 20
WIND_RIDGE = 1e-3

Prices = Mapping[dt.date, Mapping[int, float]]
# Wind speed at 100 m (km/h) per site, local date and hour
Wind = Mapping[str, Mapping[dt.date, Mapping[int, float]]]


def easter(year: int) -> dt.date:
    """Return Easter Sunday (Gregorian, the anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return dt.date(year, month, day + 1)


@functools.cache
def swedish_holidays(year: int) -> frozenset[dt.date]:
    """Return the Swedish public holidays on weekdays, and the eves that price alike.

    Holidays that always fall on a weekend (Easter Sunday, Whitsun, Midsummer Day,
    All Saints' Day) are left out.
    """
    sunday = easter(year)
    midsummer_eve = next(
        dt.date(year, 6, d) for d in range(19, 26) if dt.date(year, 6, d).weekday() == 4
    )
    return frozenset(
        {
            dt.date(year, 1, 1),
            dt.date(year, 1, 6),
            sunday - dt.timedelta(days=2),  # Good Friday
            sunday + dt.timedelta(days=1),  # Easter Monday
            dt.date(year, 5, 1),
            sunday + dt.timedelta(days=39),  # Ascension Day
            dt.date(year, 6, 6),
            midsummer_eve,
            dt.date(year, 12, 24),
            dt.date(year, 12, 25),
            dt.date(year, 12, 26),
            dt.date(year, 12, 31),
        }
    )


def is_offday(day: dt.date) -> bool:
    """Return whether a day is a weekend day or a Swedish holiday."""
    return day.weekday() >= 5 or day in swedish_holidays(day.year)


def _complete(prices: Prices, day: dt.date) -> bool:
    return len(prices.get(day, {})) >= MIN_HOURS


def _base(prices: Prices, day: dt.date) -> dict[int, float] | None:
    """Return the uncorrected estimate for `day` from the days before it."""
    previous = day - dt.timedelta(days=1)
    if not _complete(prices, previous):
        return None
    offday = is_offday(day)
    same = [
        prices[d]
        for d in (
            day - dt.timedelta(days=i) for i in range(1, SAME_TYPE_LOOKBACK_DAYS + 1)
        )
        if _complete(prices, d) and is_offday(d) == offday
    ][:SAME_TYPE_DAYS]
    base = {}
    for hour, price in prices[previous].items():
        values = [p[hour] for p in same if hour in p]
        base[hour] = (price + mean(values)) / 2 if values else price
    return base


def _kind(day: dt.date) -> tuple[bool, bool]:
    return is_offday(day - dt.timedelta(days=1)), is_offday(day)


def estimate(
    prices: Prices,
    day: dt.date,
    last: float | None = None,
    wind: Wind | None = None,
) -> dict[int, float] | None:
    """Return the estimated price per local hour of `day`, None without history.

    `prices` are known prices per local date and hour, in any unit; the estimate is
    in the same unit. The day before `day` must be known. `last` is the price just
    before `day` (the last quarter): the gap from it to the first hour's estimate is
    added to hour h with the weight exp(-(h + 0.5) / GAP_TIME_CONSTANT). With `wind`
    (forecasts for WIND_SITES, also for the past days the fit uses) the wind
    correction is added, see `wind_correction`.
    """
    result = _estimate(prices, day, last)
    if result is None or wind is None:
        return result
    correction = wind_correction(prices, wind, day)
    return {hour: price + correction.get(hour, 0.0) for hour, price in result.items()}


def _estimate(
    prices: Prices, day: dt.date, last: float | None
) -> dict[int, float] | None:
    """Return the estimate without the wind correction (only uses days before)."""
    return _estimates(prices, {day: last}).get(day)


def _estimates(
    prices: Prices, wanted: Mapping[dt.date, float | None]
) -> dict[dt.date, dict[int, float]]:
    """Return the estimate for each day in `wanted` (day -> its `last`), in one pass.

    Days are walked in order; each wanted day uses the errors of the base estimate
    on the earlier complete days of its kind, then the day's own errors are added.
    """
    residuals: dict[tuple[bool, bool], dict[int, list[float]]] = {}
    result = {}
    for day in sorted(set(prices) | set(wanted)):
        base = _base(prices, day)
        if base is None:
            continue
        samples = residuals.setdefault(_kind(day), {})
        if day in wanted:
            result[day] = _corrected(base, samples, wanted[day])
        if _complete(prices, day):
            for hour, price in prices[day].items():
                if hour in base:
                    samples.setdefault(hour, []).append(price - base[hour])
    return result


def _corrected(
    base: Mapping[int, float], residuals: Mapping[int, list[float]], last: float | None
) -> dict[int, float]:
    """Return the base plus the shrunk median error per hour and the fading gap."""
    result = {}
    for hour, price in base.items():
        samples = residuals.get(hour, [])
        correction = (
            median(samples) * len(samples) / (len(samples) + CORRECTION_SHRINK)
            if samples
            else 0.0
        )
        result[hour] = price + correction
    if last is not None and result:
        gap = last - result[min(result)]
        for hour in result:
            result[hour] += gap * math.exp(-(hour + 0.5) / GAP_TIME_CONSTANT)
    return result


def wind_power(speed_kmh: float) -> float:
    """Return a rough turbine output share for a wind speed at 100 m (km/h).

    0 below 3 m/s, full from 12 m/s, cubic in between.
    """
    return min(max((speed_kmh / 3.6 - 3) / 9, 0.0), 1.0) ** 3


def _daily_power(wind: Wind, sites: list[str], day: dt.date) -> float | None:
    means = []
    for site in sites:
        hours = wind.get(site, {}).get(day, {})
        if len(hours) < MIN_HOURS:
            return None
        means.append(mean(wind_power(v) for v in hours.values()))
    return mean(means)


def wind_features(wind: Wind, day: dt.date) -> dict[int, tuple[float, ...]] | None:
    """Return per hour of `day` its change in wind power against the day before.

    (Swedish sites' daily mean, Danish/German sites' daily mean, all sites in that
    hour), as output shares (`wind_power`). None if a site lacks either day.
    """
    previous = day - dt.timedelta(days=1)
    changes = []
    for group in ("se", "de"):
        sites = [site for site, (_, _, g) in WIND_SITES.items() if g == group]
        now, before = (
            _daily_power(wind, sites, day),
            _daily_power(wind, sites, previous),
        )
        if now is None or before is None:
            return None
        changes.append(now - before)
    result = {}
    for hour in range(24):
        try:
            now = mean(wind_power(wind[site][day][hour]) for site in WIND_SITES)
            before = mean(wind_power(wind[site][previous][hour]) for site in WIND_SITES)
        except KeyError:
            continue
        result[hour] = (*changes, now - before)
    return result


def _lad(features: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Return least absolute error coefficients by reweighted least squares."""
    weights = np.ones(len(target))
    ridge = WIND_RIDGE * np.eye(features.shape[1])
    coefficients = np.zeros(features.shape[1])
    for _ in range(WIND_LAD_ITERATIONS):
        weighted = features * weights[:, None]
        coefficients = np.linalg.solve(
            features.T @ weighted + ridge, weighted.T @ target
        )
        residuals = np.abs(target - features @ coefficients)
        weights = 1 / np.maximum(residuals, WIND_LAD_FLOOR * residuals.mean() + 1e-9)
    return coefficients


def wind_correction(prices: Prices, wind: Wind, day: dt.date) -> dict[int, float]:
    """Return the wind correction per hour of `day`, empty if it can not be fitted.

    For each of the last WIND_FIT_DAYS days with prices, the day before and wind:
    the estimate's error (with the gap, from the days before it) per hour against
    that day's `wind_features`. Per hour, coefficients without an intercept are
    fitted by least absolute error (`_lad`); the correction is them times tomorrow's
    features.
    """
    features = wind_features(wind, day)
    if features is None:
        return {}
    lasts = {}
    for offset in range(1, WIND_FIT_DAYS + 1):
        past = day - dt.timedelta(days=offset)
        previous = prices.get(past - dt.timedelta(days=1), {})
        if _complete(prices, past) and 23 in previous:
            lasts[past] = previous[23]
    rows: dict[int, list[tuple[tuple[float, ...], float]]] = {}
    used = 0
    for past, past_estimate in _estimates(prices, lasts).items():
        past_features = wind_features(wind, past)
        if not past_features:
            continue
        used += 1
        for hour, price in prices[past].items():
            if hour in past_estimate and hour in past_features:
                rows.setdefault(hour, []).append(
                    (past_features[hour], price - past_estimate[hour])
                )
    if used < WIND_MIN_DAYS:
        return {}
    result = {}
    for hour, x in features.items():
        samples = rows.get(hour, [])
        if len(samples) < WIND_MIN_DAYS:
            continue
        coefficients = _lad(
            np.array([f for f, _ in samples]), np.array([e for _, e in samples])
        )
        result[hour] = float(np.array(x) @ coefficients)
    return result


def scale_to(history: Prices, known: Prices, min_hours: int) -> float | None:
    """Return the factor that turns `history` into the unit of `known`, or None.

    The median ratio over the hours both have (unit, VAT, currency and rounding
    differ between sources); None with fewer than `min_hours` such hours.
    """
    ratios = [
        price / history[day][hour]
        for day, hours in known.items()
        for hour, price in hours.items()
        if history.get(day, {}).get(hour)
    ]
    return median(ratios) if len(ratios) >= min_hours else None


def merge(*histories: Prices) -> dict[dt.date, dict[int, float]]:
    """Return per day the first history that has it complete, else the fullest."""
    result: dict[dt.date, dict[int, float]] = {}
    for day in {day for history in histories for day in history}:
        candidates = [dict(history[day]) for history in histories if day in history]
        complete = [hours for hours in candidates if len(hours) >= MIN_HOURS]
        result[day] = complete[0] if complete else max(candidates, key=len)
    return result
