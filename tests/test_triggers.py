"""Tests for the plan triggers in __init__.py."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import custom_components.energy_planner as integration
from custom_components.energy_planner.const import DOMAIN

from .planner.plans import make_plan

TZ = ZoneInfo("Europe/Stockholm")
START = dt.datetime(2026, 3, 12, 0, 0, tzinfo=TZ)
SOC_SENSOR = "sensor.solis_s6_solis_battery_soc"


async def test_soc_drift_replans_at_most_every_interval():
    """Test that a plan that stays old does not replan on every SOC change."""
    # Made an hour ago, at 50 %: the SOC is 30 % off
    hass = MagicMock()
    hass.data = {DOMAIN: {"config": {}, "plan": make_plan(START, size=8)}}
    hass.states.get = lambda _: SimpleNamespace(state="80", attributes={})
    callbacks = {}

    def track(_hass, entity_ids, callback):
        for entity_id in [entity_ids] if isinstance(entity_ids, str) else entity_ids:
            callbacks[entity_id] = callback
        return lambda: None

    request = AsyncMock()
    with (
        patch.object(integration, "async_track_state_change_event", track),
        patch.object(integration, "async_request_plan", request),
    ):
        integration._track_plan_triggers(hass, MagicMock())
        event = SimpleNamespace(data={"entity_id": SOC_SENSOR})
        for minutes in (60, 61, 62, 66):
            now = START + dt.timedelta(minutes=minutes)
            with patch.object(integration.dt_utils, "now", return_value=now):
                await callbacks[SOC_SENSOR](event)
    # 60: the plan is old; 61 and 62 within 5 min of that request; 66 again
    assert request.await_count == 2


async def test_unplugging_clears_the_entered_soc_not_an_unreachable_charger():
    """Test that only a real unplug ends the entered SOC's session."""
    hass = MagicMock()
    hass.data = {DOMAIN: {"config": {"ev_charger_status_sensor": "sensor.charger"}}}
    callbacks = {}

    def track(_hass, entity_ids, callback):
        for entity_id in [entity_ids] if isinstance(entity_ids, str) else entity_ids:
            callbacks[entity_id] = callback
        return lambda: None

    def change(old, new):
        return SimpleNamespace(
            data={
                "entity_id": "sensor.charger",
                "old_state": SimpleNamespace(state=old),
                "new_state": SimpleNamespace(state=new),
            }
        )

    unplugged = AsyncMock()
    with (
        patch.object(integration, "async_track_state_change_event", track),
        patch.object(integration, "async_request_plan", AsyncMock()) as request,
        patch.object(integration, "async_car_unplugged", unplugged),
    ):
        integration._track_plan_triggers(hass, MagicMock())
        await callbacks["sensor.charger"](change("connected_charging", "unavailable"))
        unplugged.assert_not_awaited()
        await callbacks["sensor.charger"](change("unavailable", "disconnected"))
        unplugged.assert_awaited_once()
    # Plugged -> unavailable already counted as unplugged for the plan
    assert request.await_count == 1
