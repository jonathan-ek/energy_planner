"""Tests for the battery plan from its inputs (planning.py), without HA."""

import dataclasses
import datetime as dt
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.planner import (
    forecast,
    manual,
    planning,
    settings,
)
from custom_components.energy_planner.planner.battery_optimizer import Limit
from custom_components.energy_planner.planner.tariff import (
    TARIFF_PRESETS,
    fuse_power,
    tariff_from_dict,
)
from custom_components.energy_planner.planner.timeline import Timeline

TZ = ZoneInfo("Europe/Stockholm")
NOW = dt.datetime(2026, 1, 13, 22, 50, tzinfo=TZ)  # Tuesday evening, winter
START = dt.datetime(2026, 1, 13, tzinfo=TZ)
STARTS = [START + dt.timedelta(minutes=15 * i) for i in range(192)]
BATTERY = settings.battery(
    {
        "battery_capacity": 25600,
        "battery_shutdown_soc": 15,
        "battery_max_soc": 90,
        "max_charge_current": 20,
        "max_discharge_current": 18,
        "battery_efficiency": 85,
        "battery_wear_cost": 20,
        # The scenarios do not keep a reserve unless they set one
        "battery_reserve_soc": 0,
    }
)
HEAT_PUMP_SETTINGS = {
    "heat_pump_power": 300,
    "heat_pump_margin": 5,
    "heat_pump_heating_limit": 15,
}
# A day limit of 1 kW, as the owner's
LIMIT = Limit(1.0, 6, 23, 0.0)


def make_forecast(starts, **changes):
    """Return a forecast: 1 kW load until 17, 2 kW after, no PV or planned loads."""
    count = len(starts)
    made = forecast.Forecast(
        updated=starts[0],
        timeline=Timeline.of(starts),
        load=tuple(0.25 if s.hour < 17 else 0.5 for s in starts),
        planned=(0.0,) * count,
        reserve=(0.0,) * count,
        pv=(0.0,) * count,
    )
    return dataclasses.replace(made, **changes)


def prices(starts=STARTS):
    """Cheap night 00-06, expensive evening."""
    return {
        s: 200.0 if s.hour < 6 else 1500.0 if s.hour >= 17 else 800.0 for s in starts
    }


def heat_pump(outdoor=None, indoor=None, **config):
    """Return the heat pump's readings with its settings."""
    return planning.HeatPumpReadings(settings.heat_pump(config), outdoor, indoor)


def inputs(**changes):
    """Return the inputs at NOW: SOC 20 % at 536 V, today's and tomorrow's prices."""
    made = planning.PlanInputs(
        now=NOW,
        forecast=make_forecast(STARTS),
        published=prices(),
        estimated={},
        battery=BATTERY,
        soc=20.0,
        voltage=536.0,
        tariff=tariff_from_dict(TARIFF_PRESETS["tekniska_verken_alternativ"]),
        limit=LIMIT,
        import_history={},
        manual_slots=(),
        last_plan=None,
        car=None,
        heat_pump=heat_pump(),
    )
    return dataclasses.replace(made, **changes)


def plan(**changes):
    """Build the plan and return it as the plan sensor publishes it."""
    result = planning.build_plan(inputs(**changes))
    assert result is not None
    return result.to_attributes()


def car(soc=None, charge_limit=None, delivered_kwh=None, **config):
    """Return a plugged-in car's readings, ready at 06:30 by default."""
    car_settings = settings.car(
        {
            "ev_charger_status_sensor": "sensor.charger_status",
            "ev_ready_time": dt.time(6, 30),
            **config,
        }
    )
    assert car_settings is not None
    return planning.CarReadings(car_settings, soc, charge_limit, delivered_kwh)


def test_plan_from_the_current_quarter():
    """Test the plan's horizon, the night charging and the published values."""
    result = plan()
    starts = [dt.datetime.fromisoformat(s) for s in result["starts"]]
    assert starts[0] == dt.datetime(2026, 1, 13, 22, 45, tzinfo=TZ)
    assert starts[-1] == STARTS[-1]
    modes = dict(zip(starts, result["modes"], strict=True))
    night = [m for s, m in modes.items() if s.date() == NOW.date() + dt.timedelta(1)]
    assert "charge" in night[: 6 * 4]
    assert "charge" not in night[6 * 4 : 23 * 4]
    # 6 A at 536 V: 10.7 kW, so the peak is the planner's choice
    assert max(result["grid_import"]) < 10.7
    # SOC at the end of each quarter: the first evening quarter uses the battery
    assert result["soc_start"] == 20.0
    assert 15 < result["soc"][0] < 20
    assert result["wear_cost"] > 0
    assert {p["month"] for p in result["peaks"]} == {"2026-01"}
    assert {tuple(p["hours"]) for p in result["peaks"]} == {(6, 23), (23, 6)}


def test_nothing_to_plan():
    """Test no plan without a price now, or without room in the battery."""
    assert planning.build_plan(inputs(published={})) is None
    full = dataclasses.replace(BATTERY, max_soc=BATTERY.shutdown_soc)
    assert planning.build_plan(inputs(battery=full)) is None


