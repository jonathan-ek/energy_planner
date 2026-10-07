"""Tests for the battery schedule optimizer."""

import dataclasses
import datetime as dt
import importlib
from zoneinfo import ZoneInfo

import pytest

opt = importlib.import_module(
    "custom_components.energy_planner.planner.battery_optimizer"
)
tariff = importlib.import_module("custom_components.energy_planner.planner.tariff")

TZ = ZoneInfo("Europe/Stockholm")
# A Tuesday in January: winter power charges
DAY = dt.datetime(2026, 1, 13, tzinfo=TZ)
STARTS = [DAY + dt.timedelta(minutes=15 * i) for i in range(96)]
NO_FEES = tariff.Tariff()
TEKNISKA_VERKEN = tariff.tariff_from_dict(tariff.TEKNISKA_VERKEN_ALTERNATIV)


def battery(soc_kwh=2.0, max_kwh=20.0, grid_charge_kw=10.0):
    """Return a lossless battery."""
    return opt.Battery(
        min_kwh=2.0,
        max_kwh=max_kwh,
        soc_kwh=soc_kwh,
        max_grid_charge_kw=grid_charge_kw,
        max_power_kw=10.0,
        efficiency=1.0,
    )


def prices_by_hour(night, day, evening):
    """Return quarter prices: night 00-06, day 06-17, evening 17-24."""
    return [night if s.hour < 6 else day if s.hour < 17 else evening for s in STARTS]


def hours(plan, first, last):
    """Return the plan's modes from hour `first` up to `last`."""
    return set(plan.modes[first * 4 : last * 4])


def peak(values, first, last):
    """Return the highest hourly import between two hours."""
    return max(sum(values[h * 4 : h * 4 + 4]) for h in range(first, last))


LOAD = [0.25] * 96  # 1 kW
NO_PV = [0.0] * 96


def test_charges_cheap_and_uses_expensive():
    """Test that the battery is filled at night and used in the evening."""
    plan = opt.optimize(
        STARTS, prices_by_hour(100, 2000, 2000), LOAD, NO_PV, battery(), NO_FEES
    )
    assert hours(plan, 0, 6) >= {"charge"}
    assert "charge" not in hours(plan, 6, 24)
    # 18 kWh covers 18 hours of 1 kW: no import after the cheap night
    assert hours(plan, 6, 24) == {"self_use"}
    assert peak(plan.grid_import, 6, 24) == pytest.approx(0.0)
    assert plan.power_cost == 0.0


def test_flat_prices_need_no_charging():
    """Test that charging does not pay without a price difference."""
    plan = opt.optimize(STARTS, [500.0] * 96, LOAD, NO_PV, battery(), NO_FEES)
    assert "charge" not in plan.modes
    assert "sell" not in plan.modes


def test_import_limit_stops_daytime_charging():
    """Test that cheap daytime prices do not charge above the 1 kW limit."""
    # Cheapest at noon, but the limit applies 06-23 and the load uses it all
    prices = [100.0 if 11 <= s.hour < 14 else 1000.0 for s in STARTS]
    plan = opt.optimize(
        STARTS, prices, LOAD, NO_PV, battery(), NO_FEES, opt.Limit(1.0, 6, 23)
    )
    assert peak(plan.grid_import, 6, 23) <= 1.0 + 1e-9
    assert plan.limit_excess_kwh == pytest.approx(0.0)


def test_hold_shaves_above_the_limit():
    """Test that holding the battery still covers the load above the limit."""
    load = [0.5] * 96  # 2 kW, 1 kW above the limit
    plan = opt.optimize(
        STARTS,
        [500.0] * 96,
        load,
        NO_PV,
        battery(soc_kwh=20.0),
        NO_FEES,
        opt.Limit(1.0, 6, 23),
    )
    assert peak(plan.grid_import, 6, 23) <= 1.0 + 1e-9


