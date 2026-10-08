"""Tests for the pure forecast functions."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.planner import forecast

TZ = ZoneInfo("Europe/Stockholm")


def history_for(days, value_for):
    """Build {(date, hour): kWh} for the given dates."""
    return {(day, hour): value_for(day, hour) for day in days for hour in range(24)}


class TestQuarterStarts:
    """Tests for quarter_starts."""

    def test_starts_at_current_quarter(self):
        """Test that the quarter containing start is included."""
        start = dt.datetime(2026, 10, 4, 10, 7, 30, tzinfo=TZ)
        end = dt.datetime(2026, 10, 4, 11, 0, tzinfo=TZ)
        quarters = forecast.quarter_starts(start, end)
        assert [q.strftime("%H:%M") for q in quarters] == [
            "10:00",
            "10:15",
            "10:30",
            "10:45",
        ]

    def test_dst_end_has_100_quarters(self):
        """Test the 25 hour day when daylight saving ends."""
        start = dt.datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
        end = dt.datetime(2026, 10, 26, 0, 0, tzinfo=TZ)
        assert len(forecast.quarter_starts(start, end)) == 100

    def test_dst_start_has_92_quarters(self):
        """Test the 23 hour day when daylight saving starts."""
        start = dt.datetime(2026, 3, 29, 0, 0, tzinfo=TZ)
        end = dt.datetime(2026, 3, 30, 0, 0, tzinfo=TZ)
        assert len(forecast.quarter_starts(start, end)) == 92


class TestLoadForecast:
    """Tests for the load forecast."""

    today = dt.date(2026, 10, 7)  # Wednesday

    def test_blends_recent_and_same_type_profiles(self):
        """Test the blend of the 7-day and same-type profiles."""
        days = [self.today - dt.timedelta(days=i) for i in range(1, 36)]
        # Workdays use 1 kWh per hour, weekends 3 kWh per hour
        history = history_for(days, lambda d, h: 3.0 if forecast.is_offday(d) else 1.0)
        workday = forecast.hourly_load_forecast(history, self.today, 18, self.today)
        saturday = forecast.hourly_load_forecast(
            history, dt.date(2026, 10, 10), 18, self.today
        )
        # Recent 7 days: 5 workdays and 2 weekend days -> 11/7; same type: 1 or 3
        assert workday == pytest.approx((11 / 7 + 1) / 2)
        assert saturday == pytest.approx((11 / 7 + 3) / 2)

    def test_ignores_today(self):
        """Test that today's partial data is not used."""
        days = [self.today - dt.timedelta(days=i) for i in range(0, 10)]
        history = history_for(days, lambda d, h: 10.0 if d == self.today else 1.0)
        assert forecast.hourly_load_forecast(
            history, self.today, 12, self.today
        ) == pytest.approx(1.0)

    def test_falls_back_with_little_history(self):
        """Test the fallback when there is too little history."""
        history = {(self.today - dt.timedelta(days=1), 8): 2.0}
        assert forecast.hourly_load_forecast(history, self.today, 8, self.today) == 2.0
        assert forecast.hourly_load_forecast({}, self.today, 8, self.today) is None

    def test_quarters_split_hour_evenly(self):
        """Test that the hourly forecast is split evenly."""
        days = [self.today - dt.timedelta(days=i) for i in range(1, 15)]
        history = history_for(days, lambda d, h: float(h))
        quarters = forecast.quarter_starts(
            dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ),
            dt.datetime(2026, 10, 7, 10, 0, tzinfo=TZ),
        )
        assert forecast.load_quarters(history, quarters, self.today) == [2.25] * 4


