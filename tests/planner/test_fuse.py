"""Tests for the main fuse guard."""

import datetime as dt
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_planner.const import DOMAIN
from custom_components.energy_planner.planner import fuse, fuse_guard

TZ = ZoneInfo("Europe/Stockholm")
NOW = dt.datetime(2026, 10, 11, 11, 47, tzinfo=TZ)
# The owner's parents at 11:47: the battery charging from the grid at 17 kW, most
# of the house's load on L3
CHARGING = (5.832, 6.119, 8.035)
CAR_KW = 16 * 230 / 1000


def per_phase(limits, inverter_kw, ev_a, house_a):
    """Return the phase currents when the inverter imports `grid_limit_kw`."""
    house_w = sum(house_a) * fuse.VOLTAGE
    inverter_w = limits["grid_limit_kw"] * 1000 - house_w - 3 * ev_a * fuse.VOLTAGE
    inverter_w = min(inverter_w, inverter_kw * 1000)
    return [a + ev_a + inverter_w / 3 / fuse.VOLTAGE for a in house_a]


def test_battery_charging_is_limited_by_the_busiest_phase():
    """Test that the grid limit keeps L3 at the fuse minus the margin."""
    limits = fuse.limits(
        fuse.Reading(CHARGING, inverter_kw=17.17), 35, 2, 16, ev_wanted=False
    )
    assert limits["over_a"] == pytest.approx(1.9, abs=0.05)
    assert limits["grid_limit_kw"] < sum(CHARGING)
    currents = per_phase(limits, 17.17, 0, limits["house_a"])
    assert max(currents) == pytest.approx(33.0, abs=0.1)
    # The car is left alone while it should not charge
    assert limits["ev_current_a"] == 16


def test_the_car_comes_first():
    """Test that the car keeps its current and the battery gets the rest."""
    phases = tuple(kw + CAR_KW for kw in CHARGING)
    reading = fuse.Reading(phases, inverter_kw=17.17, ev_a=16, ev_setting_a=16)
    limits = fuse.limits(reading, 35, 2, 16, ev_wanted=True)
    assert limits["ev_current_a"] == 16
    currents = per_phase(limits, 17.17, 16, limits["house_a"])
    assert max(currents) == pytest.approx(33.0, abs=0.1)


def test_the_car_is_lowered_when_the_battery_can_not_help():
    """Test that a large house load on one phase lowers or stops the car."""
    reading = fuse.Reading((0.5 + CAR_KW, 0.6 + CAR_KW, 5.5 + CAR_KW), 0.0, 16, 16)
    limits = fuse.limits(reading, 35, 2, 16, ev_wanted=True)
    # L3: 23.9 A house + 16 A car, 6.9 A over: the car down to 9 A
    assert limits["ev_current_a"] == 9
    reading = fuse.Reading((0.5 + CAR_KW, 0.6 + CAR_KW, 7.5 + CAR_KW), 0.0, 16, 16)
    assert fuse.limits(reading, 35, 2, 16, ev_wanted=True)["ev_current_a"] == 0


def test_a_late_car_reading_does_not_lower_the_car():
    """Test that the car's draw counted as the house's does not lower its setting."""
    phases = tuple(kw + CAR_KW for kw in CHARGING)
    # The car charges at 16 A, but the charger still reports 0 A
    reading = fuse.Reading(phases, inverter_kw=17.17, ev_a=0, ev_setting_a=16)
    limits = fuse.limits(reading, 35, 2, 16, ev_wanted=True)
    assert limits["ev_current_a"] == 16
    # The battery takes it all off, more than needed until the car is reported
    assert limits["grid_limit_kw"] < 18.0


def test_the_car_is_raised_to_its_share():
    """Test that a lowered car gets the room back, up to the charger's highest."""
    reading = fuse.Reading((1.0, 1.0, 2.0), ev_a=8, ev_setting_a=8)
    assert fuse.limits(reading, 35, 2, 16, ev_wanted=True)["ev_current_a"] == 16
    reading = fuse.Reading((1.0, 1.0, 6.0), ev_a=8, ev_setting_a=8)
    # L3: 26.1 A - 8 A car = 18.1 A house: 14.9 A left
    assert fuse.limits(reading, 35, 2, 16, ev_wanted=True)["ev_current_a"] == 14


