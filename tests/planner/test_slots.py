"""Tests for the slot schedule in the hour repeated when DST ends (2026-10-25)."""

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant

from custom_components.energy_planner.const import DOMAIN, SLOT_COUNT
from custom_components.energy_planner.planner import battery_action
from custom_components.energy_planner.planner.utils import (
    clear_passed_slots,
    write_schedule,
)

TZ = ZoneInfo("Europe/Stockholm")
MIDNIGHT = dt.datetime(2026, 10, 25, tzinfo=TZ)  # 22:00 UTC the day before
# 02:30 local twice: CEST (00:30 UTC), then CET (01:30 UTC)
FIRST = dt.datetime(2026, 10, 25, 2, 30, tzinfo=TZ)
SECOND = FIRST.replace(fold=1)


def utc(day, hour, minute=0):
    """Return a UTC time in October 2026."""
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=dt.UTC)


def _hass() -> MagicMock:
    hass = MagicMock(spec=HomeAssistant)
    hass.states = MagicMock()
    values: dict[str, Any] = {}
    for i in range(1, SLOT_COUNT + 1):
        values[f"slot_{i}_date_time_start"] = None
        values[f"slot_{i}_state"] = "off"
        values[f"slot_{i}_soc"] = 50
        values[f"slot_{i}_active"] = False
    hass.data = {
        DOMAIN: {
            "config": {
                "nordpool_entity_id": "sensor.nordpool_kwh_se3_sek_3_10_025",
                "battery_max_soc": 90,
                "battery_shutdown_soc": 20,
            },
            "values": values,
            "manual_slots": [],
            "save": AsyncMock(),
        }
    }
    return hass


def _starts(hass) -> list[dt.datetime | None]:
    values = hass.data[DOMAIN]["values"]
    return [values[f"slot_{i}_date_time_start"] for i in range(1, 5)]


async def test_a_slot_in_the_second_copy_has_not_started_in_the_first():
    """Test that slot 1 stays until the slot after it has really started."""
    hass = _hass()
    write_schedule(
        hass, [{"start": MIDNIGHT, "end": SECOND, "state": "auto", "soc": 50}]
    )
    assert _starts(hass)[:2] == [utc(24, 22), utc(25, 1, 30)]
    update = "custom_components.energy_planner.planner.utils.update_entities"
    now = FIRST.replace(minute=40)
    with patch(update), patch("homeassistant.util.dt.now", return_value=now):
        # 02:40 CEST is after 02:30 on the wall clock, but before 02:30 CET
        await clear_passed_slots(hass)
    assert _starts(hass)[:2] == [utc(24, 22), utc(25, 1, 30)]
    later = now.replace(fold=1)  # 02:40 CET
    with patch(update), patch("homeassistant.util.dt.now", return_value=later):
        await clear_passed_slots(hass)
    assert _starts(hass)[0] == utc(25, 1, 30)


def test_battery_current_in_the_repeated_hour():
    """Test the time left to the end of a run across the repeated hour."""
    battery = battery_action.BatteryState(50.0, 500.0, 10.0, 20.0, 90.0, 100.0, 100.0)
    # 02:20 CEST to 02:00 CET is 40 min, though the wall clock goes back
    now = dt.datetime(2026, 10, 25, 2, 20, tzinfo=TZ)
    until = dt.datetime(2026, 10, 25, 2, 0, tzinfo=TZ, fold=1)
    # 2 kWh in 40 min at 500 V: 6 A
    assert battery_action._current(2.0, until, now, battery, 100) == 6


def test_slot_until_is_published_in_local_time():
    """Test that a slot's end, kept in UTC, is published like the plan's times."""
    battery = battery_action.BatteryState(50.0, 500.0, 10.0, 20.0, 90.0, 100.0, 100.0)
    slot = battery_action.Slot("charge", True, utc(24, 22), utc(25, 1, 30), 90)
    result = battery_action.resolve(slot, None, FIRST, battery)
    assert (result["source"], result["until"]) == ("slot", SECOND.isoformat())
