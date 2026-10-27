"""Tests for the battery plan glue."""

import dataclasses
import datetime as dt
import importlib
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.const import (
    DOMAIN,
    NUMBER_ENTITIES,
    PLAN_SENSORS,
)
from custom_components.energy_planner.planner import (
    forecast,
    manual,
    planning,
    price_estimate,
    settings,
)
from custom_components.energy_planner.planner import prices as spot
from custom_components.energy_planner.planner.timeline import Timeline

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


@pytest.fixture(autouse=True)
def no_network():
    """Keep the price history sources off the network and the disk."""
    with (
        patch.object(spot, "_wind_forecast", AsyncMock(return_value=None)),
        patch.object(spot, "_energy_charts_history", AsyncMock(return_value=None)),
        patch.object(spot, "async_save_to_store", AsyncMock()) as save,
    ):
        yield save


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
                "battery_efficiency": 85,
                "battery_wear_cost": 20,
                # The scenarios do not keep a reserve unless they set one
                "battery_reserve_soc": 0,
                "grid_import_limit": 1.0,
                "grid_import_limit_start": 6,
                "grid_import_limit_end": 23,
            },
            "forecast": make_forecast(STARTS),
            PLAN_SENSORS: [MagicMock()],
            spot.PRICE_STORE: {},
        }
    }
    yield hass
    battery_plan.dt_utils.set_default_time_zone(default)


def make_forecast(starts):
    """Return a forecast: 1 kW load until 17, 2 kW after, no PV or planned loads."""
    count = len(starts)
    return forecast.Forecast(
        updated=starts[0],
        timeline=Timeline.of(starts),
        load=tuple(0.25 if s.hour < 17 else 0.5 for s in starts),
        planned=(0.0,) * count,
        reserve=(0.0,) * count,
        pv=(0.0,) * count,
    )


def forecast_data(hass, **changes):
    """Change fields of the mock forecast."""
    data = hass.data[DOMAIN]
    data["forecast"] = dataclasses.replace(data["forecast"], **changes)


def prices():
    """Cheap night 00-06, expensive evening, for today and tomorrow."""
    return {
        s: 200.0 if s.hour < 6 else 1500.0 if s.hour >= 17 else 800.0 for s in STARTS
    }


async def run(hass, history=None, price_history=None):
    """Run async_update_plan at NOW."""
    with (
        patch.object(battery_plan.dt_utils, "now", return_value=NOW),
        patch.object(spot, "_published", AsyncMock(return_value=prices())),
        patch.object(
            spot,
            "_price_history",
            AsyncMock(return_value=price_history or {}),
        ),
        patch.object(
            battery_plan, "_hourly_import", AsyncMock(return_value=history or {})
        ),
    ):
        await battery_plan.async_update_plan(hass)
    plan = hass.data[DOMAIN].get("plan")
    # As the plan sensor publishes it
    return plan.to_attributes() if plan is not None else None


async def test_plan_is_stored_and_published(mock_hass):
    """Test that the plan is stored, available from when it was made, and shown."""
    finished = NOW + dt.timedelta(seconds=3)
    # The prune, the prices and the plan at NOW, done 3 s later
    clock = MagicMock(side_effect=[NOW, NOW, NOW, finished])
    with (
        patch.object(battery_plan.dt_utils, "now", clock),
        patch.object(spot, "_published", AsyncMock(return_value=prices())),
        patch.object(battery_plan, "_hourly_import", AsyncMock(return_value={})),
    ):
        await battery_plan.async_update_plan(mock_hass)
    stored = mock_hass.data[DOMAIN]["plan"]
    assert stored.timeline.local(0) == dt.datetime(2026, 1, 13, 22, 45, tzinfo=TZ)
    assert (stored.updated, stored.finished) == (NOW, finished)
    mock_hass.data[DOMAIN][PLAN_SENSORS][0].async_write_ha_state.assert_called_once()


