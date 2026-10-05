"""Tests for basic_planner module."""

import logging
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_utils
from zoneinfo import ZoneInfo

from custom_components.energy_planner.planner.basic_planner import plan_day, planner
from custom_components.energy_planner import parse_stored_data
from custom_components.energy_planner.const import DOMAIN, SLOT_COUNT

_LOGGER = logging.getLogger(__name__)


@pytest.fixture
def mock_hass():
    """Create mock Home Assistant instance."""
    hass = MagicMock(spec=HomeAssistant)
    hass.states = MagicMock()
    hass.data = {
        DOMAIN: {
            "config": {
                "nordpool_entity_id": "sensor.nordpool_kwh_se3_sek_3_10_025",
                "battery_max_soc": 100,
                "battery_shutdown_soc": 20,
                "earliest_charge_time": dt.time(0, 0),
                "earliest_discharge_time": dt.time(16, 0),
                "basic_nr_of_charge_hours": 4,
                "basic_nr_of_discharge_hours": 3,
            },
            "values": {},
            "save": AsyncMock(),
        }
    }
    # Initialize slots as after a reset
    for i in range(1, SLOT_COUNT + 1):
        hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"] = None
        hass.data[DOMAIN]["values"][f"slot_{i}_state"] = "off"
        hass.data[DOMAIN]["values"][f"slot_{i}_soc"] = 50
        hass.data[DOMAIN]["values"][f"slot_{i}_active"] = False
    return hass


@pytest.fixture(autouse=True)
def fixed_now():
    """Freeze time at the start of the day covered by mock_nordpool_data."""
    now = dt.datetime(2026, 3, 12, 0, 0, 0, tzinfo=ZoneInfo("Europe/Stockholm"))
    with patch("homeassistant.util.dt.now", return_value=now):
        yield now


@pytest.fixture
def mock_nordpool_data():
    """Create mock Nordpool data for testing."""
    zone = ZoneInfo("Europe/Stockholm")
    base_time = dt.datetime(2026, 3, 12, 0, 0, 0, tzinfo=zone)

    # Create 24 hours of data with varying prices
    data = []
    prices = [
        # Night hours (0-5): cheap
        50,
        45,
        40,
        35,
        30,
        35,
        # Morning (6-11): moderate
        60,
        70,
        75,
        80,
        70,
        65,
        # Afternoon (12-15): moderate
        55,
        60,
        65,
        70,
        # Peak hours (16-19): expensive
        120,
        130,
        125,
        115,
        # Evening (20-23): moderate to cheap
        80,
        70,
        60,
        55,
    ]

    for i, price in enumerate(prices):
        start = base_time + dt.timedelta(hours=i)
        for quarter in range(4):
            data.append(
                {
                    "start": start + dt.timedelta(minutes=quarter * 15),
                    "end": start + dt.timedelta(minutes=(quarter + 1) * 15),
                    "value": price,
                }
            )

    return data


