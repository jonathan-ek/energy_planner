"""Tests for the services in __init__.py."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.energy_planner as integration
from custom_components.energy_planner.const import (
    DATE_TIME_ENTITIES,
    DOMAIN,
    NUMBER_ENTITIES,
    SELECT_ENTITIES,
    SWITCH_ENTITIES,
    TIME_ENTITIES,
)

START = dt.datetime(2026, 3, 11, 23, 0, tzinfo=dt.UTC)


def make_hass(planner_state):
    """Return a hass with one manual charge slot written into the schedule."""
    hass = MagicMock()
    values = {
        "slot_1_date_time_start": START,
        "slot_1_state": "charge",
        "slot_1_active": True,
        "slot_1_soc": 90,
        "slot_2_date_time_start": START + dt.timedelta(hours=1),
        "slot_2_state": "off",
        "slot_2_active": True,
        "slot_2_soc": 50,
    }
    hass.data = {
        DOMAIN: {
            "config": {"planner_state": planner_state},
            "values": values,
            "manual_slots": [
                {
                    "start": START,
                    "end": START + dt.timedelta(hours=1),
                    "state": "charge",
                    "soc": 90,
                }
            ],
            "save": AsyncMock(),
            **{
                platform: []
                for platform in (
                    DATE_TIME_ENTITIES,
                    TIME_ENTITIES,
                    SELECT_ENTITIES,
                    SWITCH_ENTITIES,
                    NUMBER_ENTITIES,
                )
            },
        }
    }
    return hass


async def test_clearing_manual_slots_with_the_planner_off_clears_the_schedule():
    """Test that a cleared manual slot no longer runs from the schedule."""
    hass = make_hass("off")
    with patch.object(integration, "async_request_plan", AsyncMock()) as request:
        await integration.clear_manual_slots(hass)
    data = hass.data[DOMAIN]
    assert data["manual_slots"] == []
    assert data["values"]["slot_1_state"] == "off"
    assert data["values"]["slot_1_date_time_start"] is None
    data["save"].assert_awaited()
    request.assert_awaited_once()


@pytest.mark.parametrize("planner_state", ["dynamic", "basic"])
async def test_clearing_manual_slots_rebuilds_the_schedule(planner_state):
    """Test that the planner rewrites the schedule without the manual slots."""
    hass = make_hass(planner_state)
    with (
        patch.object(integration, "async_request_plan", AsyncMock()),
        patch.object(integration, "run_planner", AsyncMock()) as run,
    ):
        await integration.clear_manual_slots(hass)
    assert hass.data[DOMAIN]["manual_slots"] == []
    run.assert_awaited_once_with(hass)