async def test_inputs_from_the_settings_and_sensors(mock_hass):
    """Test what the plan is made from: settings, live readings and the last plan."""
    STATES.update(
        {
            "sensor.charger_status": "connected_charging",
            "sensor.charger_session": "4.0",
            "sensor.car_soc": "60",
            "sensor.gw1100a_outdoor_temperature": "3.5",
        }
    )
    config = mock_hass.data[DOMAIN]["config"]
    config.update(
        {
            "main_fuse": 20,
            "ev_charger_status_sensor": "sensor.charger_status",
            "ev_session_energy_sensor": "sensor.charger_session",
            "ev_soc_sensor": "sensor.car_soc",
        }
    )
    slot = manual.make(NOW, NOW + dt.timedelta(hours=1), "sell", 30)
    mock_hass.data[DOMAIN]["manual_slots"] = [slot]
    last = object()
    mock_hass.data[DOMAIN]["plan"] = last
    history = {dt.datetime(2026, 1, 2, 3, tzinfo=dt.UTC): 6.0}
    build = MagicMock(return_value=None)
    try:
        with (
            patch.object(planning, "build_plan", build),
            patch.object(
                battery_plan, "_hourly_import", AsyncMock(return_value=history)
            ) as hourly,
            patch.object(battery_plan.dt_utils, "now", return_value=NOW),
            patch.object(spot, "_published", AsyncMock(return_value=prices())),
        ):
            await battery_plan.async_update_plan(mock_hass)
    finally:
        for key in (
            "sensor.charger_status",
            "sensor.charger_session",
            "sensor.car_soc",
            "sensor.gw1100a_outdoor_temperature",
        ):
            del STATES[key]
    inputs = build.call_args.args[0]
    assert (inputs.now, inputs.soc, inputs.voltage) == (NOW, 20.0, 536.0)
    assert inputs.battery.capacity_kwh == 25.6
    assert inputs.published == prices()
    assert inputs.forecast is mock_hass.data[DOMAIN]["forecast"]
    limit = inputs.limit
    assert (limit.kw, limit.start_hour, limit.end_hour) == (1.0, 6, 23)
    # 20 A on three phases
    assert limit.fuse_kw == pytest.approx(13.8)
    # This month's import, from the HAN meter by default
    assert hourly.call_args.args[1:] == (
        "sensor.matarstallning_aktiv_energi_uttag",
        dt.datetime(2026, 1, 1, tzinfo=TZ),
        NOW,
    )
    assert inputs.import_history == history
    assert (inputs.manual_slots, inputs.last_plan) == ((slot,), last)
    # The car without a charge limit sensor
    assert inputs.car is not None
    assert (inputs.car.soc, inputs.car.charge_limit, inputs.car.delivered_kwh) == (
        60.0,
        None,
        4.0,
    )
    assert inputs.heat_pump.outdoor == 3.5
    # Nothing to plan: the last plan stays
    assert mock_hass.data[DOMAIN]["plan"] is last


async def test_unplugged_car_is_not_planned(mock_hass):
    """Test that a car that is set up but not plugged in is left out."""
    mock_hass.data[DOMAIN]["config"]["ev_charger_status_sensor"] = "sensor.missing"
    build = MagicMock(return_value=None)
    with patch.object(planning, "build_plan", build):
        await run(mock_hass)
    assert build.call_args.args[0].car is None


@pytest.fixture
def car_hass(mock_hass):
    """Return mock_hass with a plugged-in car that has delivered 6 kWh."""
    STATES.update({"sensor.charger_status": "connected_charging"})
    STATES.update({"sensor.charger_session": "6.0"})
    mock_hass.data[DOMAIN]["config"].update(
        {
            "ev_charger_status_sensor": "sensor.charger_status",
            "ev_session_energy_sensor": "sensor.charger_session",
        }
    )
    entity = MagicMock(id="ev_soc")
    mock_hass.data[DOMAIN]["save"] = AsyncMock()
    mock_hass.data[DOMAIN][NUMBER_ENTITIES] = [entity]
    try:
        yield mock_hass
    finally:
        del STATES["sensor.charger_status"], STATES["sensor.charger_session"]


async def test_entered_soc_with_the_session_energy(car_hass):
    """Test that an entered SOC is stored with the session energy and planned."""
    with patch.object(battery_plan, "async_request_plan", AsyncMock()) as request:
        await battery_plan.async_set_ev_soc(car_hass, 40, charge_limit=80)
        # The plan follows the entity's state change
        request.assert_not_awaited()
        await battery_plan.async_set_ev_soc(car_hass, 40)
        # The same SOC again changes no state
        request.assert_awaited_once()
    config = car_hass.data[DOMAIN]["config"]
    assert (config["ev_soc"], config["ev_soc_session_kwh"]) == (40.0, 6.0)
    assert config["ev_charge_limit"] == 80.0
    car_hass.data[DOMAIN]["save"].assert_awaited()
    car_hass.data[DOMAIN][NUMBER_ENTITIES][0].update.assert_called()
    car = battery_plan._car(car_hass)
    assert car is not None
    assert (car.entered_soc, car.entered_at_kwh, car.delivered_kwh) == (40.0, 6.0, 6.0)