def test_smooth_lowers_at_once_and_raises_slowly():
    """Test the hysteresis for the devices."""
    minute = dt.timedelta(minutes=1)
    value = fuse.smooth(None, 10.0, NOW, 1.0, 15 * minute)
    assert value == (10.0, NOW)
    lower = fuse.smooth(value, 8.0, NOW + minute, 1.0, 15 * minute, 0.5)
    assert lower == (7.5, NOW + minute)
    # Up by less than a step, or too soon: kept
    assert fuse.smooth(lower, 8.0, NOW + 30 * minute, 1.0, 15 * minute) == lower
    assert fuse.smooth(lower, 12.0, NOW + 5 * minute, 1.0, 15 * minute) == lower
    raised = fuse.smooth(lower, 12.0, NOW + 20 * minute, 1.0, 15 * minute)
    assert raised == (12.0, NOW + 20 * minute)


def state(value, unit=None, reported=NOW):
    """Return a state, fresh by default."""
    return SimpleNamespace(
        state=str(value),
        attributes={"unit_of_measurement": unit} if unit else {},
        last_reported=reported,
    )


def test_update_guard_reads_the_sensors():
    """Test the guard from HA states, with W and kW and a missing reading."""
    states = {
        "sensor.l1": state(5.832, "kW"),
        "sensor.l2": state(6119, "W"),
        "sensor.l3": state(8.035, "kW"),
        "sensor.inverter": state(-17170, "W"),
        "sensor.ev1": state(0.0, "A"),
        "number.ev": state(16.0, "A"),
    }
    hass = MagicMock()
    hass.states.get = states.get
    data: dict[str, Any] = {
        DOMAIN: {
            "config": {
                "main_fuse": 35,
                "fuse_margin": 2,
                "grid_phase_import_sensors": ["sensor.l1", "sensor.l2", "sensor.l3"],
                "inverter_ac_power_sensor": "sensor.inverter",
                "ev_phase_current_sensors": ["sensor.ev1"],
                "ev_charger_current_number": "number.ev",
            }
        }
    }
    hass.data = data
    with patch.object(fuse_guard.dt_utils, "now", return_value=NOW):
        fuse_guard.update_guard(hass)
    guard = hass.data[DOMAIN]["fuse_guard"]
    assert guard["phase_a"] == [25.4, 26.6, 34.9]
    assert guard["grid_limit_kw"] == pytest.approx(18.6, abs=0.05)
    assert guard["ev_current_a"] == 16
    # The inverter's own meter reads 1.3 kW less: its limit is 1.3 kW lower
    states["sensor.meter"] = state(18705, "W")
    hass.data[DOMAIN]["config"]["inverter_grid_power_sensor"] = "sensor.meter"
    # (a fresh start: lowering it would also take the smoothing margin off)
    del hass.data[DOMAIN]["fuse_guard_smoothing"]
    with patch.object(fuse_guard.dt_utils, "now", return_value=NOW):
        fuse_guard.update_guard(hass)
    guard = hass.data[DOMAIN]["fuse_guard"]
    assert guard["inverter_meter_offset_kw"] == pytest.approx(1.28, abs=0.01)
    assert guard["grid_limit_kw"] == pytest.approx(18.65 - 1.28, abs=0.06)
    # While the published car action is to charge, room is kept for the car,
    # unless it has finished
    states["sensor.charger"] = state("connected_charging")
    hass.data[DOMAIN]["config"]["ev_charger_status_sensor"] = "sensor.charger"
    hass.data[DOMAIN]["actions"] = SimpleNamespace(car={"state": "charge"})
    del hass.data[DOMAIN]["fuse_guard_smoothing"]
    with patch.object(fuse_guard.dt_utils, "now", return_value=NOW):
        fuse_guard.update_guard(hass)
        guard = hass.data[DOMAIN]["fuse_guard"]
        # 16 A per phase kept for the car
        assert guard["grid_limit_kw"] == pytest.approx(
            18.65 - 1.28 - 3 * CAR_KW, abs=0.06
        )
        states["sensor.charger"] = state("connected_finished")
        del hass.data[DOMAIN]["fuse_guard_smoothing"]
        fuse_guard.update_guard(hass)
    guard = hass.data[DOMAIN]["fuse_guard"]
    assert guard["grid_limit_kw"] == pytest.approx(18.65 - 1.28, abs=0.06)
    # An unchanged phase is not reported again (MQTT): used while the meter reports
    states["sensor.l1"] = state(5.832, "kW", NOW - dt.timedelta(minutes=10))
    with patch.object(fuse_guard.dt_utils, "now", return_value=NOW):
        fuse_guard.update_guard(hass)
    assert hass.data[DOMAIN]["fuse_guard"] is not guard
    guard = hass.data[DOMAIN]["fuse_guard"]
    # A silent meter keeps the last guard
    later = NOW + dt.timedelta(minutes=5)
    with patch.object(fuse_guard.dt_utils, "now", return_value=later):
        fuse_guard.update_guard(hass)
    assert hass.data[DOMAIN]["fuse_guard"] is guard
    # A missing phase keeps the last guard
    states["sensor.l2"] = state("unavailable")
    with patch.object(fuse_guard.dt_utils, "now", return_value=NOW):
        fuse_guard.update_guard(hass)
    assert hass.data[DOMAIN]["fuse_guard"] is guard
    # Without a fuse there is no guard
    hass.data[DOMAIN]["config"]["main_fuse"] = 0
    fuse_guard.update_guard(hass)
    assert hass.data[DOMAIN]["fuse_guard"] is None


