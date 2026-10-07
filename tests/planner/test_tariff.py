"""Tests for the grid tariff model."""

import datetime as dt
import importlib
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.const import DOMAIN

tariff = importlib.import_module("custom_components.energy_planner.planner.tariff")
utils = importlib.import_module("custom_components.energy_planner.planner.utils")

TZ = ZoneInfo("Europe/Stockholm")
TEKNISKA_VERKEN = tariff.tariff_from_dict(tariff.TEKNISKA_VERKEN_ALTERNATIV)


def at(month, day, hour, year=2026):
    """Return a local time."""
    return dt.datetime(year, month, day, hour, tzinfo=TZ)


class TestPeriod:
    """Tests for when rules apply."""

    def test_hours_wrap_midnight(self):
        """Test a night window from 23 to 06."""
        night = tariff.Period(start_hour=23, end_hour=6)
        assert [h for h in range(24) if night.contains(at(1, 5, h))] == [
            0, 1, 2, 3, 4, 5, 23,
        ]  # fmt: skip

    def test_months_and_weekdays(self):
        """Test winter weekdays; 2026-01-05 is a Monday, 01-10 a Saturday."""
        period = tariff.Period(
            months=frozenset({1}), weekdays=frozenset({1, 2, 3, 4, 5})
        )
        assert period.contains(at(1, 5, 12))
        assert not period.contains(at(1, 10, 12))
        assert not period.contains(at(2, 2, 12))

    def test_whole_day(self):
        """Test that equal start and end mean the whole day."""
        assert tariff.in_hours(3, 0, 24)
        assert tariff.in_hours(3, 6, 6)


class TestTekniskaVerken:
    """Tests for the Tekniska verken preset."""

    def test_buy_fee_day_and_night(self):
        """Test transfer fee plus energy tax."""
        assert TEKNISKA_VERKEN.buy_fee(at(1, 5, 12)) == pytest.approx(63.0)
        assert TEKNISKA_VERKEN.buy_fee(at(1, 5, 23)) == pytest.approx(54.0)
        assert TEKNISKA_VERKEN.buy_fee(at(1, 5, 5)) == pytest.approx(54.0)
        assert TEKNISKA_VERKEN.buy_fee(at(1, 5, 6)) == pytest.approx(63.0)

    def test_sell_fee_high_load_period(self):
        """Test 7.20 öre on winter weekdays 06-22, 5.10 otherwise."""
        assert TEKNISKA_VERKEN.sell_fee(at(1, 5, 12)) == pytest.approx(7.2)
        assert TEKNISKA_VERKEN.sell_fee(at(1, 5, 22)) == pytest.approx(5.1)
        assert TEKNISKA_VERKEN.sell_fee(at(1, 10, 12)) == pytest.approx(5.1)
        assert TEKNISKA_VERKEN.sell_fee(at(6, 1, 12)) == pytest.approx(5.1)

    def test_power_cost_day_and_night_peaks(self):
        """Test one day and one night peak per month, winter prices."""
        hourly = {at(1, 5, 18): 8.0, at(1, 6, 12): 2.0, at(1, 7, 2): 5.0}
        assert TEKNISKA_VERKEN.power_cost(hourly) == pytest.approx(8 * 45 + 5 * 12)

    def test_power_cost_per_month(self):
        """Test that each month is billed on its own peaks."""
        hourly = {at(3, 31, 12): 4.0, at(4, 1, 12): 4.0}
        assert TEKNISKA_VERKEN.power_cost(hourly) == pytest.approx(4 * 45 + 4 * 23)


PEAKS = {at(1, 5, 8): 6.0, at(1, 5, 9): 5.0, at(1, 6, 8): 3.0, at(1, 7, 8): 1.0}