class TestPowerCharge:
    """Tests for the trade-off between charging speed and the monthly peak."""

    night_charge = tariff.Tariff(
        power_charges=(
            tariff.PowerCharge(50.0, tariff.Period(start_hour=0, end_hour=6)),
        )
    )

    def test_peak_already_paid_is_free(self):
        """Test that charging up to this month's peak costs no power charge."""
        history = {dt.datetime(2026, 1, 2, 3, tzinfo=TZ): 5.0}
        plan = opt.optimize(
            STARTS,
            prices_by_hour(100, 2000, 2000),
            LOAD,
            NO_PV,
            battery(),
            self.night_charge,
            history=history,
        )
        assert peak(plan.grid_import, 0, 6) >= 4.0
        assert plan.power_cost == pytest.approx(0.0)

    def test_a_late_month_peak_is_kept_low(self):
        """Test that on the last day the peak costs in full, so it charges slower."""
        last_day = [s.replace(day=31) for s in STARTS]
        cheap = opt.optimize(
            last_day,
            prices_by_hour(100, 2000, 2000),
            LOAD,
            NO_PV,
            battery(),
            tariff.Tariff(),
        )
        charged = opt.optimize(
            last_day,
            prices_by_hour(100, 2000, 2000),
            LOAD,
            NO_PV,
            battery(),
            self.night_charge,
        )
        assert peak(charged.grid_import, 0, 6) < peak(cheap.grid_import, 0, 6)

    def test_an_early_month_peak_is_spread(self):
        """Test that early in the month a higher peak pays off over the month."""
        early = opt.optimize(
            [s.replace(day=1) for s in STARTS],
            prices_by_hour(100, 2000, 2000),
            LOAD,
            NO_PV,
            battery(),
            self.night_charge,
        )
        late = opt.optimize(
            [s.replace(day=31) for s in STARTS],
            prices_by_hour(100, 2000, 2000),
            LOAD,
            NO_PV,
            battery(),
            self.night_charge,
        )
        assert peak(early.grid_import, 0, 6) > peak(late.grid_import, 0, 6)


def test_tekniska_verken_winter_day():
    """Test a winter day with the 1 kW limit: night charging, no day import peak."""
    plan = opt.optimize(
        STARTS,
        prices_by_hour(300, 900, 1500),
        [0.25 if s.hour < 6 else 0.3 if s.hour < 17 else 0.5 for s in STARTS],
        NO_PV,
        battery(),
        TEKNISKA_VERKEN,
        opt.Limit(1.0, 6, 23),
    )
    assert hours(plan, 0, 6) >= {"charge"}
    assert "charge" not in hours(plan, 6, 23)
    assert peak(plan.grid_import, 6, 23) <= 1.0 + 1e-9
    assert plan.peaks["0:2026-01"][1] <= 1.0 + 1e-9


def test_pv_surplus_is_stored_not_sold():
    """Test that midday PV fills the battery for the evening."""
    pv = [1.0 if 10 <= s.hour < 14 else 0.0 for s in STARTS]  # 4 kW
    plan = opt.optimize(STARTS, [1000.0] * 96, LOAD, pv, battery(), NO_FEES)
    assert plan.soc[14 * 4 - 1] > plan.soc[10 * 4] + 10
    assert "charge" not in plan.modes


PV_MIDDAY = [1.0 if 10 <= s.hour < 14 else 0.0 for s in STARTS]  # 4 kW


def lossy_battery(soc_kwh):
    """Return a battery with 81% round trip efficiency and no grid charging."""
    return opt.Battery(2.0, 20.0, soc_kwh, 0.0, 10.0, 0.81)


def test_pv_surplus_is_exported_while_prices_fall():
    """Test that a nearly full battery exports the surplus while the price is high."""
    prices = [1200.0 if 10 <= s.hour < 12 else 1000.0 for s in STARTS]
    plan = opt.optimize(
        STARTS, prices, [0.0] * 96, PV_MIDDAY, lossy_battery(18.0), NO_FEES
    )
    assert hours(plan, 10, 12) == {"sell_excess"}
    # Too small a difference to cover the losses of emptying the battery
    assert "sell" not in plan.modes
    assert plan.soc[12 * 4 - 1] == pytest.approx(18.0)


def test_full_battery_exports_in_self_use():
    """Test that a full battery exports the surplus in self_use, not sell_excess."""
    plan = opt.optimize(
        STARTS, [1000.0] * 96, [0.0] * 96, PV_MIDDAY, lossy_battery(20.0), NO_FEES
    )
    assert set(plan.modes) == {"self_use"}
    assert sum(plan.grid_export) == pytest.approx(16.0)


NEGATIVE_AT_11 = [-100.0 if 11 <= s.hour < 12 else 300.0 for s in STARTS]


def test_negative_export_price_discards_the_surplus():
    """Test that surplus a full battery cannot take is curtailed, not exported."""
    # -100 SEK/MWh + 5 öre/kWh: exporting costs 5 öre/kWh 11-12, earns 35 öre else
    stuck = opt.Battery(2.0, 20.0, 20.0, 0.0, 0.0, 0.81)  # can neither charge nor sell
    plan = opt.optimize(
        STARTS, NEGATIVE_AT_11, [0.0] * 96, PV_MIDDAY, stuck, tariff.flat_tariff(60, 5)
    )
    assert hours(plan, 11, 12) == {"discard_excess"}
    assert hours(plan, 10, 11) | hours(plan, 12, 14) == {"self_use"}
    assert sum(plan.grid_export) == pytest.approx(12.0)


def test_negative_export_price_makes_room():
    """Test that a full battery is emptied before PV exported at a loss."""
    plan = opt.optimize(
        STARTS,
        NEGATIVE_AT_11,
        [0.0] * 96,
        PV_MIDDAY,
        lossy_battery(20.0),
        tariff.flat_tariff(60, 5),
    )
    assert "sell" in hours(plan, 10, 11)
    assert sum(plan.grid_export[11 * 4 : 12 * 4]) == 0.0


