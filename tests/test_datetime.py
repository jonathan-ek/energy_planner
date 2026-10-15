"""Tests for the datetime entities' state."""

import datetime
from unittest.mock import MagicMock

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


def test_slot_state_is_short():
    """The older slot scripts read "day/month HH:MM"."""
    entity = _entity(
        {"id": "slot_1_date_time_start", "name": "Slot 1 start", "short_state": True}
    )
    assert entity._stringify_state(True) == "15/10 20:00"