class TestChargedPeak:
    """Tests for peaks averaged over several hours or days."""

    def test_mean_of_highest_hours(self):
        """Test the mean of the three highest hours."""
        charge = tariff.PowerCharge(10.0, peaks=3)
        assert tariff.charged_peak(charge, PEAKS) == pytest.approx(14 / 3)

    def test_one_peak_per_day(self):
        """Test that two peaks the same day count once."""
        charge = tariff.PowerCharge(10.0, peaks=3, one_per_day=True)
        assert tariff.charged_peak(charge, PEAKS) == pytest.approx(10 / 3)

    def test_missing_peaks_count_as_zero(self):
        """Test early in the month, with fewer days than peaks."""
        charge = tariff.PowerCharge(10.0, peaks=3, one_per_day=True)
        assert tariff.charged_peak(charge, {at(1, 1, 8): 3.0}) == pytest.approx(1.0)

    def test_outside_the_period_is_free(self):
        """Test that hours outside the charge's period are ignored."""
        charge = tariff.PowerCharge(10.0, tariff.Period(start_hour=7, end_hour=20))
        assert tariff.charged_peak(charge, {at(1, 5, 20): 9.0}) == 0.0


class TestParsing:
    """Tests for the tariff dict."""

    def test_empty(self):
        """Test that an empty tariff costs nothing."""
        empty = tariff.tariff_from_dict({})
        assert empty.buy_fee(at(1, 5, 12)) == 0.0
        assert empty.power_cost({at(1, 5, 12): 5.0}) == 0.0

    @pytest.mark.parametrize(
        "data",
        [
            {"energy_fees": [{"hours": [6, 23]}]},
            {"energy_fees": [{"ore_per_kwh": 1, "months": [13]}]},
            {"energy_fees": [{"ore_per_kwh": 1, "weekdays": [0]}]},
            {"energy_fees": [{"ore_per_kwh": 1, "hours": [6, 25]}]},
            {"energy_fees": [{"ore_per_kwh": 1, "hours": 6}]},
            {"power_charges": [{"kr_per_kw": 1, "peaks": 0}]},
            {"power_charges": [{"kr_per_kw": "x"}]},
        ],
    )
    def test_invalid(self, data):
        """Test that invalid rules raise ValueError."""
        with pytest.raises(ValueError, match=r"."):
            tariff.tariff_from_dict(data)


def test_eon():
    """Test the E.ON preset: flat fees, no power charge."""
    eon = tariff.tariff_from_dict(tariff.EON_20A)
    assert eon.buy_fee(at(1, 5, 12)) == pytest.approx(77.3)
    assert eon.sell_fee(at(1, 5, 12)) == pytest.approx(10.0)
    assert eon.power_cost({at(1, 5, 12): 9.0}) == 0.0


def test_import_limit():
    """Test the household's import limit window."""
    assert tariff.import_limit(at(1, 5, 12), 1.0, 6, 23) == 1.0
    assert tariff.import_limit(at(1, 5, 23), 1.0, 6, 23) is None
    assert tariff.import_limit(at(1, 5, 12), 0.0, 6, 23) is None


class TestGetTariff:
    """Tests for choosing the tariff from the options."""

    @staticmethod
    def hass(options):
        """Create a mock hass with options and flat network settings."""
        mock = MagicMock()
        mock.data = {
            DOMAIN: {
                "options": options,
                "config": {"network_cost": 60, "network_compensation": 5},
            }
        }
        return mock

    def test_flat_by_default(self):
        """Test the fallback to the network settings."""
        chosen = utils.get_tariff(self.hass({}))
        assert chosen.buy_fee(at(1, 5, 12)) == 60
        assert chosen.sell_fee(at(1, 5, 12)) == 5

    def test_preset(self):
        """Test a preset."""
        options = {"tariff_preset": "tekniska_verken_alternativ"}
        assert utils.get_tariff(self.hass(options)) == TEKNISKA_VERKEN

    def test_custom(self):
        """Test a custom tariff."""
        options = {
            "tariff_preset": "custom",
            "tariff": {"energy_fees": [{"ore_per_kwh": 7}]},
        }
        assert utils.get_tariff(self.hass(options)).buy_fee(at(1, 5, 12)) == 7

    def test_invalid_custom_falls_back(self):
        """Test that a broken custom tariff falls back to the flat settings."""
        options = {"tariff_preset": "custom", "tariff": {"energy_fees": [{}]}}
        assert utils.get_tariff(self.hass(options)).buy_fee(at(1, 5, 12)) == 60
