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


def test_forced_charge_stops_at_its_target():
    """Test that a manual charge to 12 kWh then holds, even where selling pays."""
    forced = [None] * 96
    forced[0:16] = ["charge"] * 16
    target = [None] * 96
    target[0:16] = [12.0] * 16
    prices = [100.0] * 16 + [3000.0] * 80
    plan = opt.optimize(
        STARTS,
        prices,
        LOAD,
        NO_PV,
        battery(),
        NO_FEES,
        forced=forced,
        forced_soc=target,
    )
    assert hours(plan, 0, 4) == {"charge"}
    assert max(plan.soc[0:16]) == pytest.approx(12.0)
    # At the target the battery is kept, the load comes from the grid
    assert plan.soc[15] == pytest.approx(12.0)


def test_forced_sell_stops_at_its_target_then_self_use():
    """Test that a manual sell to 10 kWh then covers the load from the battery."""
    forced = [None] * 96
    forced[0:16] = ["sell"] * 16
    target = [None] * 96
    target[0:16] = [10.0] * 16
    plan = opt.optimize(
        STARTS,
        [500.0] * 96,
        LOAD,
        NO_PV,
        battery(soc_kwh=15.0),
        NO_FEES,
        forced=forced,
        forced_soc=target,
    )
    # 15 -> 10 kWh at 10 kW less the 1 kW load: two quarters, then self-use
    assert plan.modes[0:2] == ["sell", "sell"]
    assert plan.soc[1] == pytest.approx(10.0)
    assert set(plan.modes[2:16]) == {"self_use"}
    assert plan.soc[15] == pytest.approx(10.0 - 14 * 0.25)


def test_forced_sell_may_go_below_the_reserve():
    """Test that a manual sell's target below the reserve is sold down to."""
    forced = [None] * 96
    forced[0:8] = ["sell"] * 8
    target = [None] * 96
    target[0:8] = [3.0] * 8
    reserve = opt.Battery(
        min_kwh=2.0,
        max_kwh=20.0,
        soc_kwh=10.0,
        max_grid_charge_kw=10.0,
        max_power_kw=10.0,
        efficiency=1.0,
        reserve_kwh=6.0,
    )
    plan = opt.optimize(
        STARTS,
        [500.0] * 96,
        [0.0] * 96,
        NO_PV,
        reserve,
        NO_FEES,
        forced=forced,
        forced_soc=target,
    )
    assert min(plan.soc[0:8]) == pytest.approx(3.0)
    assert plan.modes[0] == "sell"


def test_the_repeated_hour_is_two_power_charge_hours():
    """Test that the two 02:xx hours when DST ends are not one 8 kWh hour."""
    utc = dt.datetime(2026, 10, 25, 0, tzinfo=dt.UTC)  # 02:00 CEST
    starts = [(utc + dt.timedelta(minutes=15 * i)).astimezone(TZ) for i in range(8)]
    hourly = opt._hourly(starts, [1.0] * 8)
    assert sorted(hourly.values()) == [4.0, 4.0]


def test_month_share_counts_real_hours():
    """Test that October has 745 hours (DST ends), so a full plan covers it all."""
    first = dt.datetime(2026, 10, 1, tzinfo=TZ)
    utc = first.astimezone(dt.UTC)
    starts = [
        (utc + dt.timedelta(minutes=15 * i)).astimezone(TZ) for i in range(745 * 4)
    ]
    assert opt._month_share(starts[:4], first) == pytest.approx(1 / 745)


def test_month_share():
    """Test the share of the month's remaining hours a plan covers."""
    assert opt._month_share(STARTS, STARTS[0]) == pytest.approx(24 / (19 * 24))
    last = [s.replace(day=31) for s in STARTS]
    assert opt._month_share(last, last[0]) == pytest.approx(1.0)


def test_marginal_is_the_price_of_more_load():
    """Test what more load costs: the buy price, the lost export, or battery energy."""
    probe = 0.075
    # Empty battery and no PV: every extra kWh is bought
    plan = opt.optimize(
        STARTS, [1000.0] * 96, LOAD, NO_PV, battery(), NO_FEES, probe_kwh=probe
    )
    assert plan.marginal == pytest.approx([1.25] * 96)
    # Full battery that cannot take the PV: the extra kWh is not exported
    stuck = opt.Battery(2.0, 20.0, 20.0, 0.0, 0.0, 0.81)
    plan = opt.optimize(
        STARTS, [1000.0] * 96, [0.0] * 96, PV_MIDDAY, stuck, NO_FEES, probe_kwh=probe
    )
    assert plan.marginal[11 * 4] == pytest.approx(1.0)
    # Without a probe there is no marginal
    assert (
        opt.optimize(STARTS, [1000.0] * 96, LOAD, NO_PV, battery(), NO_FEES).marginal
        == []
    )