class TestPv:
    """Tests for the PV forecast."""

    def test_integrates_linear_power(self):
        """Test integration of a linearly rising power forecast."""
        day = dt.datetime(2026, 7, 1, tzinfo=TZ)
        # 0 W at 10:00 rising to 4000 W at 11:00
        watts = {day.replace(hour=10): 0, day.replace(hour=11): 4000}
        quarters = forecast.quarter_starts(day.replace(hour=10), day.replace(hour=11))
        result = forecast.pv_quarters(watts, quarters)
        assert result == pytest.approx([0.125, 0.375, 0.625, 0.875])
        assert sum(result) == pytest.approx(2.0)  # 2 kW average for an hour

    def test_zero_outside_forecast(self):
        """Test that there is no production outside the forecast."""
        day = dt.datetime(2026, 7, 1, tzinfo=TZ)
        watts = {day.replace(hour=10): 1000, day.replace(hour=11): 1000}
        quarters = forecast.quarter_starts(day.replace(hour=6), day.replace(hour=7))
        assert forecast.pv_quarters(watts, quarters) == [0.0] * 4

    def test_calibration_ratio(self):
        """Test the actual / forecast calibration ratio."""
        hours = [dt.datetime(2026, 7, 1, h, tzinfo=dt.UTC) for h in range(6, 18)]
        actual = dict.fromkeys(hours, 3.0)
        predicted = dict.fromkeys(hours, 2.0)
        assert forecast.pv_calibration(actual, predicted) == pytest.approx(1.5)

    def test_calibration_needs_enough_data_and_is_limited(self):
        """Test the minimum data and the limits of the ratio."""
        hour = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
        assert forecast.pv_calibration({hour: 3.0}, {hour: 1.0}) == 1.0
        hours = [dt.datetime(2026, 7, 1, h, tzinfo=dt.UTC) for h in range(6, 18)]
        assert (
            forecast.pv_calibration(
                dict.fromkeys(hours, 10.0), dict.fromkeys(hours, 1.0)
            )
            == forecast.CALIBRATION_LIMITS[1]
        )


def test_reserve_only_weekend_window():
    """Test that the reserve only covers weekend evenings (default 18-22)."""
    friday = dt.datetime(2026, 10, 9, 0, 0, tzinfo=TZ)
    quarters = forecast.quarter_starts(friday, friday + dt.timedelta(days=2))
    reserve = forecast.reserve_quarters(quarters, 4.0)
    by_day = {}
    for start, value in zip(quarters, reserve, strict=True):
        by_day[start.date()] = by_day.get(start.date(), 0) + value
        if value:
            assert 18 <= start.hour < 22
    assert by_day[dt.date(2026, 10, 9)] == 0
    assert by_day[dt.date(2026, 10, 10)] == pytest.approx(4.0)


def test_reserve_custom_and_empty_window():
    """Test a custom reserve window and that an empty window gives no reserve."""
    saturday = dt.datetime(2026, 10, 10, 0, 0, tzinfo=TZ)
    quarters = forecast.quarter_starts(saturday, saturday + dt.timedelta(days=1))
    reserve = forecast.reserve_quarters(quarters, 2.0, 19, 21)
    assert sum(reserve) == pytest.approx(2.0)
    assert {q.hour for q, v in zip(quarters, reserve, strict=True) if v} == {19, 20}
    assert sum(forecast.reserve_quarters(quarters, 2.0, 20, 20)) == 0


def test_cap_large_loads():
    """Test that only hours far above the usual level are capped."""
    today = dt.date(2026, 10, 4)
    days = [today - dt.timedelta(days=i) for i in range(0, 15)]
    history = history_for(days, lambda d, h: 1.0)
    history[(today, 19)] = 8.0  # sauna
    history[(today, 18)] = 3.0  # cooking, below the margin
    capped = forecast.cap_large_loads(history)
    assert capped[(today, 19)] == pytest.approx(1.0 + forecast.LARGE_LOAD_MARGIN_KWH)
    assert capped[(today, 18)] == 3.0
    # Too little history to compare with: unchanged
    assert forecast.cap_large_loads({(today, 19): 8.0}) == {(today, 19): 8.0}


@pytest.mark.parametrize(
    ("summary", "description", "expected"),
    [
        ("Bastu", "", 5.0),
        ("bastu 6,5 kWh", "", 6.5),
        ("Tvättmaskin", "", 1.5),
        ("Tvätt och tork", "", 4.0),
        ("Torktumlare", "", 2.5),
        ("Ladda elbilen", "", 20.0),
        ("Ladda bilen", "Behöver 32 kWh", 32.0),
        ("EV", "", 20.0),
        ("Kvällsfika", "", None),
        ("Tandläkare", "", None),
    ],
)
def test_planned_event_energy(summary, description, expected):
    """Test energy from the text of calendar events."""
    assert forecast.planned_event_energy(summary, description) == expected


def test_planned_quarters_spreads_by_overlap():
    """Test that an event is spread by how much of each quarter it covers."""
    start = dt.datetime(2026, 10, 4, 19, 0, tzinfo=TZ)
    quarters = forecast.quarter_starts(start, start + dt.timedelta(hours=1))
    # 4 kWh from 19:10 to 19:50: 40 minutes, so 0.1 kWh per minute
    event = (start + dt.timedelta(minutes=10), start + dt.timedelta(minutes=50), 4.0)
    result = forecast.planned_quarters([event], quarters)
    assert result == pytest.approx([0.5, 1.5, 1.5, 0.5])
    # Parts outside the forecast range are dropped
    late = (start + dt.timedelta(minutes=30), start + dt.timedelta(minutes=90), 6.0)
    assert sum(forecast.planned_quarters([late], quarters)) == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("compass", "expected"),
    [(75, -105), (256, 76), (345, 165), (180, 0), (0, -180)],
)
def test_open_meteo_azimuth(compass, expected):
    """Test compass (0 = north) to Open-Meteo (0 = south) azimuth."""
    assert forecast.open_meteo_azimuth(compass) == expected


