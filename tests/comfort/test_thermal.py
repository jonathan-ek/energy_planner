"""Tests for the room thermal model and the setpoint plan."""

import math

import numpy as np
import pytest

from custom_components.energy_planner.comfort import thermal

# Like the bedroom last winter: slow, and heats about 0.3 °C/h
ROOM = thermal.RoomModel(loss=0.004, gain=0.02, heat=0.4)


def test_step_moves_towards_the_equilibrium():
    """Test the exact solution and the equilibrium."""
    assert ROOM.equilibrium(0.0, 0.0) == pytest.approx(5.0)
    assert ROOM.equilibrium(0.0, 1.0) == pytest.approx(105.0)
    after = ROOM.step(20.0, 0.0, 0.0, 10.0)
    assert after == pytest.approx(5 + 15 * math.exp(-0.04))
    assert 19.0 < after < 20.0


def test_warmup_hours():
    """Test the heating time and an unreachable target."""
    hours = thermal.warmup_hours(ROOM, 18.0, 19.0, 0.0)
    assert hours is not None
    assert ROOM.step(18.0, 0.0, 1.0, hours) == pytest.approx(19.0)
    assert thermal.warmup_hours(ROOM, 20.0, 19.0, 0.0) == 0.0
    weak = thermal.RoomModel(loss=0.004, gain=0.0, heat=0.06)
    assert thermal.max_temperature(weak, -5.0) == pytest.approx(10.0)
    assert thermal.warmup_hours(weak, 18.0, 19.0, -5.0) is None


def test_fit_recovers_the_model():
    """Test the fit on simulated hourly data with noise."""
    rng = np.random.default_rng(1)
    samples, temp = [], 20.0
    for hour in range(400):
        outdoor = 5 + 5 * math.sin(hour / 12)
        heating = 1.0 if (hour // 6) % 2 else 0.0
        after = ROOM.step(temp, outdoor, heating, 1.0) + rng.normal(0, 0.01)
        samples.append((temp, after, outdoor, heating, 1.0))
        temp = after
    model = thermal.fit(samples)
    assert model is not None
    assert model.loss == pytest.approx(ROOM.loss, abs=0.002)
    assert model.heat == pytest.approx(ROOM.heat, rel=0.1)


def test_fit_needs_heating_and_not_heating():
    """Test that a fit without both kinds of samples gives nothing."""
    samples = [(20.0, 19.99, 0.0, 0.0, 1.0)] * 50
    assert thermal.fit(samples) is None
    assert thermal.fit(samples[:5]) is None


def test_plan_starts_early_to_reach_the_target():
    """Test optimum start: 18 -> 20 at step 40, heating starts hours before."""
    targets = [18.0] * 40 + [20.0] * 20
    outdoor = [0.0] * 60
    result = thermal.plan(ROOM, 18.0, targets, outdoor)
    # Before it the thermostat only holds 18
    first = result.setpoints.index(20.0)
    hours = thermal.warmup_hours(ROOM, result.temperatures[first - 1], 20.0, 0.0)
    assert hours is not None
    assert 0 < first < 40
    assert (40 - first) * 0.25 == pytest.approx(hours, abs=0.5)
    assert result.setpoints[0] == 18.0
    assert result.temperatures[39] == pytest.approx(20.0, abs=0.1)
    # The thermostat holds it, no overshoot
    assert max(result.temperatures) <= 20.0 + 1e-6


def test_plan_coasts_down():
    """Test that a lower target lets the room cool freely."""
    targets = [22.0] * 4 + [18.0] * 40
    result = thermal.plan(ROOM, 22.0, targets, [0.0] * 44)
    assert not any(result.heating[4:])
    assert result.setpoints[10] == 18.0
    assert result.temperatures[-1] < 22.0


def test_plan_caps_an_unreachable_target():
    """Test that a weak room heats as much as it can without an endless start."""
    weak = thermal.RoomModel(loss=0.004, gain=0.0, heat=0.1)
    targets = [18.0] * 8 + [24.0] * 8
    result = thermal.plan(weak, 20.0, targets, [0.0] * 16)
    assert all(
        t <= thermal.max_temperature(weak, 0.0) + 1e-6 for t in result.temperatures
    )
