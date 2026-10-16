"""Tests for the battery plan glue."""

import datetime as dt
import importlib
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from custom_components.energy_planner.const import DOMAIN, PLAN_SENSORS
from custom_components.energy_planner.planner import price_estimate

battery_plan = importlib.import_module(
    "custom_components.energy_planner.planner.battery_plan"
)

TZ = ZoneInfo("Europe/Stockholm")
NOW = dt.datetime(2026, 1, 13, 22, 50, tzinfo=TZ)  # Tuesday evening, winter
START = dt.datetime(2026, 1, 13, tzinfo=TZ)
STARTS = [START + dt.timedelta(minutes=15 * i) for i in range(192)]
STATES = {
    "sensor.solis_s6_solis_battery_soc": "20",
    "sensor.solis_s6_solis_battery_voltage": "536",
}


@pytest.fixture
def mock_hass():
    """Create a mock hass with a forecast, battery settings and sensor states."""
    default = battery_plan.dt_utils.DEFAULT_TIME_ZONE
    battery_plan.dt_utils.set_default_time_zone(TZ)
    hass = MagicMock()
    hass.states.get = lambda entity_id: (
        SimpleNamespace(state=STATES[entity_id]) if entity_id in STATES else None
    )
    hass.async_add_executor_job = AsyncMock(side_effect=lambda job: job())
    hass.data = {
        DOMAIN: {
            "options": {"tariff_preset": "tekniska_verken_alternativ"},
            "config": {
                "battery_capacity": 25600,
                "battery_shutdown_soc": 15,
                "battery_max_soc": 90,
                "max_charge_current": 20,
                "max_discharge_current": 18,
                "price_peak_efficiency_factor": 85,
                "battery_wear_cost": 20,
                "grid_import_limit": 1.0,
                "grid_import_limit_start": 6,
                "grid_import_limit_end": 23,
            },
            "forecast": {
                "starts": [s.isoformat() for s in STARTS],
                "load": [0.25 if s.hour < 17 else 0.5 for s in STARTS],
                "planned": [0.0] * 192,
                "reserve": [0.0] * 192,
                "pv": [0.0] * 192,
            },
            PLAN_SENSORS: [MagicMock()],
        }
    }
    yield hass
    battery_plan.dt_utils.set_default_time_zone(default)


def prices():
    """Cheap night 00-06, expensive evening, for today and tomorrow."""
    return {
        s: 200.0 if s.hour < 6 else 1500.0 if s.hour >= 17 else 800.0 for s in STARTS
    }


async def run(hass, history=None):
    """Run async_update_plan at NOW."""
    with (
        patch.object(battery_plan.dt_utils, "now", return_value=NOW),
        patch.object(battery_plan, "_prices", AsyncMock(return_value=prices())),
        patch.object(battery_plan, "_price_history", AsyncMock(return_value={})),
        patch.object(
            battery_plan, "_hourly_import", AsyncMock(return_value=history or {})
        ),
    ):
        await battery_plan.async_update_plan(hass)
    return hass.data[DOMAIN].get("plan")


async def test_plan_from_the_current_quarter(mock_hass):
    """Test the plan's horizon, the night charging and the published values."""
    plan = await run(mock_hass)
    starts = [dt.datetime.fromisoformat(s) for s in plan["starts"]]
    assert starts[0] == dt.datetime(2026, 1, 13, 22, 45, tzinfo=TZ)
    assert starts[-1] == STARTS[-1]
    modes = dict(zip(starts, plan["modes"], strict=True))
    night = [m for s, m in modes.items() if s.date() == NOW.date() + dt.timedelta(1)]
    assert "charge" in night[: 6 * 4]
    assert "charge" not in night[6 * 4 : 23 * 4]
    # 6 A at 536 V: 10.7 kW, so the peak is the planner's choice
    assert max(plan["grid_import"]) < 10.7
    # SOC at the end of each quarter: the first evening quarter uses the battery
    assert 15 < plan["soc"][0] < 20
    assert plan["wear_cost"] > 0
    assert {p["month"] for p in plan["peaks"]} == {"2026-01"}
    assert {tuple(p["hours"]) for p in plan["peaks"]} == {(6, 23), (23, 6)}
    mock_hass.data[DOMAIN][PLAN_SENSORS][0].async_write_ha_state.assert_called_once()


async def test_peak_already_reached_is_used(mock_hass):
    """Test that this month's night peak sets the free import level."""
    history = {dt.datetime(2026, 1, 2, 3, tzinfo=TZ): 6.0}
    plan = await run(mock_hass, history)
    night = next(p for p in plan["peaks"] if p["hours"] == [23, 6])
    assert night["reached_kw"] == 6.0
    assert night["planned_kw"] >= 6.0
    # Only the day peak (no day hours yet this month) costs: 1 kW at 45 kr/kW
    day = next(p for p in plan["peaks"] if p["hours"] == [6, 23])
    assert plan["power_cost"] == pytest.approx(day["planned_kw"] * 45, abs=0.1)


