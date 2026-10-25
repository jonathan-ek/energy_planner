"""Tests for the settings table, the typed groups and live readings."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.helpers.entity import Entity
import pytest

from custom_components.energy_planner import datetime as datetime_platform
from custom_components.energy_planner import number, select, time
from custom_components.energy_planner.const import DOMAIN
from custom_components.energy_planner.planner import settings

NOW = dt.datetime(2026, 3, 12, 12, 0, tzinfo=dt.UTC)


def hass_with(states: dict[str, SimpleNamespace]) -> MagicMock:
    """Create a mock hass with the given states."""
    hass = MagicMock()
    hass.states.get = states.get
    return hass


def state(value, unit=None, reported=NOW):
    """Create a state as hass.states.get returns it."""
    return SimpleNamespace(
        state=value,
        attributes={"unit_of_measurement": unit} if unit else {},
        last_reported=reported,
    )


def test_get_falls_back_to_the_default():
    """Test that a missing or None setting gives its default."""
    assert settings.get({}, "battery_reserve_soc") == 20
    assert settings.get({"battery_reserve_soc": None}, "battery_reserve_soc") == 20
    assert settings.get({"battery_reserve_soc": 0}, "battery_reserve_soc") == 0
    assert settings.number({"main_fuse": "35"}, "main_fuse") == 35.0


def test_unknown_key_is_an_error():
    """Test that every key read must be in the table."""
    with pytest.raises(KeyError):
        settings.get({}, "no_such_setting")


def test_battery_in_domain_units():
    """Test that the battery group converts Wh, % and öre."""
    battery = settings.battery(
        {
            "battery_capacity": 10000,
            "price_peak_efficiency_factor": 91,
            "battery_wear_cost": 25,
        }
    )
    assert battery.capacity_kwh == 10.0
    assert battery.efficiency == 0.91
    assert battery.wear_sek_per_kwh == 0.25
    assert battery.reserve_soc == 20.0
    assert battery.soc_sensor == "sensor.solis_s6_solis_battery_soc"


def test_car_only_with_a_charger_status():
    """Test that without a charger status sensor there is no car."""
    assert settings.car({}) is None
    car = settings.car(
        {"ev_charger_status_sensor": "sensor.garaget_laddstatus", "ev_soc_sensor": ""}
    )
    assert car is not None
    assert car.charger_max_kw == 11.0
    assert car.ready_time == dt.time(6, 30)
    assert car.soc_sensor is None


def test_fuse_only_with_a_fuse_and_phases():
    """Test that the fuse guard needs both a main fuse and phase sensors."""
    phases = {"grid_phase_import_sensors": ["sensor.l1", "sensor.l2", "sensor.l3"]}
    assert settings.fuse(phases) is None
    assert settings.fuse({"main_fuse": 35}) is None
    guard = settings.fuse({**phases, "main_fuse": 35})
    assert guard is not None
    assert guard.phase_import_sensors == ("sensor.l1", "sensor.l2", "sensor.l3")
    assert guard.margin_a == 2.0
    assert guard.ev_charger_current_number is None


def test_reading():
    """Test that a reading is None unless configured, present and a number."""
    hass = hass_with(
        {
            "sensor.soc": state("54.5"),
            "sensor.gone": state("unavailable"),
            "sensor.power": state("1500", "W"),
            "sensor.old": state("3", reported=NOW - dt.timedelta(minutes=5)),
        }
    )
    assert settings.reading(hass, "sensor.soc") == 54.5
    assert settings.reading(hass, None) is None
    assert settings.reading(hass, "sensor.missing") is None
    assert settings.reading(hass, "sensor.gone") is None
    assert settings.reading(hass, "sensor.power", unit="kW") == 1.5
    assert settings.reading(hass, "sensor.power") == 1500.0
    age = dt.timedelta(minutes=2)
    assert settings.reading(hass, "sensor.old", max_age=age, now=NOW) is None
    assert settings.reading(hass, "sensor.soc", max_age=age, now=NOW) == 54.5


def test_replan_entities():
    """Test that the plan triggers cover every platform, the planner choice too."""
    entity_ids = settings.replan_entity_ids()
    assert f"number.{DOMAIN}_battery_reserve_soc" in entity_ids
    assert f"select.{DOMAIN}_planner_state" in entity_ids
    assert f"time.{DOMAIN}_ev_ready_time" in entity_ids
    assert f"datetime.{DOMAIN}_ev_departure" in entity_ids
    assert f"number.{DOMAIN}_forecast_weekend_reserve" not in entity_ids


@pytest.mark.parametrize("platform", [number, select, time, datetime_platform])
async def test_entities_match_the_table(platform):
    """Test that every config entity is a setting and writes the setting's default."""
    hass = MagicMock()
    hass.data = {DOMAIN: {"config": {}, "values": {}}}
    entities = []
    # Not added to a running hass
    with patch.object(Entity, "schedule_update_ha_state"):
        await platform.async_setup_entry(hass, MagicMock(), entities.extend)
    config_ids = {e.id for e in entities if e.data_store == "config"}
    name = platform.__name__.rsplit(".", 1)[-1]
    assert config_ids == {
        s.key for s in settings.SETTINGS.values() if s.platform == name
    }
    for key in config_ids:
        assert hass.data[DOMAIN]["config"].get(key) == settings.default(key)