def test_the_sensors_are_tracked_before_a_fuse_is_set():
    """Test that main_fuse 0 still lists the sensors, so a fuse set later is live."""
    config = {
        "main_fuse": 0,
        "grid_phase_import_sensors": ["sensor.l1", "sensor.l2", "sensor.l3"],
        "inverter_ac_power_sensor": "sensor.inverter",
        "inverter_grid_power_sensor": "",
    }
    assert fuse_guard.config_entities(config) == [
        "sensor.l1",
        "sensor.l2",
        "sensor.l3",
        "sensor.inverter",
    ]
    assert fuse_guard.config_entities({}) == []


@pytest.mark.parametrize(
    ("status", "published", "wanted"),
    [
        ("disconnected", "charge", False),
        ("connected_charging", "charge", True),
        ("connected_charging", "wait", False),
        # Plugged in since the action was published: charges as without the planner
        ("connected_requesting", None, True),
        # No actions (resolving failed): the same
        ("connected_requesting", "none", True),
        # A car that has finished draws nothing
        ("connected_finished", "charge", False),
        # The car's own state (Tesla BLE) while the charger lags: full
        ("Complete", "charge", False),
    ],
)
def test_car_wanted_from_the_published_action_and_the_live_charger(
    status, published, wanted
):
    """Test that room is kept for a car that may charge now."""
    hass = MagicMock()
    hass.states.get = {"sensor.charger": state(status)}.get
    actions = None if published == "none" else SimpleNamespace(car={"state": published})
    data: dict[str, Any] = {
        DOMAIN: {
            "config": {"ev_charger_status_sensor": "sensor.charger"},
            "actions": actions,
        }
    }
    hass.data = data
    assert fuse_guard._car_wanted(hass, NOW) is wanted


def test_the_car_circuit_fuse_limits_the_car_not_the_battery():
    """Test Kidde's 16 A house and car circuit, the battery on the 35 A side."""
    # House 8 A on L1 (circuit and grid), the car at 0 A, the battery idle
    circuit = (8 * 0.23, 2 * 0.23, 2 * 0.23)
    reading = fuse.Reading(circuit, ev_a=0, ev_setting_a=0, circuit_kw=circuit)
    limits = fuse.limits(reading, 35, 2, 10, ev_wanted=True, circuit_a=16)
    # 16 - 2 margin - 8 A: the car gets 6 A, not its highest 10 A
    assert limits["ev_current_a"] == 6
    assert limits["circuit_room_a"] == 6.0
    # The battery still gets the main fuse's room: (33 - 8 - 6 car) A x 3 + house
    assert limits["grid_limit_kw"] == pytest.approx((19 * 3 + 12) * 0.23, abs=0.01)
    # Without a circuit fuse: unchanged
    assert fuse.limits(reading, 35, 2, 10, ev_wanted=True)["ev_current_a"] == 10


def test_an_overloaded_car_circuit_lowers_the_car():
    """Test that a circuit phase over its fuse lowers the car, battery or not."""
    # L1: 9 A house + 10 A car = 19 A, 5 A over 16 - 2; the battery charges 9 kW
    circuit = (19 * 0.23, 12 * 0.23, 12 * 0.23)
    phases = tuple(kw + 3.0 for kw in circuit)
    reading = fuse.Reading(phases, 9.0, 10, 10, circuit_kw=circuit)
    limits = fuse.limits(reading, 35, 2, 10, ev_wanted=True, circuit_a=16)
    assert limits["ev_current_a"] == 0  # 10 - 5 = 5 A is below the car's 6 A
    circuit = (17 * 0.23, 12 * 0.23, 12 * 0.23)
    reading = fuse.Reading(phases, 9.0, 10, 10, circuit_kw=circuit)
    assert (
        fuse.limits(reading, 35, 2, 10, ev_wanted=True, circuit_a=16)["ev_current_a"]
        == 7
    )