def test_exempt_load_ignores_the_import_limit():
    """Test that exempt load (the heat pump) is not held back by the import limit."""
    limit = opt.Limit(1.0, 0, 24)
    heat_pump = [0.075] * 96
    load = [0.25 + 0.075] * 96  # 1 kW house + 0.3 kW heat pump
    capped = opt.optimize(
        STARTS, [1000.0] * 96, load, NO_PV, battery(), NO_FEES, limit=limit
    )
    exempt = opt.optimize(
        STARTS,
        [1000.0] * 96,
        load,
        NO_PV,
        battery(),
        NO_FEES,
        limit=limit,
        exempt=heat_pump,
    )
    assert capped.limit_excess_kwh == pytest.approx(0.075 * 96)
    assert exempt.limit_excess_kwh == 0.0


def test_reserve_is_kept():
    """Test that the plan does not discharge below the reserve."""
    kept = dataclasses.replace(battery(soc_kwh=10.0), reserve_kwh=4.0)
    plan = opt.optimize(
        STARTS, prices_by_hour(2000, 2000, 2000), LOAD, NO_PV, kept, NO_FEES
    )
    assert min(plan.soc) == pytest.approx(4.0)
    assert plan.end_value == pytest.approx(0.0)


def test_reserve_is_refilled():
    """Test that a battery below the reserve is charged back at the cheapest price."""
    low = dataclasses.replace(battery(soc_kwh=2.0), reserve_kwh=4.0)
    plan = opt.optimize(
        STARTS, prices_by_hour(100, 2000, 2000), LOAD, NO_PV, low, NO_FEES
    )
    assert hours(plan, 0, 6) & {"charge"}
    assert plan.soc[-1] >= 4.0 - 1e-6


def test_peak_levels_ignore_the_import_limit():
    """Test that the power charge level is published apart from the import limit."""
    plan = opt.optimize(
        STARTS,
        prices_by_hour(100, 2000, 2000),
        LOAD,
        NO_PV,
        battery(),
        TEKNISKA_VERKEN,
        limit=opt.Limit(0.5),
        history={DAY.replace(day=5, hour=12): 3.0, DAY.replace(day=5, hour=2): 2.0},
    )
    noon = STARTS.index(DAY.replace(hour=12))
    level = plan.peak_levels[noon]
    assert level is not None
    assert level >= 3.0
    assert plan.targets[noon] == pytest.approx(0.5)


def car(need=8.0, plugged=None, deadline=28, powers=(4.0, 8.0)):
    """Return a car needing `need` kWh by quarter `deadline` (07:00)."""
    return opt.Vehicle(
        powers_kw=powers,
        need_kwh=need,
        plugged=plugged if plugged is not None else [True] * 96,
        deadline=deadline,
    )


def charged(plan, first, last):
    """Return the car's kWh charged between two hours."""
    return sum(plan.ev_kwh[first * 4 : last * 4])


def test_car_charges_in_the_cheapest_quarters():
    """Test that the car gets its need by the deadline, in the cheap night hours."""
    prices = [100.0 if 2 <= s.hour < 4 else 1000.0 for s in STARTS]
    plan = opt.optimize(
        STARTS,
        prices,
        LOAD,
        NO_PV,
        battery(soc_kwh=2.0, max_kwh=2.5),
        NO_FEES,
        vehicle=car(),
    )
    assert sum(plan.ev_kwh) == pytest.approx(8.0)
    assert charged(plan, 2, 4) == pytest.approx(8.0)
    assert plan.ev_missing_kwh == pytest.approx(0.0)
    # The car's energy is imported (no battery worth using)
    assert sum(plan.grid_import) == pytest.approx(8.0 + sum(LOAD), abs=0.5)


def test_car_only_charges_while_plugged_in():
    """Test that a car home from 04:00 can not use the cheaper hours before."""
    plugged = [s.hour >= 4 for s in STARTS]
    prices = [100.0 if s.hour < 4 else 500.0 if s.hour < 6 else 1000.0 for s in STARTS]
    plan = opt.optimize(
        STARTS,
        prices,
        LOAD,
        NO_PV,
        battery(max_kwh=2.5),
        NO_FEES,
        vehicle=car(plugged=plugged),
    )
    assert charged(plan, 0, 4) == 0.0
    assert charged(plan, 4, 6) == pytest.approx(8.0)
    assert charged(plan, 7, 24) == 0.0  # nothing after the deadline


