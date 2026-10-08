"""Tests for the estimate of tomorrow's prices."""

import datetime as dt

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