class TestPlanDay:
    """Tests for plan_day function."""

    @pytest.mark.asyncio
    async def test_plan_day_basic(self, mock_hass, mock_nordpool_data):
        """Test basic planning functionality."""
        zone = ZoneInfo("Europe/Stockholm")
        earliest_charge = dt.datetime(2026, 3, 12, 0, 0, 0, tzinfo=zone)
        earliest_discharge = dt.datetime(2026, 3, 12, 16, 0, 0, tzinfo=zone)

        config = {
            "earliest_charge": earliest_charge,
            "earliest_discharge": earliest_discharge,
            "nr_of_charge_hours": 4,
            "nr_of_discharge_hours": 3,
        }

        await plan_day(mock_hass, mock_nordpool_data, config)

        # Verify that some slots were created
        has_charge_slot = False
        has_discharge_slot = False
        has_pause_slot = False

        for i in range(1, SLOT_COUNT + 1):
            state = mock_hass.data[DOMAIN]["values"][f"slot_{i}_state"]
            if state == "charge":
                has_charge_slot = True
            elif state == "discharge":
                has_discharge_slot = True
            elif state == "pause":
                has_pause_slot = True
            elif state == "off":
                break

        assert has_charge_slot, "Should have at least one charge slot"
        assert has_discharge_slot, "Should have at least one discharge slot"
        assert has_pause_slot, "Should have at least one pause slot"

    @pytest.mark.asyncio
    async def test_plan_day_charge_slots_in_correct_period(
        self, mock_hass, mock_nordpool_data
    ):
        """Test that charge slots are only created in the charge period."""
        zone = ZoneInfo("Europe/Stockholm")
        earliest_charge = dt.datetime(2026, 3, 12, 0, 0, 0, tzinfo=zone)
        earliest_discharge = dt.datetime(2026, 3, 12, 16, 0, 0, tzinfo=zone)

        config = {
            "earliest_charge": earliest_charge,
            "earliest_discharge": earliest_discharge,
            "nr_of_charge_hours": 4,
            "nr_of_discharge_hours": 3,
        }

        await plan_day(mock_hass, mock_nordpool_data, config)

        # Check that all charge slots are before discharge time
        for i in range(1, SLOT_COUNT + 1):
            state = mock_hass.data[DOMAIN]["values"][f"slot_{i}_state"]
            start_time = mock_hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"]

            if state == "charge" and start_time:
                assert start_time < earliest_discharge, (
                    f"Charge slot {i} at {start_time} should be before discharge time"
                )
            elif state == "off":
                break

    @pytest.mark.asyncio
    async def test_plan_day_discharge_slots_in_correct_period(
        self, mock_hass, mock_nordpool_data
    ):
        """Test that discharge slots are only created in the discharge period."""
        zone = ZoneInfo("Europe/Stockholm")
        earliest_charge = dt.datetime(2026, 3, 12, 0, 0, 0, tzinfo=zone)
        earliest_discharge = dt.datetime(2026, 3, 12, 16, 0, 0, tzinfo=zone)

        config = {
            "earliest_charge": earliest_charge,
            "earliest_discharge": earliest_discharge,
            "nr_of_charge_hours": 4,
            "nr_of_discharge_hours": 3,
        }

        await plan_day(mock_hass, mock_nordpool_data, config)

        # Check that all discharge slots are after discharge time
        for i in range(1, SLOT_COUNT + 1):
            state = mock_hass.data[DOMAIN]["values"][f"slot_{i}_state"]
            start_time = mock_hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"]

            if state == "discharge" and start_time:
                assert start_time >= earliest_discharge, (
                    f"Discharge slot {i} at {start_time} should be in discharge period"
                )
            elif state == "off":
                break

    @pytest.mark.asyncio
    async def test_plan_day_soc_values(self, mock_hass, mock_nordpool_data):
        """Test that SOC values are set correctly."""
        zone = ZoneInfo("Europe/Stockholm")
        earliest_charge = dt.datetime(2026, 3, 12, 0, 0, 0, tzinfo=zone)
        earliest_discharge = dt.datetime(2026, 3, 12, 16, 0, 0, tzinfo=zone)

        config = {
            "earliest_charge": earliest_charge,
            "earliest_discharge": earliest_discharge,
            "nr_of_charge_hours": 4,
            "nr_of_discharge_hours": 3,
        }

        await plan_day(mock_hass, mock_nordpool_data, config)

        max_soc = mock_hass.data[DOMAIN]["config"]["battery_max_soc"]
        min_soc = mock_hass.data[DOMAIN]["config"]["battery_shutdown_soc"]

        # Check SOC values
        for i in range(1, SLOT_COUNT + 1):
            state = mock_hass.data[DOMAIN]["values"][f"slot_{i}_state"]
            soc = mock_hass.data[DOMAIN]["values"][f"slot_{i}_soc"]

            if state == "charge" or state == "pause":
                assert soc == max_soc, (
                    f"Charge/pause slot {i} should have max SOC {max_soc}"
                )
            elif state == "discharge":
                assert soc == min_soc, (
                    f"Discharge slot {i} should have min SOC {min_soc}"
                )
            elif state == "off":
                break

    @pytest.mark.asyncio
    async def test_plan_day_removes_past_hours(self, mock_hass):
        """Test that past hours are removed from the schedule."""
        now = dt_utils.now()

        # Create data where some hours are in the past
        data = []
        for i in range(-2, 22):  # -2 to +22 hours from now
            start = now + dt.timedelta(hours=i)
            for quarter in range(4):
                data.append(
                    {
                        "start": start + dt.timedelta(minutes=quarter * 15),
                        "end": start + dt.timedelta(minutes=(quarter + 1) * 15),
                        "value": 50 + i,
                    }
                )

        earliest_charge = (now - dt.timedelta(hours=3)).replace(
            minute=0, second=0, microsecond=0
        )
        earliest_discharge = (now + dt.timedelta(hours=12)).replace(
            minute=0, second=0, microsecond=0
        )

        config = {
            "earliest_charge": earliest_charge,
            "earliest_discharge": earliest_discharge,
            "nr_of_charge_hours": 4,
            "nr_of_discharge_hours": 3,
        }

        await plan_day(mock_hass, data, config)

        # Verify no slot has already ended, a slot ends where the next one starts
        assert mock_hass.data[DOMAIN]["values"]["slot_1_state"] != "off"
        for i in range(1, SLOT_COUNT):
            state = mock_hass.data[DOMAIN]["values"][f"slot_{i}_state"]
            if state == "off":
                break
            end_time = mock_hass.data[DOMAIN]["values"][f"slot_{i + 1}_date_time_start"]
            assert end_time > now, (
                f"Slot {i} ending at {end_time} should not have passed"
            )

    @pytest.mark.asyncio
    async def test_plan_day_empty_data(self, mock_hass):
        """Test planning with empty data."""
        zone = ZoneInfo("Europe/Stockholm")
        earliest_charge = dt.datetime(2026, 3, 12, 0, 0, 0, tzinfo=zone)
        earliest_discharge = dt.datetime(2026, 3, 12, 16, 0, 0, tzinfo=zone)

        config = {
            "earliest_charge": earliest_charge,
            "earliest_discharge": earliest_discharge,
            "nr_of_charge_hours": 4,
            "nr_of_discharge_hours": 3,
        }

        await plan_day(mock_hass, [], config)

        # Should handle empty data gracefully - slot 1 should remain off
        assert mock_hass.data[DOMAIN]["values"]["slot_1_state"] == "off"

    @pytest.mark.asyncio
    async def test_plan_day_zero_charge_hours(self, mock_hass, mock_nordpool_data):
        """Test planning with zero charge hours requested."""
        zone = ZoneInfo("Europe/Stockholm")
        earliest_charge = dt.datetime(2026, 3, 12, 0, 0, 0, tzinfo=zone)
        earliest_discharge = dt.datetime(2026, 3, 12, 16, 0, 0, tzinfo=zone)

        config = {
            "earliest_charge": earliest_charge,
            "earliest_discharge": earliest_discharge,
            "nr_of_charge_hours": 0,
            "nr_of_discharge_hours": 3,
        }

        await plan_day(mock_hass, mock_nordpool_data, config)

        # Should have no charge slots
        has_charge_slot = False
        for i in range(1, SLOT_COUNT + 1):
            state = mock_hass.data[DOMAIN]["values"][f"slot_{i}_state"]
            if state == "charge":
                has_charge_slot = True
            elif state == "off":
                break

        assert not has_charge_slot, (
            "Should have no charge slots when nr_of_charge_hours is 0"
        )

    @pytest.mark.asyncio
    async def test_plan_day_slots_are_chronological(
        self, mock_hass, mock_nordpool_data
    ):
        """Test that slots are in chronological order."""
        zone = ZoneInfo("Europe/Stockholm")
        earliest_charge = dt.datetime(2026, 3, 12, 0, 0, 0, tzinfo=zone)
        earliest_discharge = dt.datetime(2026, 3, 12, 16, 0, 0, tzinfo=zone)

        config = {
            "earliest_charge": earliest_charge,
            "earliest_discharge": earliest_discharge,
            "nr_of_charge_hours": 4,
            "nr_of_discharge_hours": 3,
        }

        await plan_day(mock_hass, mock_nordpool_data, config)

        # Verify chronological order
        prev_time = None
        for i in range(1, SLOT_COUNT + 1):
            start_time = mock_hass.data[DOMAIN]["values"][f"slot_{i}_date_time_start"]
            state = mock_hass.data[DOMAIN]["values"][f"slot_{i}_state"]

            if state != "off" and start_time:
                if prev_time is not None:
                    assert start_time >= prev_time, (
                        f"Slot {i} at {start_time} should be after {prev_time}"
                    )
                prev_time = start_time
            elif state == "off":
                break


