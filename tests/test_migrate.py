"""Tests for moving stored data from older versions."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.energy_planner import migrate
from custom_components.energy_planner.const import DOMAIN


@pytest.mark.parametrize(
    ("state", "enabled", "replan"),
    [("off", False, False), ("dynamic", True, False), ("basic", True, True)],
)
def test_planner_state_becomes_the_switch(state, enabled, replan):
    """Test that only a planner that was off stays off.

    A rule-based planner's slots must be replaced.
    """
    config = {"planner_state": state}
    assert migrate.migrate_config(config) is replan
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
    migrate.migrate_config(config)
    assert config == {"enabled": False, "battery_efficiency": 94}


def test_removed_entities_leave_the_registry():
    """Test that the old entities are removed and others are not looked up."""
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda platform, domain, unique_id: (
        f"{platform}.{unique_id}" if unique_id == f"{DOMAIN}_planner_state" else None
    )
    with patch.object(migrate.er, "async_get", return_value=registry):
        migrate.remove_entities(MagicMock())
    registry.async_remove.assert_called_once_with(f"select.{DOMAIN}_planner_state")
