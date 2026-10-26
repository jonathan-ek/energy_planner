"""Tests for the manual slot services in __init__.py."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.energy_planner as integration
from custom_components.energy_planner.const import DOMAIN
from custom_components.energy_planner.planner import manual

NOW = dt.datetime(2026, 3, 12, 0, 0, tzinfo=dt.UTC)


@pytest.fixture
def planner_hass():
    """Return a hass with no manual slots, its saves and plan requests recorded."""
    hass = MagicMock()
    hass.data = {DOMAIN: {"config": {}, "manual_slots": [], "save": AsyncMock()}}
    with (
        patch.object(integration, "async_request_plan", AsyncMock()) as request,
        patch.object(integration, "write_plan_sensors") as write,
        patch.object(integration.dt_utils, "now", return_value=NOW),
    ):
        hass.request, hass.write = request, write
        yield hass


async def test_add_rounds_to_quarters_and_replans(planner_hass):
    """Test that a slot is rounded out, stored, shown and planned around."""
    slot = await integration.add_manual_slot(
        planner_hass,
        {
            "start": "2026-03-12T02:05:00+00:00",
            "end": "2026-03-12T03:10:00+00:00",
            "state": "charge",
            "soc": 80,
        },
    )
    assert (slot.start, slot.end, slot.soc) == (
        NOW + dt.timedelta(hours=2),
        NOW + dt.timedelta(hours=3, minutes=15),
        80.0,
    )
    assert planner_hass.data[DOMAIN]["manual_slots"] == [slot]
    planner_hass.data[DOMAIN]["save"].assert_awaited_once()
    planner_hass.write.assert_called_once()
    planner_hass.request.assert_awaited_once()


@pytest.mark.parametrize(
    "data",
    [
        {"start": "2026-03-12T02:00:00+00:00", "end": "2026-03-12T03:00:00+00:00"},
        {
            "start": "2026-03-12T02:00:00+00:00",
            "end": "2026-03-12T03:00:00+00:00",
            "state": "off",
        },
        {
            "start": "2026-03-11T20:00:00+00:00",
            "end": "2026-03-11T21:00:00+00:00",
            "state": "charge",
        },
        {"start": "soon", "end": "2026-03-12T03:00:00+00:00", "state": "charge"},
    ],
)
async def test_add_refuses_invalid_slots(planner_hass, data):
    """Test a missing or unknown state, an ended slot and an unreadable time."""
    with pytest.raises((KeyError, ValueError)):
        await integration.add_manual_slot(planner_hass, data)
    assert planner_hass.data[DOMAIN]["manual_slots"] == []


async def test_remove_and_clear(planner_hass):
    """Test removing one slot by its start, and clearing them all."""
    first = manual.make(NOW, NOW + dt.timedelta(hours=1), "charge")
    second = manual.make(
        NOW + dt.timedelta(hours=2), NOW + dt.timedelta(hours=3), "sell"
    )
    planner_hass.data[DOMAIN]["manual_slots"] = [first, second]
    await integration.remove_manual_slot(planner_hass, "2026-03-12T02:10:00+00:00")
    assert planner_hass.data[DOMAIN]["manual_slots"] == [first]
    with pytest.raises(ValueError):
        await integration.remove_manual_slot(planner_hass, "2026-03-12T05:00:00+00:00")
    await integration.clear_manual_slots(planner_hass)
    assert planner_hass.data[DOMAIN]["manual_slots"] == []
    assert planner_hass.request.await_count == 2


def test_stored_slots_load_in_whole_quarters():
    """Test that stored slots are parsed, rounded and the ended or invalid dropped."""
    stored = [
        {
            "start": "2026-03-12T02:05:00+00:00",
            "end": "2026-03-12T03:00:00+00:00",
            "state": "pause",
            "soc": None,
        },
        {
            "start": "2026-03-11T02:00:00+00:00",
            "end": "2026-03-11T03:00:00+00:00",
            "state": "pause",
            "soc": None,
        },
        {
            "start": "2026-03-12T02:00:00+00:00",
            "end": "2026-03-12T03:00:00+00:00",
            "state": "off",
            "soc": None,
        },
    ]
    with patch.object(integration.dt_utils, "now", return_value=NOW):
        slots = integration._load_manual_slots(stored)
    assert slots == [
        manual.ManualSlot(
            NOW + dt.timedelta(hours=2), NOW + dt.timedelta(hours=3), "pause", None
        )
    ]