async def test_no_battery_state(mock_hass):
    """Test that a missing SOC sensor skips the plan."""
    with patch.dict(STATES, clear=True):
        assert await run(mock_hass) is None


async def test_manual_slot_is_planned_around(mock_hass):
    """Test that a manual sell slot forces the plan's mode in its quarters."""
    mock_hass.data[DOMAIN]["config"]["planner_state"] = "dynamic"
    start = dt.datetime(2026, 1, 14, 18, tzinfo=TZ)
    mock_hass.data[DOMAIN]["manual_slots"] = [
        {
            "start": start,
            "end": start + dt.timedelta(hours=1),
            "state": "sell",
            "soc": 20,
        }
    ]
    plan = await run(mock_hass)
    modes = dict(zip(plan["starts"], plan["modes"], strict=True))
    hour = [modes[(start + dt.timedelta(minutes=15 * i)).isoformat()] for i in range(4)]
    assert hour == ["sell"] * 4
    # A preview with another planner is not forced
    mock_hass.data[DOMAIN]["config"]["planner_state"] = "off"
    plan = await run(mock_hass)
    modes = dict(zip(plan["starts"], plan["modes"], strict=True))
    hour = [modes[(start + dt.timedelta(minutes=15 * i)).isoformat()] for i in range(4)]
    assert hour != ["sell"] * 4


async def test_running_mode_is_passed_on(mock_hass):
    """Test that the next calculation knows the mode running now."""
    await run(mock_hass)
    first = mock_hass.data[DOMAIN]["plan"]["modes"][0]
    with patch.object(
        battery_plan, "optimize", wraps=battery_plan.optimize
    ) as optimize:
        await run(mock_hass)
    assert optimize.call_args.kwargs["current_mode"] == first


async def test_requests_during_a_run_are_coalesced(mock_hass):
    """Test that requests while the plan is calculated give one more run."""
    calls = []

    async def update(hass):
        calls.append(1)
        if len(calls) == 1:
            await battery_plan.async_request_plan(hass, "second")
            await battery_plan.async_request_plan(hass, "third")

    with patch.object(battery_plan, "async_update_plan", update):
        await battery_plan.async_request_plan(mock_hass, "first")
    assert len(calls) == 2


async def test_current_action_follows_an_auto_slot(mock_hass):
    """Test the action sensor's value for an auto slot."""
    await run(mock_hass)
    mock_hass.data[DOMAIN]["values"] = {
        "slot_1_date_time_start": NOW - dt.timedelta(hours=1),
        "slot_1_state": "auto",
        "slot_1_active": True,
        "slot_1_soc": 50,
        "slot_2_date_time_start": NOW + dt.timedelta(days=1),
    }
    with patch.object(battery_plan.dt_utils, "now", return_value=NOW):
        result = battery_plan.current_action(mock_hass)
    assert result["source"] == "plan"
    assert result["mode"] == mock_hass.data[DOMAIN]["plan"]["modes"][0]


@pytest.mark.parametrize(("temperature", "heats"), [(0.0, True), (20.0, False)])
async def test_heat_pump_is_planned(mock_hass, temperature, heats):
    """Test that the heat pump runs on cheap night electricity when heat is needed."""
    mock_hass.data[DOMAIN]["config"].update(
        {"heat_pump_power": 300, "heat_pump_margin": 5, "heat_pump_heating_limit": 15}
    )
    mock_hass.data[DOMAIN]["forecast"]["temperature"] = [temperature] * 192
    plan = await run(mock_hass)
    on = dict(zip(plan["starts"], plan["heat_pump"], strict=True))
    night = dt.datetime(2026, 1, 14, 3, tzinfo=TZ).isoformat()
    assert on[night] is heats
    assert len(plan["marginal"]) == len(plan["starts"])
    # At least 0.79 kr/kWh at night (200 SEK/MWh * 1.25 + 54 öre night fees); more,
    # because the battery charges at the night import target and the load raises it
    assert plan["marginal"][plan["starts"].index(night)] >= 79.0
    # Its load is exempt from the 1 kW day limit
    assert plan["limit_excess_kwh"] == 0.0


