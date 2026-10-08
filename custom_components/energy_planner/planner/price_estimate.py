"""Estimate of tomorrow's spot prices before Nord Pool publishes them (about 13:00).

Pure functions, no Home Assistant imports. Until tomorrow's prices are known, the
battery plan would end at midnight and value the energy left at today's cheapest
price; with an estimate it can see whether keeping energy (or solar excess) for
tomorrow pays. The estimate is replaced as soon as the real prices arrive.

Per hour: the mean of today's price and the mean of the last 7 days of the same type
as tomorrow (workday, or weekend and holiday), plus a correction per hour and type
change (e.g. Friday -> Saturday) learned from the price history, shrunk toward zero
while it has few samples. Backtested on SE3, May 2025 - Oct 2026 (see AGENTS.md):
the mean hourly error was 23 öre/kWh against 25 without the day types; following the
plan made with it until the real prices came cost 0.10 kr/day more than knowing
them, against 0.31 kr/day for a plan that ends at midnight. Weather (wind,
temperature, sun in the Nordics and Germany) predicted the prices better but did not
improve the battery's decisions.
"""

from collections.abc import Mapping
import datetime as dt
import functools
from statistics import mean, median

SAME_TYPE_DAYS = 7
SAME_TYPE_LOOKBACK_DAYS = 21
MIN_HOURS = 20  # a day with fewer known hours is not used
# Samples at which a correction counts half
CORRECTION_SHRINK = 20

Prices = Mapping[dt.date, Mapping[int, float]]


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


def estimate(prices: Prices, day: dt.date) -> dict[int, float] | None:
    """Return the estimated price per local hour of `day`, None without history.

    `prices` are known prices per local date and hour, in any unit; the estimate is
    in the same unit. The day before `day` must be known.
    """
    base = _base(prices, day)
    if base is None:
        return None
    kind = _kind(day)
    residuals: dict[int, list[float]] = {}
    for past in prices:
        if past >= day or _kind(past) != kind or not _complete(prices, past):
            continue
        past_base = _base(prices, past)
        if past_base is None:
            continue
        for hour, price in prices[past].items():
            if hour in past_base:
                residuals.setdefault(hour, []).append(price - past_base[hour])
    result = {}
    for hour, price in base.items():
        samples = residuals.get(hour, [])
        correction = (
            median(samples) * len(samples) / (len(samples) + CORRECTION_SHRINK)
            if samples
            else 0.0
        )
        result[hour] = price + correction
    return result