class TestPlanner:
    """Tests for planner function."""

    @pytest.mark.asyncio
    async def test_planner_missing_nordpool_entity_id(self, mock_hass):
        """Test planner raises error when nordpool entity ID is not set."""
        mock_hass.data[DOMAIN]["config"]["nordpool_entity_id"] = None

        with pytest.raises(ValueError, match="Nordpool entity not set"):
            await planner(mock_hass)

    @pytest.mark.asyncio
    async def test_planner_nordpool_entity_not_found(self, mock_hass):
        """Test planner raises error when nordpool entity is not found."""
        mock_hass.states.get.return_value = None

        with pytest.raises(ValueError, match="Nordpool entity not found"):
            await planner(mock_hass)

    @pytest.mark.asyncio
    @patch("custom_components.energy_planner.planner.basic_planner.fetch_nordpool_data")
    @patch("custom_components.energy_planner.planner.basic_planner.update_entities")
    @patch("custom_components.energy_planner.planner.basic_planner.reset")
    @patch("custom_components.energy_planner.planner.basic_planner.store_disable_state")
    @patch(
        "custom_components.energy_planner.planner.basic_planner.restore_disable_state"
    )
    @patch("custom_components.energy_planner.planner.basic_planner.add_manual_slots")
    async def test_planner_success(
        self,
        mock_add_manual_slots,
        mock_restore_disable_state,
        mock_store_disable_state,
        mock_reset,
        mock_update_entities,
        mock_fetch_nordpool,
        mock_hass,
        mock_nordpool_data,
    ):
        """Test successful planner execution."""
        # Setup mocks
        mock_nordpool_state = MagicMock()
        mock_nordpool_state.attributes = {"tomorrow_valid": False}
        mock_hass.states.get.return_value = mock_nordpool_state

        # Create yesterday, today data (no tomorrow)
        zone = ZoneInfo("Europe/Stockholm")
        yesterday = []
        today = mock_nordpool_data

        mock_fetch_nordpool.return_value = (yesterday, today, None)
        mock_reset.return_value = AsyncMock()
        mock_update_entities.return_value = AsyncMock()
        mock_store_disable_state.return_value = AsyncMock()
        mock_restore_disable_state.return_value = AsyncMock()
        mock_add_manual_slots.return_value = AsyncMock()

        # Mock async_get_time_zone
        with patch("homeassistant.util.dt.async_get_time_zone", return_value=zone):
            await planner(mock_hass)

        # Verify the workflow was executed
        mock_store_disable_state.assert_called_once()
        mock_reset.assert_called_once()
        mock_add_manual_slots.assert_called_once()
        mock_restore_disable_state.assert_called_once()
        mock_update_entities.assert_called_once()
        mock_hass.data[DOMAIN]["save"].assert_called_once()

    @pytest.mark.asyncio
    @patch("custom_components.energy_planner.planner.basic_planner.fetch_nordpool_data")
    async def test_planner_missing_nordpool_data(self, mock_fetch_nordpool, mock_hass):
        """Test planner raises error when nordpool data is missing."""
        mock_nordpool_state = MagicMock()
        mock_nordpool_state.attributes = {"tomorrow_valid": False}
        mock_hass.states.get.return_value = mock_nordpool_state

        # Return None for today data
        mock_fetch_nordpool.return_value = (None, None, None)

        with pytest.raises(ValueError, match="Nordpool data not found"):
            await planner(mock_hass)

    @pytest.mark.asyncio
    @patch("custom_components.energy_planner.planner.basic_planner.fetch_nordpool_data")
    @patch("custom_components.energy_planner.planner.basic_planner.update_entities")
    @patch("custom_components.energy_planner.planner.basic_planner.reset")
    @patch("custom_components.energy_planner.planner.basic_planner.store_disable_state")
    @patch(
        "custom_components.energy_planner.planner.basic_planner.restore_disable_state"
    )
    @patch("custom_components.energy_planner.planner.basic_planner.add_manual_slots")
    @patch("custom_components.energy_planner.planner.basic_planner.plan_day")
    async def test_planner_with_tomorrow_data(
        self,
        mock_plan_day,
        mock_add_manual_slots,
        mock_restore_disable_state,
        mock_store_disable_state,
        mock_reset,
        mock_update_entities,
        mock_fetch_nordpool,
        mock_hass,
        mock_nordpool_data,
    ):
        """Test planner with tomorrow data available."""
        # Setup mocks
        mock_nordpool_state = MagicMock()
        mock_nordpool_state.attributes = {"tomorrow_valid": True}
        mock_hass.states.get.return_value = mock_nordpool_state

        zone = ZoneInfo("Europe/Stockholm")
        yesterday = []
        today = mock_nordpool_data
        tomorrow = mock_nordpool_data  # Simplified for test

        mock_fetch_nordpool.return_value = (yesterday, today, tomorrow)
        mock_reset.return_value = AsyncMock()
        mock_update_entities.return_value = AsyncMock()
        mock_store_disable_state.return_value = AsyncMock()
        mock_restore_disable_state.return_value = AsyncMock()
        mock_add_manual_slots.return_value = AsyncMock()
        mock_plan_day.return_value = AsyncMock()

        # Mock async_get_time_zone
        with patch("homeassistant.util.dt.async_get_time_zone", return_value=zone):
            await planner(mock_hass)

        # plan_day should be called twice (today and tomorrow)
        assert mock_plan_day.call_count == 2

    def test_stored_strings_are_parsed(self, mock_hass):
        """Test that dates and times loaded from storage are parsed to objects."""
        mock_hass.data[DOMAIN]["config"]["earliest_charge_time"] = "00:00:00"
        mock_hass.data[DOMAIN]["config"]["earliest_discharge_time"] = "16:00:00"
        mock_hass.data[DOMAIN]["values"]["slot_1_date_time_start"] = (
            "2026-03-12T00:00:00+01:00"
        )
        mock_hass.data[DOMAIN]["manual_slots"] = [
            {"start": "2026-03-12T01:00:00+01:00", "end": "2026-03-12T02:00:00+01:00"}
        ]

        parse_stored_data(mock_hass)

        config = mock_hass.data[DOMAIN]["config"]
        assert config["earliest_charge_time"] == dt.time(0, 0)
        assert config["earliest_discharge_time"] == dt.time(16, 0)
        slot_start = mock_hass.data[DOMAIN]["values"]["slot_1_date_time_start"]
        assert slot_start == dt.datetime(
            2026, 3, 12, 0, 0, tzinfo=ZoneInfo("Europe/Stockholm")
        )
        manual_slot = mock_hass.data[DOMAIN]["manual_slots"][0]
        assert isinstance(manual_slot["start"], dt.datetime)
        assert isinstance(manual_slot["end"], dt.datetime)

    @pytest.mark.asyncio
    @patch("custom_components.energy_planner.planner.basic_planner.fetch_nordpool_data")
    @patch("custom_components.energy_planner.planner.basic_planner.update_entities")
    @patch("custom_components.energy_planner.planner.basic_planner.reset")
    @patch("custom_components.energy_planner.planner.basic_planner.store_disable_state")
    @patch(
        "custom_components.energy_planner.planner.basic_planner.restore_disable_state"
    )
    @patch("custom_components.energy_planner.planner.basic_planner.add_manual_slots")
    async def test_planner_different_areas(
        self,
        mock_add_manual_slots,
        mock_restore_disable_state,
        mock_store_disable_state,
        mock_reset,
        mock_update_entities,
        mock_fetch_nordpool,
        mock_hass,
        mock_nordpool_data,
    ):
        """Test planner with different Nordic areas."""
        for area in ["NO1", "DK1", "FI", "SE1"]:
            # Update entity ID for different area
            mock_hass.data[DOMAIN]["config"]["nordpool_entity_id"] = (
                f"sensor.nordpool_kwh_{area.lower()}_nok_3_10_025"
            )

            mock_nordpool_state = MagicMock()
            mock_nordpool_state.attributes = {"tomorrow_valid": False}
            mock_hass.states.get.return_value = mock_nordpool_state

            zone_map = {
                "NO1": "Europe/Oslo",
                "DK1": "Europe/Copenhagen",
                "FI": "Europe/Helsinki",
                "SE1": "Europe/Stockholm",
            }
            zone = ZoneInfo(zone_map[area])

            yesterday = []
            today = mock_nordpool_data

            mock_fetch_nordpool.return_value = (yesterday, today, None)
            mock_reset.return_value = AsyncMock()
            mock_update_entities.return_value = AsyncMock()
            mock_store_disable_state.return_value = AsyncMock()
            mock_restore_disable_state.return_value = AsyncMock()
            mock_add_manual_slots.return_value = AsyncMock()

            # Mock async_get_time_zone
            with patch("homeassistant.util.dt.async_get_time_zone", return_value=zone):
                await planner(mock_hass)

            # Should complete without errors for each area
            assert mock_hass.data[DOMAIN]["save"].called


