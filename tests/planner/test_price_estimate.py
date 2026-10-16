"""Tests for the estimate of tomorrow's prices."""

import datetime as dt
import math

import pytest

from custom_components.energy_planner.planner import price_estimate

# Weekdays 100, weekends 50; no holidays from February to late March 2026
DAYS = [dt.date(2026, 2, 1) + dt.timedelta(days=i) for i in range(53)]


def history(days=DAYS):
    """Return flat prices per day: 100 on workdays, 50 on weekends."""
    return {
        day: dict.fromkeys(range(24), 50.0 if day.weekday() >= 5 else 100.0)
        for day in days
    }


@pytest.mark.parametrize(
    ("year", "sunday"),
    [
        (2024, dt.date(2024, 3, 31)),
        (2025, dt.date(2025, 4, 20)),
        (2026, dt.date(2026, 4, 5)),
    ],
)
def test_easter(year, sunday):
    """Test Easter Sunday."""
    assert price_estimate.easter(year) == sunday


def test_offdays():
    """Test weekends, holidays and the eves that price like holidays."""
    holidays = price_estimate.swedish_holidays(2026)
    assert dt.date(2026, 4, 3) in holidays  # Good Friday
    assert dt.date(2026, 5, 14) in holidays  # Ascension Day
    assert dt.date(2026, 6, 19) in holidays  # Midsummer Eve
    assert dt.date(2026, 12, 24) in holidays
    assert price_estimate.is_offday(dt.date(2026, 10, 10))  # Saturday
    assert not price_estimate.is_offday(dt.date(2026, 10, 9))


def test_workday_after_workday():
    """Test that a workday is estimated from today and the last workdays."""
    estimate = price_estimate.estimate(history(), dt.date(2026, 3, 25))  # Wednesday
    assert estimate == pytest.approx(dict.fromkeys(range(24), 100.0))


def test_weekend_after_workday():
    """Test the same-type days and the learned correction for Friday to Saturday."""
    prices = history()
    saturday = dt.date(2026, 3, 21)
    assert saturday.weekday() == 5
    # Earlier Saturdays: (100 Friday + 50 weekends) / 2 = 75 uncorrected, 25 too high
    samples = sum(
        1
        for day in prices
        if day < saturday
        and day.weekday() == 5
        and day - dt.timedelta(days=1) in prices
    )
    correction = -25 * samples / (samples + price_estimate.CORRECTION_SHRINK)
    estimate = price_estimate.estimate(prices, saturday)
    assert estimate is not None
    assert estimate == pytest.approx(dict.fromkeys(range(24), 75 + correction))
    assert estimate[12] < 75


def test_holiday_is_an_offday():
    """Test that Good Friday is estimated from weekends, not workdays."""
    days = [dt.date(2026, 3, 1) + dt.timedelta(days=i) for i in range(33)]
    estimate = price_estimate.estimate(history(days), dt.date(2026, 4, 3))
    # Thursday 100, weekends 50, before any correction
    assert estimate is not None
    assert estimate[12] <= 75


def test_no_estimate_without_today():
    """Test that the day before must be known."""
    assert price_estimate.estimate(history(), dt.date(2026, 4, 1)) is None
    assert price_estimate.estimate({}, dt.date(2026, 4, 1)) is None


def test_gap_to_tonight_fades():
    """Test that the estimate starts near tonight's last price and fades to its own."""
    wednesday = dt.date(2026, 3, 25)
    estimate = price_estimate.estimate(history(), wednesday, last=20.0)
    assert estimate is not None
    tau = price_estimate.GAP_TIME_CONSTANT
    for hour in (0, 6, 23):
        expected = 100 - 80 * math.exp(-(hour + 0.5) / tau)
        assert estimate[hour] == pytest.approx(expected)
    assert estimate[0] < 30
    assert estimate[0] < estimate[6] < estimate[23] < 100


def windy(days, speed):
    """Return wind for all sites: `speed(day)` km/h in every hour."""
    return {
        site: {day: dict.fromkeys(range(24), speed(day)) for day in days}
        for site in price_estimate.WIND_SITES
    }


def test_wind_power():
    """Test the turbine curve: nothing below 3 m/s, full from 12 m/s."""
    assert price_estimate.wind_power(3 * 3.6) == 0.0
    assert price_estimate.wind_power(7.5 * 3.6) == pytest.approx(0.125)
    assert price_estimate.wind_power(20 * 3.6) == 1.0


def test_wind_features():
    """Test the change in wind power per group and in the hour."""
    days = [dt.date(2026, 3, 1), dt.date(2026, 3, 2)]
    wind = windy(days, lambda day: 12 * 3.6 if day == days[1] else 0.0)
    features = price_estimate.wind_features(wind, days[1])
    assert features is not None
    assert features[5] == pytest.approx((1.0, 1.0, 1.0))
    assert price_estimate.wind_features(wind, days[0]) is None  # no day before


def test_wind_lowers_the_estimate():
    """Test that a learned wind effect is applied to a windy tomorrow."""
    days = [dt.date(2025, 6, 1) + dt.timedelta(days=i) for i in range(160)]
    # Every third day is windy, and windy days are 40 cheaper than the profile
    is_windy = {day: i % 3 == 0 for i, day in enumerate(days)}
    prices = {day: dict.fromkeys(range(24), 100.0 - 40 * is_windy[day]) for day in days}
    speeds = windy(days, lambda day: 12 * 3.6 if is_windy[day] else 0.0)
    tomorrow = days[-1]
    assert is_windy[tomorrow]
    del prices[tomorrow]
    last = prices[tomorrow - dt.timedelta(days=1)][23]
    plain = price_estimate.estimate(prices, tomorrow, last)
    with_wind = price_estimate.estimate(prices, tomorrow, last, speeds)
    assert plain is not None
    assert with_wind is not None
    # Most of the 40 (the base and the shrunk corrections blur it a little)
    assert with_wind[12] == pytest.approx(60.0, abs=6.0)
    assert with_wind[12] < plain[12] - 10


def test_no_wind_correction_with_little_history():
    """Test that without enough wind history the estimate is unchanged."""
    days = DAYS
    prices = history(days)
    wednesday = dt.date(2026, 3, 25)
    speeds = windy(days[-10:], lambda day: 30.0)
    assert price_estimate.estimate(prices, wednesday, 100.0, speeds) == (
        price_estimate.estimate(prices, wednesday, 100.0)
    )
