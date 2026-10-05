"""Tests for the price peak planner."""

import datetime as dt
import importlib
from itertools import pairwise
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.const import DOMAIN

# planner/__init__.py re-exports the planner function under the module's name
ppp = importlib.import_module(
    "custom_components.energy_planner.planner.price_peak_planner"
)

TZ = ZoneInfo("Europe/Stockholm")

# SE3 quarter prices for 2026-03-07 (SEK/MWh excl. VAT), from the planner's cache
PRICES_2026_03_07 = [
    783.87, 763.21, 742.55, 749.19, 756.68, 735.81, 733.56, 729.49, 715.9, 714.08,
    725.32, 737.09, 691.49, 724.03, 734.2, 741.69, 713.22, 730.67, 737.73, 759.25,
    737.95, 766.96, 776.91, 813.52, 797.57, 815.55, 822.62, 820.69, 837.39, 797.78,
    792.54, 678.65, 803.03, 770.59, 655.2, 555.12, 767.17, 678.97, 602.97, 535.21,
    647.92, 561.86, 516.69, 465.2, 553.09, 536.71, 512.41, 488.86, 532.11, 521.83,
    503.63, 495.18, 437.48, 494.53, 487.25, 479.98, 494.21, 515.94, 393.38, 379.78,
    337.4, 561.01, 633.26, 762.25, 588.73, 773.81, 880.31, 1095.25, 849.06, 1012.62,
    986.61, 1133.25, 1023.32, 1040.66, 1100.18, 1021.29, 1102.53, 1021.61, 1018.08,
    932.33, 1001.06, 957.81, 911.46, 859.01, 900.44, 871.11, 853.02, 807.1, 893.91,
    841.35, 811.48, 777.02, 892.84, 811.48, 781.3, 747.26,
]  # fmt: skip
DAY = dt.datetime(2026, 3, 7, tzinfo=TZ)


def nordpool_values(prices, start=DAY):
    """Build the quarter list plan_day receives."""
    quarter = dt.timedelta(minutes=15)
    return [
        {"start": start + i * quarter, "end": start + (i + 1) * quarter, "value": p}
        for i, p in enumerate(prices)
    ]


def more_than_20_percent(charge_price, discharge_price):
    """Profitability rule for the matching tests."""
    return discharge_price > charge_price * 1.2


class TestIsProfitable:
    """Tests for the cost model."""

    def test_losses_make_charging_more_expensive(self):
        """Test that 15% losses need a 1/0.85 higher price (was multiplied)."""
        assert not ppp.is_profitable(500, 520, 0.85, 0, 0, sells=False)
        assert not ppp.is_profitable(500, 580, 0.85, 0, 0, sells=False)
        assert ppp.is_profitable(500, 600, 0.85, 0, 0, sells=False)

    def test_fees_only_count_on_losses_when_used_at_home(self):
        """Test the break-even with 63 öre network cost and use at home."""
        # (400 * 1.25 + 630) / 0.85 - 630 = 699.4 SEK/MWh incl. VAT -> 559.5 excl.
        assert ppp.is_profitable(400, 570, 0.85, 63, 5, sells=False)
        assert not ppp.is_profitable(400, 550, 0.85, 63, 5, sells=False)

    def test_selling_pays_spot_plus_compensation(self):
        """Test the break-even when the discharged energy is sold."""
        # Cost (400 * 1.25 + 630) / 0.85 = 1329.4, value spot + 50
        assert ppp.is_profitable(400, 1290, 0.85, 63, 5, sells=True)
        assert not ppp.is_profitable(400, 1270, 0.85, 63, 5, sells=True)


class TestMatching:
    """Tests for pairing charge and discharge periods."""

    def test_pairs_cheap_and_expensive(self):
        """Test a profitable pair."""
        prices = [100, 100, 500, 500, 1000, 1000]
        assert ppp.match_charge_discharge_periods(
            prices, [[0, 1]], [[4, 5]], more_than_20_percent
        ) == [([0, 1], [4, 5])]

    def test_unprofitable_pair_is_dropped(self):
        """Test that a pair that does not pay off is not scheduled."""
        prices = [500, 500, 510, 510, 520, 520]
        assert (
            ppp.match_charge_discharge_periods(
                prices, [[0, 1]], [[4, 5]], more_than_20_percent
            )
            == []
        )

    def test_only_profitable_discharge_quarters(self):
        """Test that discharge quarters that do not pay off are left out."""
        prices = [100, 100, 500, 1000, 115, 1000]
        assert ppp.match_charge_discharge_periods(
            prices, [[0, 1]], [[3, 4, 5]], more_than_20_percent
        ) == [([0, 1], [3, 5])]

    def test_discharge_before_any_charge_is_ignored(self):
        """Test that a discharge period with nothing charged before it is skipped."""
        prices = [1000, 1000, 100, 100, 1000, 1000]
        assert ppp.match_charge_discharge_periods(
            prices, [[2, 3]], [[0, 1], [4, 5]], more_than_20_percent
        ) == [([2, 3], [4, 5])]

    def test_back_to_back_charges_keep_the_best_from_quarter_0(self):
        """Test that a period in quarter 0 is compared too (it was skipped)."""
        prices = [100, 900, 150, 900, 900, 1000]
        assert ppp.match_charge_discharge_periods(
            prices, [[0], [2]], [[5]], more_than_20_percent
        ) == [([0], [5])]