async def test_entered_soc_before_plugging_in(car_hass):
    """Test that a SOC entered before plugging in counts the whole session."""
    STATES["sensor.charger_status"] = "disconnected"
    await battery_plan.async_set_ev_soc(car_hass, 30)
    assert car_hass.data[DOMAIN]["config"]["ev_soc_session_kwh"] == 0.0


@pytest.mark.parametrize("soc", [-1, 101])
async def test_entered_soc_out_of_range(car_hass, soc):
    """Test that a SOC outside 0-100 % is refused."""
    with pytest.raises(ValueError, match="not a SOC"):
        await battery_plan.async_set_ev_soc(car_hass, soc)
    assert "ev_soc" not in car_hass.data[DOMAIN]["config"]


async def test_unplugging_clears_the_entered_soc(car_hass):
    """Test that the entered SOC ends with the session."""
    await battery_plan.async_set_ev_soc(car_hass, 40)
    await battery_plan.async_car_unplugged(car_hass)
    config = car_hass.data[DOMAIN]["config"]
    assert (config["ev_soc"], config["ev_soc_session_kwh"]) == (None, None)


async def test_no_battery_state(mock_hass):
    """Test that without a SOC the plan and the estimate are skipped, not the prices."""
    estimate = AsyncMock(return_value={})
    with (
        patch.dict(STATES, clear=True),
        patch.object(spot, "_estimated_prices", estimate),
    ):
        assert await run(mock_hass) is None
    # The heat pump sensors read them also without a plan
    assert mock_hass.data[DOMAIN]["prices"].published == prices()
    # No recorder history or network calls for a plan that can not be made
    estimate.assert_not_called()


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


async def test_current_action_follows_the_plan_or_a_manual_slot(mock_hass):
    """Test the action sensor's value: the plan, off, and a manual slot."""
    await run(mock_hass)
    with patch.object(battery_plan.dt_utils, "now", return_value=NOW):
        result = battery_plan.current_action(mock_hass)
    assert result["source"] == "plan"
    assert result["mode"] == mock_hass.data[DOMAIN]["plan"].modes[0]
    # With the planner off the plan is not followed
    mock_hass.data[DOMAIN]["config"]["enabled"] = False
    with patch.object(battery_plan.dt_utils, "now", return_value=NOW):
        result = battery_plan.current_action(mock_hass)
    assert (result["state"], result["source"]) == ("discharge", "none")
    # A manual slot runs while off and without a plan, to its target
    mock_hass.data[DOMAIN]["plan"] = None
    mock_hass.data[DOMAIN]["manual_slots"] = [
        manual.make(NOW, NOW + dt.timedelta(hours=1), "charge", 70)
    ]
    with patch.object(battery_plan.dt_utils, "now", return_value=NOW):
        result = battery_plan.current_action(mock_hass)
    assert (result["state"], result["source"], result["soc"]) == (
        "charge",
        "manual",
        70,
    )
    # Rounded out to whole quarters: 22:45 to 00:00
    assert result["until"] == (NOW + dt.timedelta(minutes=70)).isoformat()


async def test_tomorrow_is_estimated(mock_hass):
    """Test that without tomorrow's prices the plan continues on an estimate."""
    mock_hass.data[DOMAIN]["config"]["nordpool_entity_id"] = (
        "sensor.nordpool_kwh_se3_sek"
    )
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
        patch.object(spot, "_published", AsyncMock(return_value=today)),
        patch.object(spot, "_price_history", AsyncMock(return_value=sensor)),
        patch.object(battery_plan, "_hourly_import", AsyncMock(return_value={})),
    ):
        await battery_plan.async_update_plan(mock_hass)
    plan = mock_hass.data[DOMAIN]["plan"].to_attributes()
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


def three_days(hass):
    """Extend the mock forecast to today and the next two days."""
    starts = [START + dt.timedelta(minutes=15 * i) for i in range(288)]
    hass.data[DOMAIN]["forecast"] = make_forecast(starts)
    return starts