def test_plane_power():
    """Test peak power and the loss from hot cells."""
    assert forecast.plane_power(1000, 6) == pytest.approx(6.0)
    # Cells at 25 C: no loss
    assert forecast.plane_power(1000, 6, temperature=0) == pytest.approx(6.0)
    # Cells at 45 C: 8% loss
    assert forecast.plane_power(1000, 6, temperature=20) == pytest.approx(5.52)
    assert forecast.plane_power(0, 6, temperature=20) == 0


def test_open_meteo_plane_power():
    """Test the hour shift, the model average and the day-before forecast."""
    hourly = {
        "time": ["2026-07-01T11:00", "2026-07-01T12:00"],
        "global_tilted_irradiance_metno_seamless": [400.0, None],
        "global_tilted_irradiance_ecmwf_ifs025": [600.0, 800.0],
        "global_tilted_irradiance_previous_day1_ecmwf_ifs025": [200.0, None],
    }
    latest, previous = forecast.open_meteo_plane_power(hourly, 10)
    ten = dt.datetime(2026, 7, 1, 10, tzinfo=dt.UTC)
    # 11:00 is the mean of 10:00-11:00; missing values are skipped
    assert latest == {
        ten: pytest.approx(5.0),
        ten + dt.timedelta(hours=1): pytest.approx(8.0),
    }
    assert previous == {ten: pytest.approx(2.0)}


def test_hourly_power_to_watts_keeps_energy():
    """Test that hourly means placed mid-hour integrate to the same energy."""
    ten = dt.datetime(2026, 7, 1, 10, tzinfo=dt.UTC)
    hourly = {ten + dt.timedelta(hours=i): kw for i, kw in enumerate([0, 4, 4, 0])}
    watts = forecast.hourly_power_to_watts(hourly)
    assert watts[ten + dt.timedelta(minutes=90)] == 4000
    quarters = forecast.quarter_starts(ten, ten + dt.timedelta(hours=4))
    assert sum(forecast.pv_quarters(watts, quarters)) == pytest.approx(8.0)


def test_heating_shares():
    """Test the share of each UTC hour in heat mode, across hour boundaries."""
    utc = dt.UTC
    changes = [
        (dt.datetime(2026, 10, 7, 22, 30, tzinfo=utc), True),
        (dt.datetime(2026, 10, 8, 0, 15, tzinfo=utc), False),
        (dt.datetime(2026, 10, 8, 1, 45, tzinfo=utc), True),
    ]
    shares = forecast.heating_shares(changes, dt.datetime(2026, 10, 8, 2, tzinfo=utc))
    assert shares == {
        dt.datetime(2026, 10, 7, 22, tzinfo=utc): 0.5,
        dt.datetime(2026, 10, 7, 23, tzinfo=utc): 1.0,
        dt.datetime(2026, 10, 8, 0, tzinfo=utc): 0.25,
        dt.datetime(2026, 10, 8, 1, tzinfo=utc): 0.25,
    }


def test_heat_pump_excess():
    """Test the load above the same hour on days without heating."""
    days = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(10)]
    history = {(day, hour): 0.8 for day in days for hour in range(24)}
    shares = {}
    for hour in range(3):
        shares[(days[-1], hour)] = 1.0
        history[(days[-1], hour)] = 0.8 + (0.6 if hour else 7.0)  # a sauna hour
    # A quarter of an hour at 0.15 kW more scales to 0.6 kW
    shares[(days[-1], 4)] = 0.75
    history[(days[-1], 4)] = 0.8 + 0.45
    # Half an hour of heating does not measure
    shares[(days[-1], 5)] = 0.5
    excess = forecast.heat_pump_excess(history, shares)
    assert excess == pytest.approx(
        {(days[-1], 0): 2.5, (days[-1], 1): 0.6, (days[-1], 2): 0.6, (days[-1], 4): 0.6}
    )


def test_heat_pump_excess_needs_references():
    """Test that an hour without enough days without heating is not measured."""
    days = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(3)]
    history = {(day, 0): 0.8 for day in days}
    assert forecast.heat_pump_excess(history, {(days[-1], 0): 1.0}) == {}