async def test_tomorrow_is_estimated(mock_hass):
    """Test that without tomorrow's prices the plan continues on an estimate."""
    mock_hass.data[DOMAIN]["config"]["nordpool_entity_id"] = "sensor.nordpool_kwh_se3"
    today = {s: p for s, p in prices().items() if s.date() == NOW.date()}
    # The sensor's history in öre/kWh incl. VAT: the same daily profile
    sensor = {
        (START - dt.timedelta(days=i)).date(): {
            hour: (200.0 if hour < 6 else 1500.0 if hour >= 17 else 800.0) * 0.125
            for hour in range(24)
        }
        for i in range(30)
    }
    with (
        patch.object(battery_plan.dt_utils, "now", return_value=NOW),
        patch.object(battery_plan, "_prices", AsyncMock(return_value=today)),
        patch.object(battery_plan, "_price_history", AsyncMock(return_value=sensor)),
        patch.object(battery_plan, "_hourly_import", AsyncMock(return_value={})),
        patch.object(battery_plan, "_wind_forecast", AsyncMock(return_value=None)),
    ):
        await battery_plan.async_update_plan(mock_hass)
    plan = mock_hass.data[DOMAIN]["plan"]
    starts = [dt.datetime.fromisoformat(s) for s in plan["starts"]]
    assert starts[-1] == STARTS[-1]
    tomorrow = [s.date() > NOW.date() for s in starts]
    assert plan["estimated"] == tomorrow
    # Scaled back to the spot price, 200 SEK/MWh at night, plus most of the gap to
    # tonight's 1500: in öre incl. VAT
    night = plan["prices"][tomorrow.index(True)]
    gap = math.exp(-0.5 / price_estimate.GAP_TIME_CONSTANT)
    assert night == pytest.approx((200 + 1300 * gap) * 0.125, abs=0.01)
    estimate = mock_hass.data[DOMAIN]["price_estimate"]
    assert estimate["day"] == NOW.date() + dt.timedelta(days=1)
    assert estimate["wind"] is False


async def test_wind_forecast(mock_hass):
    """Test that Open-Meteo's answer for all sites becomes speed per date and hour."""
    times = ["2026-01-14T00:00", "2026-01-14T01:00"]
    answer = [
        {"hourly": {"time": times, "wind_speed_100m_previous_day1": [36.0, None]}}
        for _ in price_estimate.WIND_SITES
    ]
    response = MagicMock()
    response.json = AsyncMock(return_value=answer)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get.return_value = context
    with patch.object(battery_plan, "async_get_clientsession", return_value=session):
        wind = await battery_plan._wind_forecast(mock_hass, dt.date(2026, 1, 15))
    assert wind is not None
    assert set(wind) == set(price_estimate.WIND_SITES)
    assert wind["se3"] == {dt.date(2026, 1, 14): {0: 36.0}}
    params = session.get.call_args.kwargs["params"]
    assert params["end_date"] == "2026-01-15"
    assert len(params["latitude"].split(",")) == len(price_estimate.WIND_SITES)


async def test_no_wind_forecast_on_error(mock_hass):
    """Test that an Open-Meteo failure gives no wind, so the estimate goes without."""
    session = MagicMock()
    session.get.side_effect = aiohttp.ClientError("down")
    with patch.object(battery_plan, "async_get_clientsession", return_value=session):
        assert (
            await battery_plan._wind_forecast(mock_hass, dt.date(2026, 1, 15)) is None
        )


async def test_no_estimate_without_history(mock_hass):
    """Test that without the sensor's history the plan ends with today's prices."""
    mock_hass.data[DOMAIN]["config"]["nordpool_entity_id"] = "sensor.nordpool_kwh_se3"
    today = {s: p for s, p in prices().items() if s.date() == NOW.date()}
    with (
        patch.object(battery_plan.dt_utils, "now", return_value=NOW),
        patch.object(battery_plan, "_prices", AsyncMock(return_value=today)),
        patch.object(battery_plan, "_price_history", AsyncMock(return_value={})),
        patch.object(battery_plan, "_hourly_import", AsyncMock(return_value={})),
        patch.object(battery_plan, "_wind_forecast", AsyncMock(return_value=None)),
    ):
        await battery_plan.async_update_plan(mock_hass)
    plan = mock_hass.data[DOMAIN]["plan"]
    assert not any(plan["estimated"])
    assert plan["starts"][-1] == STARTS[95].isoformat()


