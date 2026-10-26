"""Tests for the datetime entities' state."""

import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_utils

from custom_components.energy_planner.const import DOMAIN
from custom_components.energy_planner.datetime import EnergyPlannerDateTimeEntity

VALUE = datetime.datetime(2026, 10, 15, 20, 0, tzinfo=dt_utils.UTC)


def _entity(definition):
    hass = MagicMock()
    hass.data = {DOMAIN: {"config": {}, "values": {}}}
    entity = EnergyPlannerDateTimeEntity(hass, definition)
    entity._attr_native_value = VALUE
    return entity


def test_departure_state_is_iso():
    """The frontend can only show and edit an ISO state."""
    entity = _entity({"id": "ev_departure", "name": "Car departure"})
    assert entity._stringify_state(True) == "2026-10-15T20:00:00+00:00"
    entity._attr_native_value = None
    assert entity._stringify_state(True) == "unknown"


async def test_set_value_is_kept_in_utc():
    """HA passes a set time in its own time zone; the departure is kept in UTC."""
    entity = _entity(
        {"id": "ev_departure", "name": "Car departure", "data_store": "config"}
    )
    entity._hass.data[DOMAIN]["save"] = AsyncMock()
    entity.schedule_update_ha_state = MagicMock()
    local = datetime.datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Stockholm"))
    await entity.async_set_value(local.replace(fold=1))
    stored = entity._hass.data[DOMAIN]["config"]["ev_departure"]
    assert stored.tzinfo == dt_utils.UTC
    assert stored.isoformat() == "2026-10-25T01:30:00+00:00"
