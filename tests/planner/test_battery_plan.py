"""Tests for the battery plan glue."""

import datetime as dt
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.const import DOMAIN, PLAN_SENSORS

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