async def test_plugged_car_charges_at_night(mock_hass):
    """Test that a plugged-in car is charged in the cheap night before 06:30."""
    STATES["sensor.charger_status"] = "connected_charging"
    STATES["sensor.charger_session"] = "4.0"
    try:
        config = mock_hass.data[DOMAIN]["config"]
        config["ev_charger_status_sensor"] = "sensor.charger_status"
        config["ev_session_energy_sensor"] = "sensor.charger_session"
        config["ev_ready_time"] = dt.time(6, 30)
        mock_hass.data[DOMAIN]["forecast"]["ev_need_kwh"] = 20.0
        plan = await run(mock_hass)
    finally:
        del STATES["sensor.charger_status"], STATES["sensor.charger_session"]
    car = plan["ev"]
    # 20 kWh from the history, 4 already delivered this session
    assert car["need_kwh"] == pytest.approx(16.0)
    assert car["need_source"] == "history"
    assert car["ready"] == dt.datetime(2026, 1, 14, 6, 30, tzinfo=TZ).isoformat()
    assert car["planned_kwh"] == pytest.approx(16.0, abs=0.5)
    assert car["missing_kwh"] == pytest.approx(0.0, abs=0.5)
    starts = [dt.datetime.fromisoformat(s) for s in plan["starts"]]
    charging = [s for s, kw in zip(starts, car["power"], strict=True) if kw > 0]
    # In the 00-06 cheap hours, at the charger's 11 kW
    assert all(s.hour < 6 for s in charging)
    assert {kw for kw in car["power"] if kw > 0} == {11.0}


async def test_car_from_soc(mock_hass):
    """Test that the car's SOC and charge limit give the need when known."""
    STATES.update(
        {"sensor.charger_status": "connected_finished", "sensor.car_soc": "60"}
    )
    STATES["sensor.car_limit"] = "80"
    try:
        config = mock_hass.data[DOMAIN]["config"]
        config["ev_charger_status_sensor"] = "sensor.charger_status"
        config["ev_soc_sensor"] = "sensor.car_soc"
        config["ev_charge_limit_sensor"] = "sensor.car_limit"
        config["ev_battery_capacity"] = 45
        plan = await run(mock_hass)
    finally:
        for key in ("sensor.charger_status", "sensor.car_soc", "sensor.car_limit"):
            del STATES[key]
    assert plan["ev"]["need_source"] == "soc"
    assert plan["ev"]["need_kwh"] == pytest.approx(0.2 * 45 / 0.9)


async def test_unplugged_car_is_not_planned(mock_hass):
    """Test that without a plugged-in car the plan has no car."""
    mock_hass.data[DOMAIN]["config"]["ev_charger_status_sensor"] = "sensor.missing"
    plan = await run(mock_hass)
    assert plan["ev"] is None


async def test_car_past_its_history_need_is_not_planned(mock_hass):
    """Test that a session past the history's need is left to charge as usual."""
    STATES["sensor.charger_status"] = "connected_charging"
    STATES["sensor.charger_session"] = "25.0"
    try:
        config = mock_hass.data[DOMAIN]["config"]
        config["ev_charger_status_sensor"] = "sensor.charger_status"
        config["ev_session_energy_sensor"] = "sensor.charger_session"
        mock_hass.data[DOMAIN]["forecast"]["ev_need_kwh"] = 20.0
        plan = await run(mock_hass)
    finally:
        del STATES["sensor.charger_status"], STATES["sensor.charger_session"]
    assert plan["ev"] is None


async def test_car_trip_charges_full(mock_hass):
    """Test that a departure before the working morning plans a full battery."""
    STATES["sensor.charger_status"] = "connected_charging"
    STATES["sensor.charger_session"] = "25.0"
    departure = dt.datetime(2026, 1, 14, 4, 0, tzinfo=TZ)
    try:
        config = mock_hass.data[DOMAIN]["config"]
        config["ev_charger_status_sensor"] = "sensor.charger_status"
        config["ev_session_energy_sensor"] = "sensor.charger_session"
        config["ev_battery_capacity"] = 45
        config["ev_departure"] = departure
        mock_hass.data[DOMAIN]["forecast"]["ev_need_kwh"] = 20.0
        plan = await run(mock_hass)
    finally:
        del STATES["sensor.charger_status"], STATES["sensor.charger_session"]
        del config["ev_departure"]
    car = plan["ev"]
    # Past the history's 20 kWh, but a trip fills the battery: 45 / 0.9 - 25
    assert car["need_source"] == "trip"
    assert car["need_kwh"] == pytest.approx(25.0)
    assert car["ready"] == departure.isoformat()
    starts = [dt.datetime.fromisoformat(s) for s in plan["starts"]]
    charging = [s for s, kw in zip(starts, car["power"], strict=True) if kw > 0]
    assert charging
    assert max(charging) < departure


async def test_main_fuse_caps_the_import(mock_hass):
    """Test that the main fuse setting caps the import targets."""
    mock_hass.data[DOMAIN]["config"].update({"grid_import_limit": 0, "main_fuse": 10})
    plan = await run(mock_hass)
    fuse = 3 * 230 * 10 / 1000
    assert all(t is not None and t <= fuse + 1e-9 for t in plan["targets"])
    assert max(plan["grid_import"]) <= fuse + 0.01
    assert plan["fuse_excess_kwh"] == 0.0