def test_estimated_prices_continue_the_plan():
    """Test that estimated prices extend the published ones and are marked."""
    today = {s: p for s, p in prices().items() if s.date() == NOW.date()}
    tomorrow = {s: p for s, p in prices().items() if s.date() > NOW.date()}
    result = plan(published=today, estimated=tomorrow)
    starts = [dt.datetime.fromisoformat(s) for s in result["starts"]]
    assert starts[-1] == STARTS[-1]
    assert result["estimated"] == [s.date() > NOW.date() for s in starts]
    # Without the estimate the plan ends with today
    result = plan(published=today)
    assert result["starts"][-1] == STARTS[95].isoformat()


def test_plan_ends_where_the_pv_forecast_does():
    """Test that quarters without a PV forecast (Forecast.Solar) end the plan."""
    starts = [START + dt.timedelta(minutes=15 * i) for i in range(288)]
    result = plan(
        forecast=make_forecast(starts, pv=(0.0,) * 192 + (None,) * 96),
        estimated=prices(starts[192:]),
    )
    assert result["starts"][-1] == starts[191].isoformat()
    assert not any(result["estimated"])


def test_the_night_dst_ends():
    """Test that the repeated hour gets its own quarters and prices."""
    now = dt.datetime(2026, 10, 24, 23, 0, tzinfo=TZ)
    # UTC quarters from 23:00 CEST to the end of Sunday: 02:xx comes twice
    quarters = Timeline.between(now, dt.datetime(2026, 10, 26, tzinfo=TZ), TZ)
    published = {start: 100.0 + i for i, start in enumerate(quarters.starts)}
    result = plan(
        now=now,
        forecast=make_forecast([quarters.local(i) for i in range(len(quarters))]),
        published=published,
    )
    assert len(result["starts"]) == len(quarters) == 4 + 100
    # Every quarter keeps its own price, the second 02:00 too (öre incl. VAT)
    assert len(set(result["prices"])) == len(quarters)
    second = [s for s in result["starts"] if s.startswith("2026-10-25T02:00")]
    assert second == ["2026-10-25T02:00:00+02:00", "2026-10-25T02:00:00+01:00"]


def test_peak_already_reached_is_used():
    """Test that this month's night peak sets the free import level."""
    result = plan(import_history={dt.datetime(2026, 1, 2, 3, tzinfo=TZ): 6.0})
    night = next(p for p in result["peaks"] if p["hours"] == [23, 6])
    assert night["reached_kw"] == 6.0
    assert night["planned_kw"] >= 6.0
    # Only the day peak (no day hours yet this month) costs: 1 kW at 45 kr/kW
    day = next(p for p in result["peaks"] if p["hours"] == [6, 23])
    assert result["power_cost"] == pytest.approx(day["planned_kw"] * 45, abs=0.1)


def test_manual_slot_is_planned_around():
    """Test that a manual sell slot forces the plan's mode in its quarters."""
    start = dt.datetime(2026, 1, 14, 18, tzinfo=TZ)
    slot = manual.make(start, start + dt.timedelta(hours=1), "sell", 30)
    result = plan(manual_slots=(slot,))
    first = result["starts"].index(start.isoformat())
    assert result["modes"][first] == "sell"
    assert result["manual"][first : first + 4] == ["sell"] * 4
    assert result["manual_soc"][first : first + 4] == [30.0] * 4
    assert result["manual"][first + 4] is None
    # Sold down to the target, not below it while the slot runs
    assert min(result["soc"][first : first + 4]) >= 30.0 - 1.0


def test_running_mode_is_passed_on():
    """Test that the next calculation knows the mode the last plan has now."""
    last = planning.build_plan(inputs())
    assert last is not None
    with patch.object(planning, "optimize", wraps=planning.optimize) as optimize:
        planning.build_plan(inputs(last_plan=last))
    assert optimize.call_args.kwargs["current_mode"] == last.modes[0]


@pytest.mark.parametrize(("temperature", "heats"), [(0.0, True), (20.0, False)])
def test_heat_pump_is_planned(temperature, heats):
    """Test that the heat pump runs on cheap night electricity when heat is needed."""
    result = plan(
        forecast=make_forecast(STARTS, temperature=(temperature,) * 192),
        heat_pump=heat_pump(**HEAT_PUMP_SETTINGS),
    )
    on = dict(zip(result["starts"], result["heat_pump"], strict=True))
    night = dt.datetime(2026, 1, 14, 3, tzinfo=TZ).isoformat()
    assert on[night] is heats
    assert len(result["marginal"]) == len(result["starts"])
    # At least 0.79 kr/kWh at night (200 SEK/MWh * 1.25 + 54 öre night fees); more,
    # because the battery charges at the night import target and the load raises it
    assert result["marginal"][result["starts"].index(night)] >= 79.0
    # Its load is exempt from the 1 kW day limit
    assert result["limit_excess_kwh"] == 0.0