def sensor_history():
    """Return the Nord Pool sensor's history (öre/kWh incl. VAT): the daily profile."""
    return {
        (START - dt.timedelta(days=i)).date(): {
            hour: (200.0 if hour < 6 else 1500.0 if hour >= 17 else 800.0) * 0.125
            for hour in range(24)
        }
        for i in range(30)
    }


async def test_day_after_tomorrow_is_estimated(mock_hass):
    """Test that with tomorrow's prices published the plan goes on a day estimated."""
    mock_hass.data[DOMAIN]["config"]["nordpool_entity_id"] = (
        "sensor.nordpool_kwh_se3_sek"
    )
    starts = three_days(mock_hass)
    wind = AsyncMock(return_value=None)
    with patch.object(spot, "_wind_forecast", wind):
        plan = await run(mock_hass, price_history=sensor_history())
    day = NOW.date() + dt.timedelta(days=2)
    assert plan["starts"][-1] == starts[-1].isoformat()
    planned = [dt.datetime.fromisoformat(s) for s in plan["starts"]]
    assert plan["estimated"] == [s.date() == day for s in planned]
    estimate = mock_hass.data[DOMAIN]["price_estimate"]
    assert (estimate["day"], estimate["lead"]) == (day, 2)
    # The wind correction from the forecast made two days before
    assert wind.call_args.args[1:] == (day, 2)
    # At midnight the same day is tomorrow: estimated again with lead 1
    with patch.object(battery_plan.dt_utils, "now", return_value=NOW):
        cached = await spot._estimated_prices(mock_hass, prices(), NOW)
    assert cached == estimate["prices"]
    midnight = dt.datetime.combine(NOW.date() + dt.timedelta(days=1), dt.time(), TZ)
    tomorrow_only = {s: p for s, p in prices().items() if s.date() > NOW.date()}
    with (
        patch.object(spot, "_wind_forecast", wind),
        patch.object(spot, "_price_history", AsyncMock(return_value=sensor_history())),
        patch.object(battery_plan.dt_utils, "now", return_value=midnight),
    ):
        await spot._estimated_prices(mock_hass, tomorrow_only, midnight)
    assert mock_hass.data[DOMAIN]["price_estimate"]["lead"] == 1
    assert wind.call_args.args[1:] == (day, 1)


async def test_no_estimate_without_history(mock_hass):
    """Test that without the sensor's history the plan ends with today's prices."""
    mock_hass.data[DOMAIN]["config"]["nordpool_entity_id"] = (
        "sensor.nordpool_kwh_se3_sek"
    )
    today = {s: p for s, p in prices().items() if s.date() == NOW.date()}
    with (
        patch.object(battery_plan.dt_utils, "now", return_value=NOW),
        patch.object(spot, "_published", AsyncMock(return_value=today)),
        patch.object(spot, "_price_history", AsyncMock(return_value={})),
        patch.object(battery_plan, "_hourly_import", AsyncMock(return_value={})),
    ):
        await battery_plan.async_update_plan(mock_hass)
    plan = mock_hass.data[DOMAIN]["plan"].to_attributes()
    assert not any(plan["estimated"])
    assert plan["starts"][-1] == STARTS[95].isoformat()


class RecordingConfig(dict):
    """A config store that records the keys read from it."""

    def __init__(self, *args, **kwargs):
        """Create the store with no keys read."""
        super().__init__(*args, **kwargs)
        self.read = set()

    def get(self, key, default=None):
        """Record and return a key."""
        self.read.add(key)
        return super().get(key, default)


async def test_plan_settings_replan(mock_hass):
    """Test that every setting with an entity the plan reads recalculates it."""
    STATES["sensor.charger_status"] = "connected_charging"
    config = RecordingConfig(
        mock_hass.data[DOMAIN]["config"],
        ev_charger_status_sensor="sensor.charger_status",
    )
    mock_hass.data[DOMAIN]["config"] = config
    forecast_data(mock_hass, ev_need_kwh=20.0)
    mock_hass.data[DOMAIN]["values"] = {}
    try:
        await run(mock_hass)
        battery_plan.current_action(mock_hass)
    finally:
        del STATES["sensor.charger_status"]
    assert "heat_pump_margin" in config.read
    # The action follows the switch through its own listener (__init__.py)
    assert "enabled" in config.read
    missing = {
        key
        for key in config.read - {"enabled"}
        if settings.SETTINGS[key].platform and not settings.SETTINGS[key].replans
    }
    assert missing == set()
