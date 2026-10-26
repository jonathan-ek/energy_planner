"""Tests for moving stored data from older versions."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.energy_planner import migrate
from custom_components.energy_planner.const import DOMAIN


@pytest.mark.parametrize(
    ("state", "enabled"), [("off", False), ("dynamic", True), ("basic", True)]
)
def test_planner_state_becomes_the_switch(state, enabled):
    """Test that only a planner that was off stays off."""
    config = {"planner_state": state}
    assert migrate.migrate_config(config)
    assert config == {"enabled": enabled}


def test_efficiency_is_kept_and_old_settings_dropped():
    """Test the renamed efficiency and the removed planners' settings."""
    config = {
        "price_peak_efficiency_factor": 91,
        "basic_nr_of_charge_hours": 4,
        "earliest_charge_time": "22:00:00",
        "battery_capacity": 25600,
    }
    migrate.migrate_config(config)
    assert config == {"battery_efficiency": 91, "battery_capacity": 25600}


def test_migrated_twice_changes_nothing():
    """Test that a store already migrated keeps its values."""
    config = {"enabled": False, "battery_efficiency": 94}
    assert not migrate.migrate_config(config)
    assert config == {"enabled": False, "battery_efficiency": 94}
    values = {"nordpool_values": {}}
    assert not migrate.migrate_values(values)


def test_slots_leave_the_values_store():
    """Test that the schedule's slots are dropped and the price cache kept."""
    values = {
        "slot_1_state": "auto",
        "slot_1_date_time_start": "2026-10-20T15:00:00+00:00",
        "nordpool_values": {"SE3": []},
    }
    assert migrate.migrate_values(values)
    assert values == {"nordpool_values": {"SE3": []}}


def test_removed_entities_leave_the_registry():
    """Test that the old entities are removed and others are not looked up."""
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda platform, domain, unique_id: (
        f"{platform}.{unique_id}" if unique_id == f"{DOMAIN}_planner_state" else None
    )
    with patch.object(migrate.er, "async_get", return_value=registry):
        migrate.remove_entities(MagicMock())
    registry.async_remove.assert_called_once_with(f"select.{DOMAIN}_planner_state")
    looked_up = {c.args[2] for c in registry.async_get_entity_id.call_args_list}
    assert f"{DOMAIN}_slot_49_active" in looked_up