def test_heat_pump_quarters():
    """Test COP, price and heat need per quarter, the live temperature as fallback."""
    starts = [NOW, NOW + dt.timedelta(minutes=15)]
    quarters = planning.heat_pump_quarters(
        heat_pump(outdoor=20.0, **HEAT_PUMP_SETTINGS), None, starts, [0.0, None]
    )
    assert quarters.probe_kwh == pytest.approx(0.075)
    assert quarters.temperatures == [0.0, 20.0]
    assert quarters.cops[0] == pytest.approx(3.0)
    # Tekniska verken in January
    assert quarters.district == [99.7, 99.7]
    assert quarters.heating == [True, False]


def test_heat_pump_quarters_use_the_heat_loss():
    """Test that the measured heat loss gives the draw per quarter."""
    loss = 1.5 / 21
    readings = heat_pump(indoor=21.0, **HEAT_PUMP_SETTINGS)
    quarters = planning.heat_pump_quarters(readings, loss, [NOW] * 2, [0.0, 7.0])
    # 0.5 kW at 0 °C, 14 / 21 * 1.5 kW heat at COP 3.62 at 7 °C
    assert quarters.draws == pytest.approx([0.125, 0.25 / 3.62])
    assert quarters.probe_kwh == pytest.approx((0.125 + 0.25 / 3.62) / 2)
    # Without an indoor temperature the setting is used
    readings = heat_pump(**HEAT_PUMP_SETTINGS)
    quarters = planning.heat_pump_quarters(readings, loss, [NOW], [0.0])
    assert quarters.draws == pytest.approx([0.075])
    # 0 W still turns planning off
    readings = heat_pump(**(HEAT_PUMP_SETTINGS | {"heat_pump_power": 0}))
    quarters = planning.heat_pump_quarters(readings, loss, [NOW], [0.0])
    assert quarters.probe_kwh == 0.0


def test_plugged_car_charges_at_night():
    """Test that a plugged-in car is charged in the cheap night before 06:30."""
    result = plan(
        forecast=make_forecast(STARTS, ev_need_kwh=20.0), car=car(delivered_kwh=4.0)
    )
    ev = result["ev"]
    # 20 kWh from the history, 4 already delivered this session
    assert ev["need_kwh"] == pytest.approx(16.0)
    assert ev["need_source"] == "history"
    assert ev["ready"] == dt.datetime(2026, 1, 14, 6, 30, tzinfo=TZ).isoformat()
    assert ev["planned_kwh"] == pytest.approx(16.0, abs=0.5)
    assert ev["missing_kwh"] == pytest.approx(0.0, abs=0.5)
    starts = [dt.datetime.fromisoformat(s) for s in result["starts"]]
    charging = [s for s, kw in zip(starts, ev["power"], strict=True) if kw > 0]
    # In the 00-06 cheap hours, at the charger's 11 kW
    assert all(s.hour < 6 for s in charging)
    assert {kw for kw in ev["power"] if kw > 0} == {11.0}


def test_car_from_soc():
    """Test that the car's SOC and charge limit give the need when known."""
    result = plan(car=car(soc=60.0, charge_limit=80.0, ev_battery_capacity=45))
    assert result["ev"]["need_source"] == "soc"
    assert result["ev"]["need_kwh"] == pytest.approx(0.2 * 45 / 0.9)


def test_unplugged_car_is_not_planned():
    """Test that without a plugged-in car the plan has no car."""
    assert plan(forecast=make_forecast(STARTS, ev_need_kwh=20.0))["ev"] is None


@pytest.mark.parametrize("need", [None, 20.0])
def test_car_without_a_known_need_is_not_planned(need):
    """Test that without a need, or a session past it, the car charges as usual."""
    result = plan(
        forecast=make_forecast(STARTS, ev_need_kwh=need), car=car(delivered_kwh=25.0)
    )
    assert result["ev"] is None


def test_car_trip_charges_full():
    """Test that a departure before the working morning plans a full battery."""
    departure = dt.datetime(2026, 1, 14, 4, 0, tzinfo=TZ)
    result = plan(
        forecast=make_forecast(STARTS, ev_need_kwh=20.0),
        car=car(delivered_kwh=25.0, ev_battery_capacity=45, ev_departure=departure),
    )
    ev = result["ev"]
    # Past the history's 20 kWh, but a trip fills the battery: 45 / 0.9 - 25
    assert ev["need_source"] == "trip"
    assert ev["need_kwh"] == pytest.approx(25.0)
    assert ev["ready"] == departure.isoformat()
    starts = [dt.datetime.fromisoformat(s) for s in result["starts"]]
    charging = [s for s, kw in zip(starts, ev["power"], strict=True) if kw > 0]
    assert charging
    assert max(charging) < departure


def test_main_fuse_caps_the_import():
    """Test that the main fuse caps the import targets."""
    result = plan(limit=Limit(0.0, 6, 23, fuse_power(10)))
    fuse = 3 * 230 * 10 / 1000
    assert all(t is not None and t <= fuse + 1e-9 for t in result["targets"])
    assert max(result["grid_import"]) <= fuse + 0.01
    assert result["fuse_excess_kwh"] == 0.0