def test_a_late_car_reading_trims_the_car_on_the_circuit():
    """Test that a car not reported yet is lowered from its setting, not stopped."""
    # Raised to 8 A, it draws, the circuit reads 14.3 A while the car reads 0 A
    circuit = (14.3 * 0.23, 10 * 0.23, 10 * 0.23)
    reading = fuse.Reading(circuit, ev_a=0, ev_setting_a=8, circuit_kw=circuit)
    limits = fuse.limits(reading, 35, 0, 10, ev_wanted=True, circuit_a=14)
    assert limits["ev_current_a"] == 7


def test_an_overloaded_circuit_lowers_a_car_that_should_not_charge():
    """Test that a car charging by hand is lowered when the circuit is over."""
    circuit = (20 * 0.23, 12 * 0.23, 12 * 0.23)
    reading = fuse.Reading(circuit, ev_a=10, ev_setting_a=10, circuit_kw=circuit)
    assert (
        fuse.limits(reading, 35, 0, 10, ev_wanted=False, circuit_a=16)["ev_current_a"]
        == 6
    )
    # Not over: left alone
    circuit = (14 * 0.23, 12 * 0.23, 12 * 0.23)
    reading = fuse.Reading(circuit, ev_a=10, ev_setting_a=10, circuit_kw=circuit)
    limits = fuse.limits(reading, 35, 0, 10, ev_wanted=False, circuit_a=16)
    assert limits["ev_current_a"] == 10
    assert limits["circuit_room_a"] == 12.0


def test_a_missing_circuit_reading_holds_the_car_at_its_lowest():
    """Test that without the circuit's load the car gets 6 A, the battery its room."""
    phases = (1.0, 1.0, 1.0)
    reading = fuse.Reading(phases, ev_a=10, ev_setting_a=10, circuit_kw=None)
    limits = fuse.limits(reading, 35, 0, 10, ev_wanted=True, circuit_a=16)
    assert limits["ev_current_a"] == fuse.EV_MIN_A
    assert "circuit_room_a" not in limits
    assert limits["grid_limit_kw"] > 0
    # Without a circuit fuse the same reading is unchanged
    assert fuse.limits(reading, 35, 0, 10, ev_wanted=True) == fuse.limits(
        fuse.Reading(phases, ev_a=10, ev_setting_a=10), 35, 0, 10, ev_wanted=True
    )


def test_update_guard_reads_the_circuit_only_with_its_fuse():
    """Test the circuit sensors from HA states: W, missing, and off at 0 A."""
    states = {
        "sensor.l1": state(1.0, "kW"),
        "sensor.l2": state(1.0, "kW"),
        "sensor.l3": state(1.0, "kW"),
        "sensor.c1": state(3220, "W"),
        "sensor.c2": state(1.0, "kW"),
        "sensor.c3": state(1.0, "kW"),
        "sensor.ev1": state(0.0, "A"),
        "sensor.ev": state(0.0, "A"),
    }
    hass = MagicMock()
    hass.states.get = states.get
    hass.data = {
        DOMAIN: {
            "config": {
                "main_fuse": 35,
                "fuse_margin": 0,
                "ev_charger_max_power": 6900,
                "ev_circuit_fuse": 16,
                "grid_phase_import_sensors": ["sensor.l1", "sensor.l2", "sensor.l3"],
                "ev_phase_current_sensors": ["sensor.ev1"],
                "ev_charger_current_number": "sensor.ev",
                "ev_circuit_load_sensors": ["sensor.c1", "sensor.c2", "sensor.c3"],
            }
        }
    }
    with (
        patch.object(fuse_guard.dt_utils, "now", return_value=NOW),
        patch.object(fuse_guard, "_car_wanted", return_value=True),
    ):
        fuse_guard.update_guard(hass)
        # L1: 3.22 kW = 14 A, 2 A left: too little for the car
        assert hass.data[DOMAIN]["fuse_guard"]["circuit_room_a"] == 2.0
        assert hass.data[DOMAIN]["fuse_guard"]["ev_current_a"] == 0
        del states["sensor.c3"]
        fuse_guard.update_guard(hass)
        assert "circuit_room_a" not in hass.data[DOMAIN]["fuse_guard"]
        # Off: the missing sensor is not read
        hass.data[DOMAIN]["config"]["ev_circuit_fuse"] = 0
        hass.data[DOMAIN].pop("fuse_guard_smoothing")
        fuse_guard.update_guard(hass)
        assert hass.data[DOMAIN]["fuse_guard"]["ev_current_a"] == 10