def test_sells_at_a_price_spike():
    """Test that a full battery is sold into a spike worth more than later use."""
    prices = [8000.0 if s.hour == 18 else 300.0 for s in STARTS]
    plan = opt.optimize(
        STARTS, prices, [0.0] * 96, NO_PV, battery(soc_kwh=20.0), NO_FEES
    )
    assert hours(plan, 18, 19) == {"sell"}
    # 10 kW for an hour
    assert sum(plan.grid_export) == pytest.approx(10.0)


def test_wear_cost_stops_small_trades():
    """Test that selling and refilling from PV needs to pay for the extra cycle."""
    # Sell at 10, refill from the 12-14 PV that would otherwise be exported at 1.0
    prices = [1150.0 if s.hour == 10 else 1000.0 for s in STARTS]
    full = battery(soc_kwh=20.0, grid_charge_kw=0.0)
    plan = opt.optimize(STARTS, prices, [0.0] * 96, PV_MIDDAY, full, NO_FEES)
    assert hours(plan, 10, 11) == {"sell"}
    assert plan.wear_cost == 0.0
    worn = dataclasses.replace(full, wear_cost=0.2)
    plan = opt.optimize(STARTS, prices, [0.0] * 96, PV_MIDDAY, worn, NO_FEES)
    assert "sell" not in plan.modes
    # Using the battery for the house still pays: 1.25 SEK/kWh saved
    plan = opt.optimize(STARTS, [1000.0] * 96, LOAD, NO_PV, worn, NO_FEES)
    assert set(plan.modes) == {"self_use"}
    assert plan.wear_cost == pytest.approx(18 * 0.2)


def test_no_hoarding_before_a_cheap_night():
    """Test that energy is used rather than kept when the horizon ends cheap."""
    prices = [
        1500.0 if s.hour < 12 else 1000.0 if s.hour < 22 else 300.0 for s in STARTS
    ]
    full = opt.Battery(2.0, 20.0, 20.0, 10.0, 10.0, 0.81, 0.2)
    plan = opt.optimize(STARTS, prices, LOAD, NO_PV, full, tariff.flat_tariff(60, 5))
    # 18 kWh: 12 for the expensive morning, the rest before the cheap night
    assert hours(plan, 0, 12) == {"self_use"}
    assert plan.soc[22 * 4 - 1] == pytest.approx(2.0)
    # Nor bought at the end just to be kept: it can be bought when it is needed
    assert "charge" not in plan.modes


def test_no_flip_flop_for_small_differences():
    """Test that modes worth a few öre more are not switched to every quarter."""
    # Exporting the surplus is worth 1 öre/kWh more every other quarter
    prices = [1010.0 if i % 2 else 1000.0 for i in range(96)]
    plan = opt.optimize(
        STARTS, prices, [0.0] * 96, PV_MIDDAY, lossy_battery(18.0), NO_FEES
    )
    changes = sum(a != b for a, b in zip(plan.modes, plan.modes[1:], strict=False))
    assert changes <= 2


def test_current_mode_is_kept_when_nearly_as_good():
    """Test that a recalculated plan keeps the running mode unless switching pays."""
    pv = [1.0 if 10 <= s.hour < 14 else 0.0 for s in STARTS]
    starts, prices = STARTS[40:], [1000.0] * 56
    free = opt.optimize(
        starts, prices, [0.0] * 56, pv[40:], lossy_battery(19.9), NO_FEES
    )
    kept = opt.optimize(
        starts,
        prices,
        [0.0] * 56,
        pv[40:],
        lossy_battery(19.9),
        NO_FEES,
        current_mode="sell_excess",
    )
    assert free.modes[0] == "self_use"
    assert kept.modes[0] == "sell_excess"


def test_forced_modes_are_used():
    """Test that a manual slot's mode is planned around."""
    forced = [None] * 96
    forced[18 * 4 : 19 * 4] = ["sell"] * 4
    plan = opt.optimize(
        STARTS,
        [500.0] * 96,
        [0.0] * 96,
        NO_PV,
        battery(soc_kwh=20.0),
        NO_FEES,
        forced=forced,
    )
    assert hours(plan, 18, 19) == {"sell"}
    # Not worth it at a flat price, but the slot says so: 10 kW for an hour
    assert plan.soc[18 * 4 - 1] - plan.soc[19 * 4 - 1] == pytest.approx(10.0)


def test_month_share():
    """Test the share of the month's remaining hours a plan covers."""
    assert opt._month_share(STARTS, STARTS[0]) == pytest.approx(24 / (19 * 24))
    last = [s.replace(day=31) for s in STARTS]
    assert opt._month_share(last, last[0]) == pytest.approx(1.0)
