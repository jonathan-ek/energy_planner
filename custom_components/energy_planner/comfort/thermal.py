"""Thermal model per room: how fast it cools and heats, and when to start heating.

Pure functions, no Home Assistant imports. A first-order model per room:

    dT/dt = loss * (outdoor - T) + gain + heat * u

with T the room temperature (°C), `loss` 1/h, `gain` °C/h (the sun, people, warm
neighbours) and `heat` °C/h at full heating (u = 1, the share of the hour the room
heats). Over a step with constant inputs the solution is exact: T moves exponentially
towards the equilibrium `outdoor + (gain + heat * u) / loss`.

The rooms are slow (Oct 2026, last winter's Tado data with the valve closed: loss
about 0.004 /h, a time constant of 230-270 h) and the radiators weak, so a target
change needs hours of heating, and some targets can not be reached at all
(`max_temperature`). `plan` starts heating early enough to reach each target on time
(optimum start), assuming the thermostat heats fully below its setpoint.

`heat` depends on the radiator supply temperature, which the district heating
controller sets from the outdoor temperature; it is a constant here until the step
test (Oct 2026) and a winter give data for more.
"""

from collections.abc import Sequence
import dataclasses
import math

import numpy as np

# Fewer samples or a smaller spread of u give no usable fit
MIN_SAMPLES = 24


@dataclasses.dataclass(frozen=True)
class RoomModel:
    """A room's thermal parameters, see the module docstring."""

    loss: float  # 1/h
    gain: float  # °C/h
    heat: float  # °C/h at full heating

    def equilibrium(self, outdoor: float, heating: float) -> float:
        """Return the temperature the room settles at with constant inputs."""
        return outdoor + (self.gain + self.heat * heating) / self.loss

    def step(self, temp: float, outdoor: float, heating: float, hours: float) -> float:
        """Return the temperature after `hours` with constant inputs."""
        target = self.equilibrium(outdoor, heating)
        return target + (temp - target) * math.exp(-self.loss * hours)


def max_temperature(model: RoomModel, outdoor: float) -> float:
    """Return the highest temperature the room can hold at an outdoor temperature."""
    return model.equilibrium(outdoor, 1.0)


def warmup_hours(
    model: RoomModel, temp: float, target: float, outdoor: float
) -> float | None:
    """Return the hours of full heating from `temp` to `target`, None if unreachable."""
    if temp >= target:
        return 0.0
    top = max_temperature(model, outdoor)
    if top <= target:
        return None
    return math.log((top - temp) / (top - target)) / model.loss


def fit(
    samples: Sequence[tuple[float, float, float, float, float]],
) -> RoomModel | None:
    """Fit a model to (temp, next temp, outdoor, heating share, hours) samples.

    Linear least squares on the rate of change; None with fewer than MIN_SAMPLES,
    without both heating and non-heating samples, or a non-physical result.
    """
    if len(samples) < MIN_SAMPLES:
        return None
    data = np.array(samples, dtype=float)
    temp, after, outdoor, heating, hours = data.T
    if heating.max() - heating.min() < 0.5:
        return None
    rate = (after - temp) / hours
    columns = np.c_[outdoor - (temp + after) / 2, np.ones(len(data)), heating]
    (loss, gain, heat), *_ = np.linalg.lstsq(columns, rate, rcond=None)
    if loss <= 0 or heat <= 0:
        return None
    return RoomModel(float(loss), float(gain), float(heat))


@dataclasses.dataclass
class RoomPlan:
    """Per step: the setpoint to send, whether the room heats, and its temperature."""

    setpoints: list[float]
    heating: list[bool]
    temperatures: list[float]  # predicted at the end of each step


def plan(
    model: RoomModel,
    temp: float,
    targets: Sequence[float],
    outdoor: Sequence[float],
    hours: float = 0.25,
    lookahead: float = 12.0,
) -> RoomPlan:
    """Plan the setpoints so each target is reached on time (optimum start).

    `targets` and `outdoor` are per step of `hours`, from now. Backwards from the end,
    the temperature needed at each step is the higher of its target and what full
    heating needs to reach the next step's need (a target the room can not reach is
    capped at its maximum). Forwards, while the room is below the next step's need
    for a higher target the setpoint is the highest target within `lookahead` (so the
    thermostat heats fully), otherwise the step's target; the room heats below its
    setpoint.
    """
    count = len(targets)
    need = [0.0] * count
    following = -math.inf
    for index in range(count - 1, -1, -1):
        top = max_temperature(model, outdoor[index])
        required = following
        if math.isfinite(following):
            # Invert one step of full heating: where it must start to end at need
            required = top + (min(following, top - 1e-6) - top) * math.exp(
                model.loss * hours
            )
        need[index] = max(min(targets[index], top), required)
        following = need[index]

    window = max(int(lookahead / hours), 1)
    setpoints, heating, temperatures = [], [], []
    current = temp
    for index in range(count):
        # Early for a higher target that needs it, else the target in force; the
        # thermostat heats below its setpoint
        early = (
            index + 1 < count
            and need[index + 1] > targets[index]
            and current < need[index + 1] - 1e-3
        )
        setpoints.append(
            max(targets[index : index + window]) if early else targets[index]
        )
        on = current < setpoints[-1]
        if on:
            reach = min(setpoints[-1], max_temperature(model, outdoor[index]))
            share = 1.0
            full = model.step(current, outdoor[index], 1.0, hours)
            if full > reach:
                # The thermostat stops at the setpoint: heat only part of the step
                free = model.equilibrium(outdoor[index], 0.0)
                share = min(max((reach - free) / (model.heat / model.loss), 0.0), 1.0)
                current = min(model.step(current, outdoor[index], share, hours), reach)
            else:
                current = full
        else:
            current = model.step(current, outdoor[index], 0.0, hours)
        heating.append(on)
        temperatures.append(current)
    return RoomPlan(setpoints, heating, temperatures)