def test_dropped_overlapping_period_leaves_no_marks():
    """Test that a dropped period does not block other periods (it did)."""
    charges, discharges = ppp.remove_overlaps(10, [[0, 1], [6, 7]], [[7, 8], [5, 6]])
    assert charges == [[0, 1]]
    assert discharges == [[7, 8], [5, 6]]


class TestFindPeriods:
    """Tests for finding cheap and expensive windows."""

    def test_peak_at_end_of_data(self):
        """Test a peak in the last window (raised IndexError)."""
        prices = [float(i) for i in range(24)]
        assert ppp.find_discharge_periods(prices, 4, 4)[0] == [20, 21, 22, 23]
        assert ppp.find_charge_periods(prices, 4, 4)[0] == [0, 1, 2, 3]

    def test_real_day(self):
        """Test that a real day finds the afternoon dip and the evening peak."""
        discharge = ppp.find_discharge_periods(PRICES_2026_03_07, 8, 8)
        charge = ppp.find_charge_periods(PRICES_2026_03_07, 8, 8)
        assert all(64 <= q < 84 for q in discharge[0])  # 16:00-21:00
        assert all(52 <= q < 64 for q in charge[0])  # 13:00-16:00

    def test_no_window(self):
        """Test zero-length windows and too little data."""
        assert ppp.find_discharge_periods([1.0, 2.0], 4, 0) == []
        assert ppp.find_charge_periods([1.0, 2.0], 4, 4) == []


def test_build_schedule_merges_and_covers_the_day():
    """Test that the schedule covers the data without gaps."""
    values = nordpool_values([1.0] * 8)
    schedule = ppp.build_schedule(
        values, [([2, 3], [5, 6])], ("charge", "discharge", "pause"), 90, 20
    )
    assert [(s["state"], s["soc"]) for s in schedule] == [
        ("pause", 90),
        ("charge", 90),
        ("pause", 90),
        ("discharge", 20),
        ("pause", 90),
    ]
    assert schedule[0]["start"] == values[0]["start"]
    assert schedule[-1]["end"] == values[-1]["end"]
    for current, following in pairwise(schedule):
        assert current["end"] == following["start"]
    assert ppp.build_schedule([], [], ("charge", "discharge", "pause"), 90, 20) == []


@pytest.fixture
def mock_hass():
    """Create a mock hass with price peak settings."""
    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "config": {
                "price_peak_nr_of_charge_hours": 2,
                "price_peak_nr_of_discharge_hours": 2,
                "price_peak_efficiency_factor": 85,
                "network_cost": 63,
                "network_compensation": 5,
                "battery_max_soc": 90,
                "battery_shutdown_soc": 20,
            },
            "save": AsyncMock(),
        }
    }
    return hass


async def run_plan_day(hass, values):
    """Run plan_day before the day starts and return the written schedule."""
    with (
        patch.object(ppp.dt_utils, "now", return_value=DAY - dt.timedelta(hours=1)),
        patch.object(ppp, "write_schedule") as write_schedule,
    ):
        await ppp.plan_day(hass, values, {})
    return write_schedule.call_args.args[1]


async def test_plan_day_real_prices_self_use(mock_hass):
    """Test a real day with real fees: charge at the dip, use it at the peak."""
    schedule = await run_plan_day(mock_hass, nordpool_values(PRICES_2026_03_07))
    states = [s["state"] for s in schedule]
    assert "charge" in states
    assert "discharge" in states
    charge = next(s for s in schedule if s["state"] == "charge")
    discharge = next(s for s in schedule if s["state"] == "discharge")
    assert 13 <= charge["start"].hour < 16
    assert 16 <= discharge["start"].hour < 21
    assert charge["start"] < discharge["start"]
    assert schedule[0]["start"] == DAY
    assert schedule[-1]["end"] == DAY + dt.timedelta(days=1)


async def test_plan_day_selling_needs_a_bigger_spread(mock_hass):
    """Test that selling at the same peak does not pay with the network fees."""
    mock_hass.data[DOMAIN]["config"]["price_peak_planner_expensive_state"] = "sell"
    schedule = await run_plan_day(mock_hass, nordpool_values(PRICES_2026_03_07))
    assert [s["state"] for s in schedule] == ["pause"]


async def test_plan_day_without_prices(mock_hass):
    """Test that no prices give an empty schedule (raised IndexError)."""
    assert await run_plan_day(mock_hass, []) == []