class TestPlannerIntegration:
    """Integration tests for the planner."""

    @pytest.mark.asyncio
    async def test_full_planning_cycle(self, mock_hass, mock_nordpool_data):
        """Test a full planning cycle from start to finish."""
        zone = ZoneInfo("Europe/Stockholm")
        earliest_charge = dt.datetime(2026, 3, 12, 0, 0, 0, tzinfo=zone)
        earliest_discharge = dt.datetime(2026, 3, 12, 16, 0, 0, tzinfo=zone)

        config = {
            "earliest_charge": earliest_charge,
            "earliest_discharge": earliest_discharge,
            "nr_of_charge_hours": 4,
            "nr_of_discharge_hours": 3,
        }

        # Run the planner
        await plan_day(mock_hass, mock_nordpool_data, config)

        # Collect all non-off slots
        slots = []
        for i in range(1, SLOT_COUNT + 1):
            state = mock_hass.data[DOMAIN]["values"][f"slot_{i}_state"]
            if state == "off":
                break
            slots.append(
                {
                    "index": i,
                    "start": mock_hass.data[DOMAIN]["values"][
                        f"slot_{i}_date_time_start"
                    ],
                    "state": state,
                    "soc": mock_hass.data[DOMAIN]["values"][f"slot_{i}_soc"],
                    "active": mock_hass.data[DOMAIN]["values"][f"slot_{i}_active"],
                }
            )

        # Verify we have slots
        assert len(slots) > 0, "Should have created at least one slot"

        # Verify slot transitions are valid
        for i in range(len(slots) - 1):
            current = slots[i]
            next_slot = slots[i + 1]

            # Times should be in order
            assert current["start"] < next_slot["start"], (
                f"Slot {current['index']} should be before slot {next_slot['index']}"
            )

        # Verify we have expected number of states
        charge_count = sum(1 for s in slots if s["state"] == "charge")
        discharge_count = sum(1 for s in slots if s["state"] == "discharge")

        assert charge_count > 0, "Should have at least one charge slot"
        assert discharge_count > 0, "Should have at least one discharge slot"