def test_car_need_that_can_not_be_met():
    """Test that a car plugged in too briefly charges at full power and misses."""
    plugged = [6 * 4 <= i < 7 * 4 for i in range(96)]  # 06:00-07:00, at most 8 kWh
    plan = opt.optimize(
        STARTS,
        [500.0] * 96,
        LOAD,
        NO_PV,
        battery(max_kwh=2.5),
        NO_FEES,
        vehicle=car(need=12.0, plugged=plugged),
    )
    assert charged(plan, 6, 7) == pytest.approx(8.0)
    assert plan.ev_missing_kwh == pytest.approx(4.0)
    assert plan.ev_missing_cost == pytest.approx(40.0)


def test_car_deadline_after_the_horizon():
    """Test a need after the horizon: deferred if there is time, else charged now.

    Two days, the last day's cheapest price (the end price) at 22-24, one cheaper
    hour on the first night. With the deadline far after the horizon the car takes
    the cheaper hour; with a deadline one hour after it, only 8 kWh can wait.
    """
    starts = [DAY + dt.timedelta(minutes=15 * i) for i in range(192)]
    prices = [
        50.0 if i // 4 == 3 else 200.0 if (i // 4) % 24 >= 22 else 1000.0
        for i in range(192)
    ]
    load, no_pv = [0.25] * 192, [0.0] * 192
    later = opt.optimize(
        starts,
        prices,
        load,
        no_pv,
        battery(max_kwh=2.5),
        NO_FEES,
        vehicle=car(plugged=[True] * 192, deadline=400),
    )
    assert charged(later, 3, 4) == pytest.approx(8.0)
    assert later.ev_missing_kwh == pytest.approx(0.0)
    # 20 kWh by an hour after the horizon at 8 kW: at least 12 kWh now, the rest is
    # left for the hour after it at the end price
    soon = opt.optimize(
        starts,
        [500.0] * 192,
        load,
        no_pv,
        battery(max_kwh=2.5),
        NO_FEES,
        vehicle=car(need=20.0, plugged=[True] * 192, deadline=196),
    )
    assert sum(soon.ev_kwh) == pytest.approx(12.0, abs=1.0)
    assert soon.ev_missing_cost == pytest.approx(
        soon.ev_missing_kwh * 500 * 1.25 / 1000
    )


def test_car_and_battery_share_the_power_charge():
    """Test that the car and the battery spread over the night instead of a peak.

    Both want the cheap 00-06 hours; with a night power charge charging the car at
    8 kW in one hour costs more than spreading it, the battery fills around it.
    """
    night = tariff.Tariff(
        power_charges=(
            tariff.PowerCharge(
                kr_per_kw=200.0, period=tariff.Period(start_hour=0, end_hour=6)
            ),
        )
    )
    prices = [100.0 if s.hour < 6 else 2000.0 for s in STARTS]
    plan = opt.optimize(
        STARTS,
        prices,
        LOAD,
        NO_PV,
        battery(soc_kwh=2.0, max_kwh=8.0),
        night,
        vehicle=car(need=8.0, deadline=24),
    )
    assert sum(plan.ev_kwh) == pytest.approx(8.0)
    # 8 kWh car + 6 kWh battery + 6 kWh load in 6 hours: about 3.3 kW, not 8+
    assert peak(plan.grid_import, 0, 6) < 4.5


def test_main_fuse_limits_car_and_battery_together():
    """Test that the car and the battery share the fuse in the cheap hours."""
    prices = [100.0 if 2 <= s.hour < 4 else 1000.0 for s in STARTS]

    def run(fuse_kw):
        return opt.optimize(
            STARTS,
            prices,
            LOAD,
            NO_PV,
            battery(),
            NO_FEES,
            limit=opt.Limit(fuse_kw=fuse_kw),
            vehicle=car(),
        )

    free, fused = run(0.0), run(12.0)
    # Without a fuse both charge at full power: 8 + 10 + 1 kW
    assert max(free.grid_import) / 0.25 == pytest.approx(19.0)
    assert max(fused.grid_import) / 0.25 <= 12.0 + 1e-9
    assert fused.fuse_excess_kwh == 0.0
    assert sum(fused.ev_kwh) == pytest.approx(8.0)
    # The inverter's import target includes the car and stays below the fuse
    for target, kwh in zip(fused.targets, fused.ev_kwh, strict=True):
        assert target is not None
        assert target <= 12.0 + 1e-9
        if kwh > 0:
            assert target >= kwh / 0.25
    assert all(level == 12.0 for level in fused.peak_levels)
